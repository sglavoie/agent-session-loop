# ASL Autopilot: review findings

On-demand reference for Step 3 of the `asl` skill (`../SKILL.md`).

When the supervised command used `--review-harness`, a clean completion still
carries the review verdict. An all-clear review returns `reason: complete` with a
`review` block whose `outcome` is `all-clear`; nothing more is needed. When the
review reports findings, the helper returns `reason: review_findings` with a
`review` block holding `review_output_path` (the `final-review.md` to read in
full), `status_line`, `total_findings`, `findings_by_severity`, and a bounded
`summary`.

A run whose review cycle ends with findings still open exits `ok: false` with
`reason: review-findings-unresolved` rather than `all-complete`; the helper
re-tags it `review_findings` instead of treating it as a blocked run, so the
severity counts survive.

`review_findings` means no automatic continuation remains for the command's
current cap and artifact state. Do **not** draft, split, or expand a
follow-up fix plan. Instead:

1. Read the full `final-review.md` at `review_output_path`, then run
   `asl review-verdict <plan-dir> --json` for the authoritative verdict and relay
   its `next_steps` remediation commands.
2. Hand the remaining findings to the user with the status line and the run's
   recorded cycle disposition (`review_fix_disposition`). When the payload's
   `review_resume` block reports `higher-cap-required` or `artifact-invalid`,
   include its minimum cap or artifact-remediation detail so the user can resume
   the same plan. When it reports `repair-group-exhausted` or
   `cycle-cap-exhausted`, do **not** relaunch with a raised cap; report the
   recurring `repair_group` (when present) and hand control back to the
   operator for a manual fix or resplit decision.

When reporting a stalled or exhausted run, classify the evidence before proposing another attempt: implementation work, verification failure, repair churn, provider/guard failure, or pending human acceptance. State what new evidence would make a retry meaningful. Slow progress alone does not justify resplitting or raising a cap; unanswered questions and the existing retry limits remain in force.

Registered review is one combined read-only review across the ordered registry.
Every finding, evidence path, and completion check is repository-attributed;
repair children read the full registry but write only finding-derived aliases,
and every review or repair turn passes the same workspace mutation guard. Keep
aliases on findings and report qualified paths—never collapse the review to one
repository or treat a mutation from the read-only review harness as a finding.
