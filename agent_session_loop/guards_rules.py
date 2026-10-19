# mypy: ignore-errors
# ruff: noqa
# ruff: noqa: F821
"""Guard parsing, matching, coverage, and Copilot projections."""

from . import guards_models as _models

globals().update(
    {name: value for name, value in vars(_models).items() if not name.startswith("__")}
)


def new_guard_id() -> str:
    """Mint an identifier short enough to type and wide enough not to collide."""
    return f"g-{uuid.uuid4().hex[:12]}"


def parse_command_rule(raw: str) -> tuple[str, ...]:
    """Turn one operator-written rule into the argv prefix it must match.

    POSIX tokenization is what makes ``--deny-command "git push"`` and
    ``--deny-command 'git push origin main'`` mean the obvious thing while
    quoting still works for an argument that contains a space. Everything a
    shell would *interpret* rather than pass through is refused outright: a
    rule is compared against literal argv, so a pattern or an operator here
    would be protection that silently never matches.
    """
    if not isinstance(raw, str) or not raw.strip():
        raise GuardRuleError(
            REJECT_EMPTY_COMMAND_RULE,
            "a command rule must contain at least one token",
        )
    try:
        tokens = shlex.split(raw, posix=True)
    except ValueError as error:
        raise GuardRuleError(
            REJECT_UNPARSABLE_COMMAND_RULE,
            f"command rule is not POSIX-tokenizable: {error}",
        ) from error
    if not tokens:
        raise GuardRuleError(
            REJECT_EMPTY_COMMAND_RULE,
            "a command rule must contain at least one token",
        )
    for token in tokens:
        if not token:
            raise GuardRuleError(
                REJECT_EMPTY_TOKEN,
                "a command rule may not contain an empty token",
            )
        if _SHELL_OPERATOR_CHARS & set(token):
            raise GuardRuleError(
                REJECT_SHELL_OPERATOR,
                f"command rule token {token!r} contains a shell operator; a "
                "rule is an argv prefix, not a shell command line",
            )
        if _GLOB_CHARS & set(token):
            raise GuardRuleError(
                REJECT_GLOB,
                f"command rule token {token!r} contains a glob; rules match "
                "literal arguments only",
            )
    executable = tokens[0]
    if "=" in executable:
        raise GuardRuleError(
            REJECT_ENVIRONMENT_ASSIGNMENT,
            f"command rule starts with the environment assignment "
            f"{executable!r}; name the executable the assignment precedes",
        )
    if (
        "/" in executable
        or os.sep in executable
        or (os.altsep is not None and os.altsep in executable)
        or executable in (os.curdir, os.pardir)
    ):
        raise GuardRuleError(
            REJECT_EXECUTABLE_NOT_BARE_NAME,
            f"command rule must start with a bare executable name, not "
            f"{executable!r}; the gate matches how PATH resolves a name and "
            "cannot see a path that bypasses it",
        )
    return tuple(tokens)


#: Pattern metacharacters this matcher implements. Anything else that a glob
#: dialect might mean — character classes, brace expansion, negation — is
#: refused, because a rule that quietly matched nothing would read as
#: protection right up until it failed to fire.
_PATH_WILDCARD_CHARS = frozenset("*?")
_UNSUPPORTED_PATTERN_CHARS = frozenset("[]{}!\0")


@functools.lru_cache(maxsize=512)
def _compile_path_pattern(pattern: str) -> re.Pattern[str]:
    """Compile a validated path pattern into a whole-string, case-sensitive regex.

    ``**`` is a whole segment and spans zero or more segments; ``*`` and ``?``
    never cross a separator. Matching is case-sensitive even on a
    case-insensitive filesystem, because the qualified paths this is compared
    against come from Git, which is case-sensitive about what it records.
    """
    segments = pattern.split("/")
    parts: list[str] = []
    for index, segment in enumerate(segments):
        last = index == len(segments) - 1
        if segment == "**":
            # Trailing ``**`` must still require something below it: ``a/**``
            # protects the contents of ``a``, and ``a`` itself is the literal
            # rule ``a``.
            parts.append("[^/]+(?:/[^/]+)*" if last else "(?:[^/]+/)*")
            continue
        for character in segment:
            if character == "*":
                parts.append("[^/]*")
            elif character == "?":
                parts.append("[^/]")
            else:
                parts.append(re.escape(character))
        if not last:
            parts.append("/")
    return re.compile("".join(parts) + r"\Z")


def parse_protected_path_rule(
    raw: str,
    *,
    aliases: Sequence[str] | None = None,
) -> ProtectedPath:
    """Turn one operator-written ``Alias:glob`` rule into a path matcher.

    The qualifier is mandatory and is validated against the plan's own
    vocabulary: a registered plan's declared aliases, or ``.`` alone for a
    legacy plan. Guessing a default repository would be the one failure mode
    with no recovery — the operator would be told the guard is armed while it
    watched a repository they did not name.

    ``aliases`` of ``None`` means "no plan context available", which validates
    the syntax and defers the alias check to the caller that has one.
    """
    if not isinstance(raw, str) or not raw.strip():
        raise GuardRuleError(
            REJECT_EMPTY_PATH_RULE,
            "a protected-path rule must name a repository and a path",
        )
    candidate = raw.strip()
    if "\\" in candidate:
        raise GuardRuleError(
            REJECT_PATH_BACKSLASH,
            f"protected-path rule {candidate!r} contains a backslash; paths are "
            "compared as POSIX paths, so `/` is the only separator",
        )
    qualifier, separator, pattern = candidate.partition(":")
    if not separator:
        raise GuardRuleError(
            REJECT_PATH_MISSING_QUALIFIER,
            f"protected-path rule {candidate!r} names no repository; write "
            "`Alias:path` (or `.:path` for a single-repository plan)",
        )
    if not qualifier or not pattern:
        raise GuardRuleError(
            REJECT_EMPTY_PATH_RULE,
            f"protected-path rule {candidate!r} has an empty repository or path",
        )
    if pattern.startswith("/"):
        raise GuardRuleError(
            REJECT_PATH_ABSOLUTE,
            f"protected-path rule {candidate!r} is absolute; a path is relative "
            "to the repository its qualifier names, so an absolute path could "
            "never match the evidence the guard compares against",
        )
    if _UNSUPPORTED_PATTERN_CHARS & set(pattern):
        raise GuardRuleError(
            REJECT_PATH_PATTERN,
            f"protected-path rule {candidate!r} uses pattern syntax this "
            "matcher does not implement; only literals, `*`, `**`, and `?` are "
            "supported",
        )
    segments = pattern.split("/")
    if segments[0] == ".":
        # A leading `./` is how people type "in this repository"; it is the one
        # `.` segment that carries an intention rather than a mistake.
        segments = segments[1:]
    for segment in segments:
        if not segment:
            raise GuardRuleError(
                REJECT_PATH_EMPTY_SEGMENT,
                f"protected-path rule {candidate!r} has an empty path segment; "
                "a trailing or doubled `/` is almost never what was meant",
            )
        if segment == os.pardir:
            raise GuardRuleError(
                REJECT_PATH_TRAVERSAL,
                f"protected-path rule {candidate!r} escapes its repository with "
                "`..`; name the repository that owns the path instead",
            )
        if segment == os.curdir:
            raise GuardRuleError(
                REJECT_PATH_PATTERN,
                f"protected-path rule {candidate!r} contains a `.` segment; "
                "write the path as it appears in the repository",
            )
        if "**" in segment and segment != "**":
            raise GuardRuleError(
                REJECT_PATH_PATTERN,
                f"protected-path rule {candidate!r} mixes `**` with other "
                "characters in one segment; `**` spans whole segments, so it "
                "must stand alone between separators",
            )
    if not segments:
        raise GuardRuleError(
            REJECT_EMPTY_PATH_RULE,
            f"protected-path rule {candidate!r} names no path inside the repository",
        )
    normalized = "/".join(segments)
    if aliases is not None and qualifier not in tuple(aliases):
        known = ", ".join(sorted(aliases)) or "none"
        raise GuardRuleError(
            REJECT_PATH_UNKNOWN_ALIAS,
            f"protected-path rule {candidate!r} names repository {qualifier!r}, "
            f"which this plan does not declare; known qualifiers: {known}",
        )
    return ProtectedPath(qualifier=qualifier, pattern=normalized)


def match_protected_paths(
    guards: Sequence[GuardRule],
    qualified_paths: Iterable[str],
) -> OrderedDict[str, tuple[str, ...]]:
    """Map each violated guard id to the qualified paths that violated it.

    Guards with no match are absent rather than present-and-empty, so a caller
    can treat a non-empty result as "the turn is convicted" without filtering.
    """
    observed = tuple(dict.fromkeys(qualified_paths))
    matched: OrderedDict[str, tuple[str, ...]] = OrderedDict()
    for guard in guards:
        if guard.path is None:
            continue
        hits = tuple(
            qualified
            for qualified in observed
            if guard.path.matches_qualified(qualified)
        )
        if hits:
            matched[guard.id] = hits
    return matched


def guard_coverage(
    guard: GuardRule,
    *,
    harness: str | None,
    gate_armed: bool = True,
    overlay_armed: bool = True,
    pretool_armed: bool = False,
    process_start: bool = False,
) -> dict[str, dict[str, str]]:
    """Say, per enforcement lane, how much of ``guard`` is actually armed.

    The three lanes fail independently and an operator's next move differs by
    which one is blind, so each carries its own verdict and its own stable
    reason rather than being folded into one number.

    ``pretool_armed`` reports whether the harness adapter installed this run's
    hook overlay; ``process_start`` reports whether the guard already existed
    when the worker process was spawned, which is the only thing a fixed
    command-line deny rule can ever cover.

    ``overlay_armed`` reports whether the turn in flight actually received the
    overlay: :meth:`GuardController.prepare_turn` fails open, so a worker can be
    running with an untouched ``PATH`` and no policy path in its environment
    while the guard is still perfectly present in the active set. That is the
    one case where both pre-execution lanes are blind for a reason no rule and
    no harness explains, so it is reported rather than inferred.
    """
    lanes: dict[str, dict[str, str]] = {}
    if guard.kind == GUARD_KIND_COMMAND:
        if not gate_armed:
            lanes[LANE_COMMAND_GATE] = _lane(
                COVERAGE_UNSUPPORTED, COVERAGE_REASON_SHIM_UNAVAILABLE
            )
        elif not overlay_armed:
            # The shim may well exist on disk; what is missing is the ``PATH``
            # entry that would make the worker find it, so the gate is not a
            # narrower enforcement but no enforcement at all.
            lanes[LANE_COMMAND_GATE] = _lane(
                COVERAGE_UNSUPPORTED, COVERAGE_REASON_OVERLAY_UNARMED
            )
        else:
            lanes[LANE_COMMAND_GATE] = _lane(
                COVERAGE_ACTIVE, COVERAGE_REASON_SHIM_ARMED
            )
        lanes[LANE_POSTFLIGHT] = _lane(
            COVERAGE_UNSUPPORTED, COVERAGE_REASON_NOT_OBSERVABLE
        )
    else:
        lanes[LANE_COMMAND_GATE] = _lane(
            COVERAGE_UNSUPPORTED, COVERAGE_REASON_NOT_A_COMMAND_RULE
        )
        lanes[LANE_POSTFLIGHT] = _lane(
            COVERAGE_ACTIVE, COVERAGE_REASON_SNAPSHOT_COMPARISON
        )
    pretool = _pretool_lane(
        guard,
        harness=harness,
        pretool_armed=pretool_armed,
        process_start=process_start,
    )
    if not overlay_armed and pretool["status"] != COVERAGE_UNSUPPORTED:
        # Every pretool lane reads the policy through the turn environment: the
        # Claude hook resolves `ASL_GUARD_POLICY`, and Copilot compiles
        # `--deny-tool` from the same document. Without the injection there is
        # nothing for either to read, so a lane that would otherwise be armed
        # is downgraded. A lane that is already unsupported keeps its own
        # reason, which is the more specific and more durable of the two.
        pretool = _lane(COVERAGE_UNSUPPORTED, COVERAGE_REASON_OVERLAY_UNARMED)
    lanes[LANE_PRETOOL] = pretool
    return lanes


def _lane(status: str, reason: str) -> dict[str, str]:
    return {"status": status, "reason": reason}


def _pretool_lane(
    guard: GuardRule,
    *,
    harness: str | None,
    pretool_armed: bool,
    process_start: bool,
) -> dict[str, str]:
    """The pre-execution verdict, which is the lane harnesses differ on.

    Claude accepts a per-turn hook overlay, so both rule kinds are refused
    before the tool call runs. Codex reads hooks only from a trusted
    ``CODEX_HOME`` file, and arming that trust means writing a hash into the
    operator's own configuration, which ASL will not do. Copilot has no hook at
    all: its ``--deny-tool`` rules are fixed when the process is spawned, so
    they cover a command rule that already existed and nothing else.
    """
    if harness == "claude":
        if not pretool_armed:
            return _lane(COVERAGE_UNSUPPORTED, COVERAGE_REASON_HOOK_UNAVAILABLE)
        return _lane(COVERAGE_ACTIVE, COVERAGE_REASON_HOOK_DENIES)
    if harness == "codex":
        return _lane(COVERAGE_UNSUPPORTED, COVERAGE_REASON_CODEX_HOOK_TRUST)
    if harness == "copilot":
        if guard.kind != GUARD_KIND_COMMAND:
            return _lane(COVERAGE_UNSUPPORTED, COVERAGE_REASON_COPILOT_NO_PATH_RULE)
        if copilot_deny_tool_rule(guard.command) is None:
            return _lane(COVERAGE_UNSUPPORTED, COVERAGE_REASON_COPILOT_UNEXPRESSIBLE)
        if not process_start:
            return _lane(
                COVERAGE_UNSUPPORTED, COVERAGE_REASON_ADDED_AFTER_PROCESS_START
            )
        return _lane(COVERAGE_DEGRADED, COVERAGE_REASON_COPILOT_PROCESS_START)
    return _lane(COVERAGE_UNSUPPORTED, COVERAGE_REASON_HOOK_UNAVAILABLE)


def copilot_deny_tool_rule(command: Sequence[str]) -> str | None:
    """The ``--deny-tool`` value for one command rule, or ``None`` if there is none.

    ``--deny-tool='shell(git push)'`` matches on the command *text*, so a rule
    is only expressible when writing its tokens back out with single spaces
    reproduces the invocation it was parsed from. A token carrying whitespace
    or a parenthesis does not survive that round trip -- it would either split
    into two tokens or close the ``shell(...)`` wrapper early -- so it compiles
    to nothing here and its ``pretool`` lane says so rather than shipping a
    rule that quietly matches something else.
    """
    tokens = [str(token) for token in command]
    if not tokens:
        return None
    if any(not token or set(token) & _COPILOT_UNEXPRESSIBLE for token in tokens):
        return None
    return f"shell({' '.join(tokens)})"


#: Characters that make a command rule impossible to state as a ``shell(...)``
#: deny value: whitespace is the token separator on the other side, and the
#: parentheses delimit the wrapper itself.
_COPILOT_UNEXPRESSIBLE = frozenset(" \t\n()")


def copilot_deny_tool_rules(guards: Sequence[GuardRule]) -> tuple[str, ...]:
    """Compile the guards Copilot's ``--deny-tool`` can express, in order.

    Only command rules compile: the CLI has no path-aware equivalent, so a
    protected-path guard contributes nothing here and is reported as
    postflight-only rather than silently dropped.
    """
    return _dedupe(copilot_deny_tool_rule(guard.command) for guard in guards)


def copilot_deny_tool_rules_from_policy(
    policy: Mapping[str, Any] | None,
) -> tuple[str, ...]:
    """The same compilation, driven by a policy document instead of objects.

    Argv is built from the derived policy the turn environment points at, not
    from the live controller: the two are the same set by construction, and
    reading the file is what makes "guards present at process start" a fact
    about the document the worker's other lanes also read. Anything malformed
    is skipped rather than raised -- a policy this side cannot parse must
    degrade to the lanes that do not depend on argv, never fail the turn.
    """
    if not policy:
        return ()
    entries = policy.get("guards")
    if not isinstance(entries, Sequence) or isinstance(entries, (str, bytes)):
        return ()
    commands: list[Sequence[str]] = []
    for entry in entries:
        if not isinstance(entry, Mapping):
            continue
        command = entry.get("command")
        if isinstance(command, Sequence) and not isinstance(command, (str, bytes)):
            commands.append(command)
    return _dedupe(copilot_deny_tool_rule(command) for command in commands)


def _dedupe(values: Iterable[str | None]) -> tuple[str, ...]:
    """First-wins deduplication that drops the ``None``s, preserving order."""
    seen: list[str] = []
    for value in values:
        if value is not None and value not in seen:
            seen.append(value)
    return tuple(seen)
