# mypy: ignore-errors
# ruff: noqa
# ruff: noqa: F821
"""Thread-safe active guard controller for one run."""

from . import guards_models as _models
from . import guards_policy as _policy
from . import guards_rules as _rules

for _module in (_models, _rules, _policy):
    globals().update(
        {
            name: value
            for name, value in vars(_module).items()
            if not name.startswith("__")
        }
    )


def legacy_workspace_aliases(cwd: str | Path) -> dict[str, str]:
    """Bind the legacy ``.`` qualifier to the repository root holding ``cwd``.

    The postflight comparison names a legacy turn's paths relative to the Git
    top level, whatever subdirectory the run was started in, so the pre-tool
    hook has to qualify a write against that same root. Binding ``.`` to the
    working directory instead let the hook pass a write the postflight then
    convicted, and refuse one the postflight would never have. A ``cwd``
    outside any repository keeps itself as the root.
    """
    from . import git_subprocess

    root = str(cwd)
    try:
        completed = git_subprocess.run_git(root, ["rev-parse", "--show-toplevel"])
    except OSError, ValueError:
        return {LEGACY_PATH_QUALIFIER: root}
    toplevel = completed.stdout.strip() if completed.returncode == 0 else ""
    return {LEGACY_PATH_QUALIFIER: toplevel or root}


def _write_policy(*args: Any, **kwargs: Any) -> None:
    """Dispatch through the public seam so callers can replace the policy writer."""
    from . import guards

    guards.write_policy(*args, **kwargs)


class GuardController:
    """The active guard set for one run, and the only writer of its files.

    Every mutation is serialized on one lock because the two callers run on
    different threads: the mailbox/control poll thread drains queued guard
    commands mid-turn, while the run loop prepares the overlay between turns.

    The activation order is the contract, and it is why a failure cannot leave
    a half-armed guard: the executable is resolved, the policy is written, the
    shim is installed, and only then does the rule join the active set the
    caller is told about. A failure at any step raises before the set changes,
    and the policy is rewritten from the unchanged set on the way out.
    """

    def __init__(
        self,
        *,
        run_id: str,
        run_dir: str | Path,
        path_value: str | None = None,
        workspace_aliases: Mapping[str, str] | None = None,
    ) -> None:
        self.run_id = run_id
        self.run_dir = Path(run_dir)
        self._path_value = path_value
        self._lock = threading.Lock()
        self._guards: dict[str, GuardRule] = {}
        self._executables: dict[str, str] = {}
        self._workspace_aliases: dict[str, str] = dict(workspace_aliases or {})
        self._external_read_only_aliases: tuple[str, ...] = ()
        self._expired = False
        # Coverage context for the turn now in flight. It is per-turn rather
        # than per-run because the harness can be switched mid-run, and a guard
        # that was pre-tool-enforced under Claude is not enforced the same way
        # after `control select-harness` moves the run to Copilot.
        self._harness: str | None = None
        self._pretool_armed = False
        self._process_start_ids: frozenset[str] = frozenset()
        # Tri-state, and the third state is the point: ``None`` means no turn
        # is in flight, ``True`` means one is and its baseline was captured,
        # ``False`` means one is and it was not. Only the last refuses a path
        # guard, because it is the only case where arming one would promise a
        # comparison that can never be made.
        self._turn_snapshot_available: bool | None = None
        # Whether the last `prepare_turn` both materialized the overlay and
        # injected it into the environment it handed back. It starts True
        # because a controller that has prepared no turn yet has told nobody
        # anything, and it is rewritten by every `prepare_turn` — so a failure
        # is reported for exactly as long as it is the truth: until the next
        # turn is prepared successfully.
        self._overlay_armed = True

    # -- read-only projections ---------------------------------------------

    @property
    def overlay_armed(self) -> bool:
        """Whether the environment the last prepared turn received is guarded."""
        with self._lock:
            return self._overlay_armed

    @property
    def guard_bin(self) -> Path:
        return guard_bin_dir(self.run_dir)

    @property
    def policy_file(self) -> Path:
        return policy_path(self.run_dir)

    def active(self) -> tuple[GuardRule, ...]:
        with self._lock:
            return tuple(self._guards.values())

    def active_paths(self) -> tuple[GuardRule, ...]:
        """The protected-path subset, which is what finalization enforces."""
        return tuple(
            guard for guard in self.active() if guard.kind == GUARD_KIND_PROTECTED_PATH
        )

    def workspace_aliases(self) -> dict[str, str]:
        with self._lock:
            return dict(self._workspace_aliases)

    def mark_turn_snapshot(self, available: bool | None) -> None:
        """Record whether the turn in flight has a baseline to compare against.

        Called with a boolean when a turn starts and with ``None`` when it
        ends. A path guard added between turns is always accepted: the next
        turn captures its own baseline, so there is nothing missing yet.
        """
        with self._lock:
            self._turn_snapshot_available = available

    def set_workspace_aliases(self, aliases: Mapping[str, str]) -> None:
        """Bind alias names to absolute roots for the pre-tool hook.

        Resolved once the run knows its workspace, which is after the
        controller exists: the controller is created with the run directory, and
        the repository registry is derived later. Rewriting the policy here
        keeps the hook from having to re-derive a binding the runner already
        made.
        """
        with self._lock:
            self._workspace_aliases = dict(aliases)
            if self._policy_needed_locked():
                self._restore_locked()

    def set_external_read_only_aliases(self, aliases: Iterable[str]) -> None:
        """Name the externally changed aliases the next turn may not write.

        Set per turn because it depends on the session's write scope. It keeps
        the policy on disk even with no operator guard active, since it is the
        only pre-execution refusal those aliases get.
        """
        with self._lock:
            self._external_read_only_aliases = tuple(sorted(set(aliases)))
            if self._policy_needed_locked():
                self._restore_locked()

    def to_payload(self) -> list[dict[str, Any]]:
        """The active summaries carried by runner state and read by status.

        Each entry carries its own per-lane coverage, because two guards on the
        same run can be enforced differently: one added before the worker
        process started can reach a fixed command-line deny rule that one added
        mid-turn never will.
        """
        with self._lock:
            harness = self._harness
            pretool_armed = self._pretool_armed
            process_start_ids = self._process_start_ids
            gate_armed = not self._expired
            overlay_armed = self._overlay_armed
            active = tuple(self._guards.values())
        return [
            {
                **guard.to_payload(),
                "coverage": guard_coverage(
                    guard,
                    harness=harness,
                    gate_armed=gate_armed,
                    overlay_armed=overlay_armed,
                    pretool_armed=pretool_armed,
                    process_start=guard.id in process_start_ids,
                ),
            }
            for guard in active
        ]

    # -- mutations ---------------------------------------------------------

    def add(
        self,
        rules: Iterable[Sequence[str]] = (),
        *,
        paths: Iterable[ProtectedPath] = (),
        reason: str | None = None,
        created_at: str | None = None,
    ) -> GuardOutcome:
        """Activate one or more rules of either kind, all-or-nothing.

        A queued command carrying several ``--deny-command`` and
        ``--protect-path`` rules is one operator intention, so a partial
        activation would be the worst outcome: the operator would be told some
        subset is armed without being told which. Every rule is staged, and the
        set only changes once every shim is installed.

        A path rule needs no shim and cannot fail to resolve, so mixing the
        kinds costs nothing: the all-or-nothing boundary is the shim
        installation, and a path rule simply joins the set alongside it.
        """
        staged = [tuple(rule) for rule in rules]
        staged_paths = list(paths)
        if not staged and not staged_paths:
            return GuardOutcome(
                action=ACTION_REJECTED,
                reason=REJECT_EMPTY_GUARD_RULE,
                detail="guard add requires at least one command or path rule",
            )
        stamp = created_at or clock.utc_iso_now()
        with self._lock:
            if self._expired:
                return GuardOutcome(
                    action=ACTION_REJECTED,
                    reason=REJECT_NO_ACTIVE_RUN,
                    detail="the run this guard names has already terminated",
                )
            if staged_paths and self._turn_snapshot_available is False:
                return GuardOutcome(
                    action=ACTION_REJECTED,
                    reason=REJECT_PROTECTED_PATH_EVIDENCE,
                    detail=(
                        "the turn in flight has no captured baseline, so a "
                        "protected-path guard could never be compared against "
                        "one; re-add it once this turn ends"
                    ),
                )
            resolved: dict[str, str] = {}
            for command in staged:
                name = command[0]
                if name in self._executables:
                    continue
                target = resolve_original(
                    name,
                    guard_bin=self.guard_bin,
                    path_value=self._path_value,
                )
                if target is None:
                    return GuardOutcome(
                        action=ACTION_REJECTED,
                        reason=REJECT_EXECUTABLE_NOT_FOUND,
                        detail=(
                            f"{name!r} does not resolve on PATH, so a shim for "
                            "it could shadow nothing and would break the "
                            "worker's own resolution"
                        ),
                    )
                resolved[name] = target

            new_rules = [
                GuardRule(
                    id=new_guard_id(),
                    command=command,
                    reason=reason,
                    created_at=stamp,
                )
                for command in staged
            ] + [
                GuardRule(
                    id=new_guard_id(),
                    path=protected,
                    reason=reason,
                    created_at=stamp,
                )
                for protected in staged_paths
            ]
            merged = dict(self._guards)
            for rule in new_rules:
                merged[rule.id] = rule
            executables = {**self._executables, **resolved}
            try:
                self._materialize_locked(merged, executables)
            except OSError as error:
                # The active set has not changed, but the policy on disk may
                # have; restore it from the set that is still authoritative so
                # the file and the state cannot disagree.
                self._restore_locked()
                return GuardOutcome(
                    action=ACTION_REJECTED,
                    reason=REJECT_ACTIVATION_FAILED,
                    detail=f"could not arm the command gate: {error}",
                )
            self._guards = merged
            self._executables = executables
            return GuardOutcome(
                action=ACTION_APPLIED,
                guard_ids=tuple(rule.id for rule in new_rules),
                guards=tuple(new_rules),
            )

    def remove(self, guard_id: str) -> GuardOutcome:
        """Deactivate one guard, updating the policy before reporting success.

        The shim is deliberately left in place: it delegates once its rule is
        gone, and deleting it would race a worker that is resolving the name
        right now.
        """
        with self._lock:
            if self._expired:
                return GuardOutcome(
                    action=ACTION_REJECTED,
                    reason=REJECT_NO_ACTIVE_RUN,
                    detail="the run this guard names has already terminated",
                )
            existing = self._guards.get(guard_id)
            if existing is None:
                return GuardOutcome(
                    action=ACTION_REJECTED,
                    reason=REJECT_UNKNOWN_GUARD,
                    detail=f"no active guard {guard_id!r} on this run",
                )
            remaining = {
                identifier: rule
                for identifier, rule in self._guards.items()
                if identifier != guard_id
            }
            try:
                self._materialize_locked(remaining, self._executables)
            except OSError as error:
                self._restore_locked()
                return GuardOutcome(
                    action=ACTION_REJECTED,
                    reason=REJECT_ACTIVATION_FAILED,
                    detail=f"could not update the command gate: {error}",
                )
            self._guards = remaining
            return GuardOutcome(
                action=ACTION_REMOVED,
                guard_ids=(guard_id,),
                guards=(existing,),
            )

    def prepare_turn(
        self,
        env: Mapping[str, str] | None = None,
        *,
        harness: str | None = None,
        pretool_armed: bool = False,
    ) -> dict[str, str]:
        """Arm the overlay for the turn about to start and return its environment.

        Called before every worker turn. The directory is created even when no
        guard is active, so a guard added mid-turn installs into a ``PATH``
        entry the running process already has; the policy only exists while
        something is denied, which is what makes an inherited shim from an
        earlier turn inert rather than stale.

        The returned mapping is the parent environment with ``PATH`` rewritten
        and ``ASL_GUARD_POLICY`` added, so a pre-tool hook reads the same
        derived policy the shims do instead of re-deriving it from the run
        directory it would otherwise have to guess.

        The guard set is snapshotted here as this turn's "existed at process
        start" cohort: it is the only cohort a fixed command-line deny rule can
        ever cover, and coverage that claimed otherwise for a mid-turn addition
        would be the exact lie an operator would act on.

        Whether the overlay was actually armed is recorded before the
        environment is returned, because this method fails open: a turn that
        could not be guarded still runs, and the only thing that keeps that
        from becoming a false claim of enforcement is that coverage reads the
        same verdict this call wrote.
        """
        base = dict(os.environ if env is None else env)
        with self._lock:
            self._harness = harness
            self._pretool_armed = pretool_armed
            self._process_start_ids = frozenset(self._guards)
            guard_bin = self.guard_bin
            try:
                guard_bin.mkdir(parents=True, exist_ok=True)
                if self._policy_needed_locked():
                    self._materialize_locked(self._guards, self._executables)
                else:
                    self.policy_file.unlink(missing_ok=True)
                overlay_path = child_path(base.get("PATH"), guard_bin)
                policy_file = str(self.policy_file)
            except OSError:
                # An overlay that cannot be armed must not cost the turn. The
                # guards stay in the active set and the next turn tries again;
                # what is lost is enforcement, and every lane that depended on
                # this overlay now says so instead of reporting a gate the
                # worker's environment does not contain.
                self._overlay_armed = False
                return base
            self._overlay_armed = True
        base["PATH"] = overlay_path
        base[GUARD_POLICY_ENV_VAR] = policy_file
        return base

    def expire(self) -> tuple[GuardRule, ...]:
        """Terminate every guard with the run, and report what was active.

        Deleting the policy is the whole mechanism: the shims left in
        ``guard-bin`` become delegators, and no later worker inherits a
        ``PATH`` that contains this run's directory anyway. They are retained
        as inert evidence of what the run enforced.
        """
        with self._lock:
            expiring = tuple(self._guards.values())
            self._guards = {}
            self._expired = True
            try:
                self.policy_file.unlink(missing_ok=True)
            except OSError as error:
                if error.errno not in (errno.ENOENT, errno.ENOTDIR):
                    raise
            return expiring

    # -- internals ---------------------------------------------------------

    def _policy_needed_locked(self) -> bool:
        return not self._expired and bool(
            self._guards or self._external_read_only_aliases
        )

    def _materialize_locked(
        self,
        guards: Mapping[str, GuardRule],
        executables: Mapping[str, str],
    ) -> None:
        """Write the policy, then install every shim it needs. Caller holds the lock.

        Policy first: a shim that exists before its rule does would deny
        nothing, while a rule that exists before its shim is simply not yet
        reachable. Neither ordering can deny something the operator did not
        ask for, and this one can never leave a shim shadowing a binary for a
        run whose policy write failed.
        """
        _write_policy(
            self.policy_file,
            policy_document(
                run_id=self.run_id,
                guard_bin=self.guard_bin,
                guards=list(guards.values()),
                executables=executables,
                workspace_aliases=self._workspace_aliases,
                external_read_only_aliases=self._external_read_only_aliases,
            ),
        )
        for name in sorted(
            {rule.executable for rule in guards.values() if rule.executable is not None}
        ):
            install_shim(
                guard_bin=self.guard_bin,
                name=name,
                policy_file=self.policy_file,
            )

    def _restore_locked(self) -> None:
        """Best-effort return of the policy file to the authoritative set."""
        with contextlib.suppress(OSError):
            self._materialize_locked(self._guards, self._executables)
