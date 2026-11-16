"""The home screen: what ``asl`` shows when it is run with no command.

One glance at the three things worth knowing on arrival: what is running,
what is waiting on a person, and what finished lately. Every row is built from
the same :class:`PlanSummary` records ``asl plans`` lists, so this view
classifies nothing itself; it only chooses which plans are current enough to
show and names each one by its title rather than its hashed directory.
"""

from __future__ import annotations

import argparse
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from typing import Any

from . import assurance_projection, assurance_report, plans_files, responses
from .assurance_models_primitives import AssuranceError
from .cli_defaults import program_name
from .formatting import count_noun, format_relative_age
from .plan_pins import load_registry
from .plan_pins_models import PlanRegistryError
from .plan_scan import discover_plans
from .plan_summary import build_plan_summaries
from .plan_summary_model import PlanSummary

HOME_COMMAND_NAME = "home"

#: A plan that needed someone weeks ago is history, not a notification.
NEEDS_ACTION_WINDOW = timedelta(days=7)
#: How far back a finished plan still counts as "just finished".
FINISHED_WINDOW = timedelta(days=3)
#: How many finished plans to show before saying how many more there are.
FINISHED_LIMIT = 5
#: How many characters of the hashed directory name identify a plan here.
SHORT_ID_LENGTH = 12

_NEXT_COMMAND_BY_ACTION = {
    "reply": "questions",
    "inspect-failure": "status",
    "unlock": "status",
    "repair-plan": "preflight",
}


def _parse(stamp: str | None) -> datetime | None:
    if not stamp:
        return None
    try:
        parsed = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


def _row(summary: PlanSummary, *, now: datetime) -> dict[str, Any]:
    """The display facts of one plan; also the row's ``--json`` shape."""
    counts = summary.counts
    complete = counts.get("complete", 0)
    total = complete + counts.get("pending", 0) + counts.get("unknown", 0)
    activity = _parse(summary.last_activity_at)
    command = _NEXT_COMMAND_BY_ACTION.get(summary.recommended_action)
    short_id = summary.plan_dir.name[:SHORT_ID_LENGTH]
    return {
        "plan_dir": str(summary.plan_dir),
        "plan_id": summary.plan_dir.name,
        "short_id": short_id,
        "title": (
            summary.label
            or plans_files.read_plan_title(summary.plan_dir)
            or summary.plan_dir.name
        ),
        "primary_state": summary.primary_state,
        "urgency_group": summary.urgency_group,
        "recommended_action": summary.recommended_action,
        "headline": summary.next_action_hint,
        "reason": summary.concise_reason,
        "sessions_complete": complete,
        "sessions_total": total,
        "last_activity_at": summary.last_activity_at,
        "age": format_relative_age(activity, now=now),
        "next_command": (
            f"{program_name()} {command} {short_id}" if command is not None else None
        ),
    }


def _recent(summary: PlanSummary, *, now: datetime, window: timedelta) -> bool:
    activity = _parse(summary.last_activity_at)
    return activity is not None and now - activity <= window


def build_home(summaries: Sequence[PlanSummary], *, now: datetime) -> dict[str, Any]:
    """Sort plan summaries into the home screen's sections.

    ``needs_you`` and ``finished`` keep only plans active inside their window
    and report how many older ones were left out, so a long history cannot
    bury today's work and nothing disappears without being counted.
    """
    running: list[PlanSummary] = []
    needs: list[PlanSummary] = []
    older_needs = 0
    ready: list[PlanSummary] = []
    finished: list[PlanSummary] = []
    for summary in summaries:
        group = summary.urgency_group
        if group == "in-progress":
            running.append(summary)
        elif group == "needs-action":
            if _recent(summary, now=now, window=NEEDS_ACTION_WINDOW):
                needs.append(summary)
            else:
                older_needs += 1
        elif group == "ready":
            ready.append(summary)
        elif group == "complete" and _recent(summary, now=now, window=FINISHED_WINDOW):
            finished.append(summary)

    def newest_first(rows: list[PlanSummary]) -> list[PlanSummary]:
        return sorted(rows, key=lambda row: row.last_activity_at or "", reverse=True)

    finished = newest_first(finished)
    return {
        "running": [_row(row, now=now) for row in newest_first(running)],
        "needs_you": [_row(row, now=now) for row in newest_first(needs)],
        "needs_you_older": older_needs,
        "ready": [_row(row, now=now) for row in ready],
        "finished": [_row(row, now=now) for row in finished[:FINISHED_LIMIT]],
        "finished_more": max(0, len(finished) - FINISHED_LIMIT),
    }


def _print_rows(rows: Sequence[dict[str, Any]], *, show_next: bool) -> None:
    for row in rows:
        print(f"  {row['short_id']}  {row['title']}")
        progress = f"{row['sessions_complete']}/{row['sessions_total']} sessions"
        detail = row["headline"] or row["reason"] or row["primary_state"]
        age = f" · {row['age']}" if row["age"] else ""
        print(f"                {detail} · {progress}{age}")
        if show_next and row["reason"] and row["reason"] != detail:
            print(f"                {row['reason']}")
        if show_next and row["next_command"]:
            print(f"                next: {row['next_command']}")


def _print_home(home: dict[str, Any]) -> None:
    program = program_name()
    needs = home["needs_you"]
    running = home["running"]
    finished = home["finished"]
    if needs:
        print(f"Needs you ({len(needs)}):")
        _print_rows(needs, show_next=True)
    else:
        print("Nothing needs you.")
    if home["needs_you_older"]:
        older = count_noun(home["needs_you_older"], "older plan")
        print(f"  ({older} stopped more than a week ago; `{program} plans`)")
    print()
    if running:
        print(f"Running ({len(running)}):")
        _print_rows(running, show_next=False)
    else:
        print("Nothing is running.")
    if finished:
        print()
        print(f"Finished lately ({len(finished) + home['finished_more']}):")
        _print_rows(finished, show_next=False)
        if home["finished_more"]:
            print(f"  (+{home['finished_more']} more; `{program} plans`)")
    ready = home["ready"]
    if ready:
        print()
        print(
            f"{count_noun(len(ready), 'prepared plan')} not started yet; "
            f"`{program} plans --never-run`"
        )
    open_issues = home.get("assurance_open")
    if isinstance(open_issues, int) and open_issues:
        most_seen = home.get("assurance_most_seen")
        lead = (
            f"; {most_seen['issue_id']} has been seen "
            f"{most_seen['occurrence_count']} time(s): "
            f"`{program} assurance show {most_seen['issue_id']}`"
            if isinstance(most_seen, dict)
            else f"; `{assurance_report.LIST_COMMAND}`"
        )
        print(f"{count_noun(open_issues, 'open assurance issue')}{lead}")
    print()
    print(
        f"`{program} status ID` shows one plan (an id prefix or a word of the "
        f"title works); `{program} --help` lists every command."
    )


def _assurance_summary(
    state_dir: str | None,
) -> tuple[int | None, dict[str, Any] | None]:
    """How many assurance issues are open, and which one recurs most.

    Naming the most-seen issue turns a count nobody acts on into one concrete
    thing to open. An unreadable ledger yields ``(None, None)``: the home
    screen says nothing rather than claiming there are none.
    """
    try:
        open_issues = assurance_projection.read_projection(
            state_dir=state_dir
        ).open_issues
    except OSError, AssuranceError:
        return None, None
    if not open_issues:
        return 0, None
    most_seen = max(open_issues, key=lambda issue: issue.occurrence_count)
    return len(open_issues), {
        "issue_id": most_seen.issue_id,
        "occurrence_count": most_seen.occurrence_count,
    }


def home_from_args(args: argparse.Namespace, *, now: datetime | None = None) -> int:
    """Print the home screen, or its sections as JSON under ``--json``."""
    moment = datetime.now(UTC) if now is None else now
    state_dir = getattr(args, "state_dir", None)
    try:
        registry = load_registry(None)
    except PlanRegistryError:
        # The registry only adds pinned plans and labels; a broken one must
        # not take the front door down with it. `asl plans` reports it.
        registry = None
    discovery = discover_plans(state_root=state_dir, registry=registry)
    summaries = build_plan_summaries(discovery.plans, state_dir=state_dir)
    home = build_home(summaries, now=moment)
    home["assurance_open"], home["assurance_most_seen"] = _assurance_summary(state_dir)
    if getattr(args, "json", False):
        responses.print_json(
            responses.success_payload(HOME_COMMAND_NAME, reason="ok", **home)
        )
        return 0
    _print_home(home)
    return 0
