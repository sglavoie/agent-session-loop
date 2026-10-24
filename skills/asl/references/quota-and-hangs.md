# ASL Autopilot: quota resets and hung workers

On-demand reference for the `asl` skill (`../SKILL.md`): sleeping through a
provider quota reset, and handling a `reason: worker_hung` stop.

## Optional: sleep through a quota reset

A `quota-exhausted` pause is fail-closed by default — the helper stops
`user_required` and waits for a human. Pass `--wait-quota-reset` to the
**helper** (not inside the `--command` string) to let it sleep to the
provider's reset instead:

```text
<skill-dir>/scripts/asl-autopilot.py --command '<exact asl run command>' --supervision-id <launch-identity> --json --detach --wait-quota-reset
```

It waits only when every guard passes: the pause is `quota-exhausted`, the
runner parsed a reset hint (`retry_at` — a `Retry-After` second count, an ISO
timestamp, `N hours` / `N minutes`, or a time of day with an optional
`(Area/City)` zone; one naming an instant off the end of the calendar counts as
unparsed), the resulting wait fits an eight-day ceiling, and fewer than eight
resets have already been slept through in this supervision. It then
sleeps to that reset plus a five-minute buffer — in short slices measured
against the wall clock, so a machine suspend is noticed rather than slept out —
and relaunches the same plan through the ordinary chunk machinery. A reset
already in the past waits just the buffer, and each attempt spends one of the
eight resumes, so a provider that keeps rejecting converges to `user_required`
rather than looping. `auth-failed` and `invalid-model` never wait. Any declined
guard keeps today's `user_required` stop and names the guard under
`quota_wait.skipped`, so a run that could have waited always says why it did
not.

## reason: worker_hung

Two hard hang bars sit above the soft `WARN possibly stalled` line: the
runner's `result_received_process_alive` suspicion observed on consecutive
probes, or a probe whose idle time reaches the idle ceiling (classed
`zero_cpu_idle` when the runner's suspicion says so, else
`idle_ceiling_exceeded`). Both rest on the runner's process-tree CPU sampling,
accumulated per process identity so a descendant exiting between two samples is
not read as the tree going idle:
it raises neither suspicion class while the tree is burning CPU, and the idle
ceiling stands down whenever the live `hang_cpu_progress` sample reports
`progressing: true` — a worker piping through a buffering consumer is silent,
not stalled. An unknown sample (no fresh `ps`) still escalates.
When either bar fires, the helper stops with `reason: worker_hung`
without killing or waiting on the `asl run` child. Before the helper can reach
that unresolved stop, the runner gives only `result_received_process_alive` one
automatic recovery attempt: it re-samples the tree, requires the harness root
and each deepest leaf to retain the same pid, birth time, command, and ancestry,
then sends SIGTERM once to the verified leaves. It never escalates to SIGKILL,
never targets parent shells, and never does this for generic `zero_cpu_idle`, a
progressing or failed sample, a root-only tree, or when process sweeping is
disabled. Resumed output clears the episode and the same runner continues.
`worker_hung` means that recovery was inapplicable or did not resume the turn;
the supervised child stays alive and keeps the plan lock. Its terminal
`supervision-end` record therefore marks the end of supervision only: that live
child can still publish mailbox questions and notices after the record lands.

Read `result.hang` first. It carries the `class`, the runner's `hang_suspected`
block verbatim (including its sampled `process_tree` and additive `recovery`
report), `idle_seconds`, `probes`, the plan/state/events paths, and the active
turn's artifact paths. The recovery report names its policy, attempt time,
signal, candidate/signaled/already-gone pids, identity mismatches, and signal
failures. Automatically recovered episodes have a later `turn_hang_cleared`
event and never reach this stop; a report inside `worker_hung` is an unresolved
attempt, not permission for a stronger signal. `supervised_child_pid` and
`detached` appear only when the helper launched the chunk itself. Then:

1. **Re-verify before trusting the snapshot** — pids get reused, so match on
   pid, birth time, command, and ancestry: compare
   `ps -axo pid,ppid,state,time,lstart,command` against
   `hang.hang_suspected.process_tree`.
2. **Ask the user before any further signal.** The automatic boundary was
   SIGTERM-only; unresolved cases require explicit approval.
3. If approved, kill **only the identified leaf** — the deepest zero-CPU
   descendant (e.g. a `perl .../diff-so-fancy` pager wedged under the worker's
   `/bin/zsh -c`) — never the runner or the `asl run` process.
4. Re-run the same helper command to re-attach and continue supervision.
