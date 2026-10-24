# ASL Autopilot: reporting the supervisor audit

On-demand reference for Step 4 of the `asl` skill (`../SKILL.md`).

When ASL audited its own supervision of the run, the helper attaches a
display-only `assurance` block to its result: `state` (`all-clear`, `findings`,
`unavailable`, `skipped`, or `pending`), `issue_ids` such as `ASI-42`,
`issue_count`, `new_issue_count`, `recurring_issue_count`, `partial`,
`status_line`, and `list_command`. Relay it and stop there:

- If `issue_ids` is present, say what the audit found and hand the user the
  `list_command` (`asl assurance list`) so they can read the inbox themselves.
- `partial: true` means an `unavailable` audit still reported what it managed
  to see, so report those findings as incomplete rather than as the whole
  picture.
- `all-clear`, `skipped`, and `pending` need no line of their own; announcing a
  clean audit every run is what teaches a user to skip the one line that
  matters.
- The audit is advisory. It never changed the run's outcome, so never restate a
  successful run as failed, or a pause reason as an audit finding, because an
  issue was opened.

This skill reports assurance and never drives it. Do not pass, add, or remove
`--supervisor-audit`, `--no-supervisor-audit`, `--audit-harness`,
`--audit-harness-model`, `--audit-effort`, or `--audit-timeout` — they belong to
the exact `asl run` command the user gave. Never run `asl assurance
process-pending`, never resolve, dismiss, accept-risk, merge, or close out an
issue, and never run `asl assurance plan`: dispositions and improvement drafts
are the user's decisions, and the helper has no field through which an audit
could be configured, launched, retried, or disposed of.
