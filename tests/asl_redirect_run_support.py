"""Shared whole-loop fixture for the redirect continuation test modules.

Split from tests/test_asl_redirect_continuation.py."""

from __future__ import annotations

import argparse
import json
import os
import re
import tempfile
import threading
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

from agent_session_loop import (
    config,
    events,
    harnesses,
    plans_handoff,
    plans_models,
    plans_session_ids,
    redirect_ack,
    redirect_control,
    redirect_store_journal,
    redirect_store_models,
    redirect_store_requests,
    review_guard,
    run_commit,
    run_control,
    run_loop,
    run_selection,
    state,
    state_locks,
)

from . import asl_redirect_continuation_support as continuation_support
from .asl_redirect_continuation_support import FEEDBACK, RUN_ID, SESSION_ID
from .support import (
    REPO_ROOT,
    FakeRunControlListener,
    init_git_repo,
    install_fake_harness,
    patched_env,
    read_jsonl,
    wait_until,
    write_plan,
)


class _RedirectedRun(unittest.TestCase):
    """The whole-loop fixture: a real run around a fake worker.

    Everything real is real here -- the plan, the journal, the run state, the
    conversation ledger and a git repository with commits enabled -- and only
    the worker and the four process operations are fakes. Kept apart from the
    claims that use it because more than one story needs the same run, and each
    of them changes the worker; a fixture that also carried tests would run the
    first story's assertions against the second story's worker.
    """

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        self.plan_dir = self.tmp / "plan"
        self.state_dir = self.tmp / "state"
        self.repo_root = self.tmp / "repo"
        self.config_home = self.tmp / "config"
        init_git_repo(self.repo_root)
        harness_config = {
            name: {"executable": str(install_fake_harness(self.tmp, name))}
            for name in ("codex", "claude")
        }
        config_path = self.config_home / config.APP_NAME / config.HARNESS_CONFIG_NAME
        config_path.parent.mkdir(parents=True, exist_ok=True)
        config_path.write_text(json.dumps(harness_config), encoding="utf-8")

        self.wakeup = threading.Event()
        self.harness_calls: list[str] = []
        self.commit_calls: list[dict[str, Any]] = []
        self.commits_before_resume: int | None = None
        self.status_at_resume: str | None = None
        self.submitted: redirect_store_models.RedirectRequest | None = None
        self.receipt: redirect_ack.Acknowledgment | None = None
        self.stale_refusal: redirect_ack.RedirectAckError | None = None

    # -- the fakes -----------------------------------------------------------

    def _session_status(self) -> str:
        return plans_handoff.read_session_status(
            plans_models.SessionFile(
                plans_session_ids.parse_session_id(SESSION_ID),
                self.plan_dir / plans_session_ids.session_id_to_filename(SESSION_ID),
            )
        ).status

    def _complete_session(self) -> None:
        path = self.plan_dir / plans_session_ids.session_id_to_filename(SESSION_ID)
        text = path.read_text(encoding="utf-8")
        path.write_text(
            text.replace("- **Status**: pending", "- **Status**: complete", 1),
            encoding="utf-8",
        )

    def _controller_factory(self):
        """The real controller with stand-in process tools.

        Nothing may signal a real process group from a test, and the escalation
        is not what this case is about: what matters is that the report the
        controller publishes is the one the successor gate reads.
        """
        real = redirect_control.UrgentController
        worker = {
            "root_pid": continuation_support.WORKER_PID,
            "root_pgid": continuation_support.WORKER_PID,
            "entries": [],
            "escaped_pids": [],
            "collected": True,
            "disabled": None,
            "sample_error": None,
        }
        tools = redirect_control.TerminationTools(
            collect_evidence=lambda root_pid, **_kwargs: dict(worker),
            force_stop=lambda pgid, **_kwargs: {
                "pgid": pgid,
                "signal": "SIGKILL",
                "signalled": True,
                "already_gone": False,
                "refused": None,
                "error": None,
            },
            terminate_escapees=lambda evidence, **_kwargs: {
                "policy": "identity_checked_sigkill",
                "candidate_pids": [],
                "killed": [],
            },
            confirm=lambda *, pgid, evidence, **_kwargs: {
                "pgid": pgid,
                "group": "stopped",
                "remaining_pids": [],
                "pid_reused": [],
                "unverified_pids": [],
                "ownership_unknown": False,
                "complete": True,
                "incomplete_reason": None,
            },
        )

        def build(**kwargs: Any) -> redirect_control.UrgentController:
            kwargs.setdefault("tools", tools)
            kwargs.setdefault("window_seconds", 0.05)
            kwargs.setdefault("evidence_wait_seconds", 2.0)
            kwargs.setdefault("collection_wait_seconds", 5.0)
            return real(**kwargs)

        return build

    def _fake_worker(self, name: str, **kwargs: Any) -> harnesses.HarnessResult:
        """A worker that is corrected on its first turn and finishes on its next."""
        self.harness_calls.append(name)
        abort = kwargs["abort"]
        on_spawn = kwargs.get("on_spawn")
        if on_spawn is not None:
            on_spawn(continuation_support.WORKER_PID)
        on_conversation = kwargs.get("on_conversation")
        if on_conversation is not None:
            on_conversation("conv-1")
        # The prompt goes out: this worker resumes what it was asked to resume
        # and is handed the turn. A real adapter reports this from the far side
        # of its handshake, which is why it is a separate hook from the spawn
        # above -- see the refused-restore worker in
        # ``test_asl_native_restore_recovery`` for one that never reaches it.
        on_prompt_submitted = kwargs.get("on_prompt_submitted")
        if on_prompt_submitted is not None:
            on_prompt_submitted()
        if len(self.harness_calls) == 1:
            # What the urgent endpoint's reader thread does when a producer's
            # correction lands: append it, then wake the turn. Admission itself
            # is exercised by its own module's tests and would refuse this run
            # for having no endpoint served in-process.
            self.submitted = redirect_store_requests.submit_request(
                self.plan_dir,
                target=redirect_store_models.RedirectTarget(
                    run_id=RUN_ID,
                    session_id=SESSION_ID,
                    turn_index=1,
                    harness=name,
                ),
                feedback=FEEDBACK,
                state_dir=self.state_dir,
                operation_id="op-1",
            )
            self.wakeup.set()
            wait_until(
                lambda: (
                    redirect_store_journal.read_journal(
                        self.plan_dir, state_dir=self.state_dir
                    )
                    .request("op-1")
                    .accepted
                ),
                timeout=10.0,
            )
            # What the real turn's owner reports once the process is reaped.
            abort.release()
            return harnesses.HarnessResult(
                name=name,
                argv=[name],
                returncode=137,
                stdout="",
                stderr="",
                aborted=True,
                conversation_id="conv-1",
            )
        self.commits_before_resume = len(self.commit_calls)
        self.status_at_resume = self._session_status()
        (self.repo_root / "worked.txt").write_text("done\n", encoding="utf-8")
        self._complete_session()
        abort.release()
        return harnesses.HarnessResult(
            name=name,
            argv=[name],
            returncode=0,
            stdout="codex completed\n",
            stderr="",
            conversation_id="conv-1",
        )

    def _receipt_from_prompt(
        self, prompt: str, *, harness: str, stale: redirect_store_models.RedirectTarget
    ) -> None:
        """Acknowledge as a real worker does: from the words it was handed.

        The prompt carries both halves of the receipt's identity, so parsing it
        is what proves the worker could acknowledge at all -- an attempt token
        the test supplied would prove only that the journal accepts one.

        ``stale`` is the attempt some earlier launch would have acknowledged
        from. Every recovery case has one, and it is tried first here so that
        each of them shows the same thing: the journal takes the receipt from
        the attempt that actually holds the words, and from no other.
        """
        match = re.search(r"--redirect-id (\S+) --attempt (\S+)", prompt)
        assert match is not None, prompt
        operation_id, token = match.group(1), match.group(2)
        run_id, session_id, turn_index = token.split("/")
        live = redirect_store_models.RedirectTarget(
            run_id=run_id,
            session_id=session_id,
            turn_index=int(turn_index),
            harness=harness,
        )
        try:
            redirect_ack.acknowledge_correction(
                self.plan_dir,
                operation_id,
                token=redirect_ack.attempt_token(stale),
                live_attempt=live,
                state_dir=self.state_dir,
            )
        except redirect_ack.RedirectAckError as error:
            self.stale_refusal = error
        self.receipt = redirect_ack.acknowledge_correction(
            self.plan_dir,
            operation_id,
            token=token,
            live_attempt=live,
            state_dir=self.state_dir,
        )

    def _fake_commit(self, **kwargs: Any) -> dict[str, Any]:
        """Commit for real, without shelling back out to ``asl commit``."""
        self.commit_calls.append(dict(kwargs))
        root = Path(str(kwargs["cwd"]))
        review_guard._run_git(root, ["add", "-A"])
        review_guard._run_git(
            root, ["commit", "--no-verify", "-m", f"session {kwargs['session_id']}"]
        )
        head = review_guard._run_git(root, ["rev-parse", "HEAD"]).stdout.strip()
        return {
            "commit_index": kwargs["commit_index"],
            "group_id": kwargs["group_id"],
            "repository_key": kwargs["repository_key"],
            "repository_alias": kwargs["repository_alias"],
            "repository_path": kwargs["repository_path"],
            "session_id": kwargs["session_id"],
            "success": True,
            "status_result": "success",
            "returncode": 0,
            "head_after": head,
            "clean_after": True,
            "commit_sha": head,
            "duration_seconds": 0.0,
        }

    # -- the run -------------------------------------------------------------

    def run_loop_once(
        self,
        *,
        run_id: str = RUN_ID,
        fresh_conversation: bool = False,
        prepare_plan: bool = True,
        expect_exit_code: int = 0,
    ) -> dict[str, Any]:
        """Spend one whole run against the fakes, as the real entry point would.

        The keyword arguments exist for the second run of a recovery case: it
        mints its own id the way a relaunch does, carries the one-use fresh
        conversation authorization, and must not rewrite the plan the first run
        already touched.
        """
        if prepare_plan:
            write_plan(self.plan_dir, ("pending",))
        listener = FakeRunControlListener(current_harness="codex")
        real_state_dir = state.plan_state_dir(self.plan_dir, self.state_dir)
        listener.state_path = real_state_dir / events.STATE_FILE_NAME
        listener.events_path = real_state_dir / events.EVENTS_FILE_NAME

        def fake_for_run(*_args: object, **kwargs: object):
            listener.on_action = kwargs.get("on_action")
            listener.on_status = kwargs.get("on_status")
            return listener

        args = argparse.Namespace(
            plan_dir=str(self.plan_dir),
            cwd=str(self.repo_root),
            harness="codex",
            review_harness=None,
            state_dir=str(self.state_dir),
            json=False,
            dry_run=False,
            unsafe_bypass=True,
            timeout=None,
            max_sessions=None,
            commit_mode=run_selection.COMMIT_MODE_PER_SESSION,
            harness_model=None,
            effort=None,
            note=None,
            allow_not_loop_ready=True,
            fresh_conversation=fresh_conversation,
        )
        with (
            patched_env(
                XDG_CONFIG_HOME=str(self.config_home),
                PYTHONPATH=str(REPO_ROOT)
                + os.pathsep
                + os.environ.get("PYTHONPATH", ""),
            ),
            mock.patch.object(harnesses, "run_harness", side_effect=self._fake_worker),
            mock.patch.object(run_commit, "_run_asl_commit", self._fake_commit),
            mock.patch.object(
                redirect_control, "UrgentController", self._controller_factory()
            ),
            mock.patch.object(
                run_control.RunControlListener, "for_run", side_effect=fake_for_run
            ),
        ):
            lock_info = state_locks.acquire_lock(self.plan_dir, self.state_dir)
            try:
                exit_code, payload = run_loop.run_locked(
                    args=args,
                    cwd=self.repo_root,
                    timeout=None,
                    max_sessions=None,
                    commit_mode=run_selection.COMMIT_MODE_PER_SESSION,
                    lock_info=lock_info,
                    run_id=run_id,
                    redirect_wakeup=self.wakeup,
                )
            finally:
                state_locks.release_lock(self.plan_dir, self.state_dir)
        self.assertEqual(expect_exit_code, exit_code, payload)
        return payload

    def events_named(self, name: str) -> list[dict[str, Any]]:
        path = (
            state.plan_state_dir(self.plan_dir, self.state_dir)
            / events.EVENTS_FILE_NAME
        )
        return [
            record["payload"] for record in read_jsonl(path) if record["name"] == name
        ]
