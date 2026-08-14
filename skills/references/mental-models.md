# Mental Models Reference

Shared vocabulary for the planning and supervision skills in this directory tree. Naming a model gives the reviewer, the auditor, and the planner one phrase for one recurring defect, so the same problem is described the same way twice instead of re-derived.

**How to use this file**: consuming skills read it on demand — the same relative-read pattern `../plan-iterate/SKILL.md` is read with in `plan-ideal/SKILL.md:29` — and cite **at most 3 models by name** per report. Do not inline this catalogue into a `SKILL.md`; the citation is the payload, the catalogue is the reference.

Each entry is the model name, the question it asks, and (first section) where the tooling already enforces it.

## Models this tooling already enforces

Cite these to name a behavior that is already mechanical. If a report cites one of these, the enforcement point is the evidence.

- **Fail-closed / margin of safety** — *What happens if this input is malformed or unknown?* Every parser has explicit caps and rejects unknown schema versions, unknown keys, duplicate lanes, and undeclared evidence references rather than guessing (`agent_session_loop/assurance_models.py:27-31`; `agent_session_loop/prepare.py:118-131` rejects unknown `claim_type`/`issue_type`).
- **Ratchet** — *Can this record be quietly walked back?* The assurance ledger is append-only and canonical artifacts are written exactly once with `O_CREAT | O_EXCL`, so two audits disagreeing about one run is an error rather than a silent overwrite (`agent_session_loop/assurance_models.py:4-5,22-26`).
- **Conflict of interest** — *Is the grader independent of the graded?* `--audit-harness` must differ from `--harness`, rejected at flag validation, because an audit harness equal to the implementation harness would grade its own turns (`agent_session_loop/run_flags.py:544-556`).
- **Signal vs noise** — *Does the identity of this thing include anything volatile?* A finding's fingerprint is schema version, category, component, and invariant key — never run IDs, timestamps, summaries, or evidence, which would make the same defect look new on every run (`agent_session_loop/assurance_models.py:13-17`).
- **Map vs territory** — *Is this claim about the plan or about the repository?* This is the whole purpose of `session-expand`: every plan claim is re-checked against on-disk paths, signatures, fields, dependencies, behavior, and names before a session acts on it (`agent_session_loop/prepare.py:116-131`).
- **Redundancy** — *If this one signal is missing, what else answers the question?* Supervisor-helper closure keeps two independent fallbacks — the bounded stdout tail and the loop itself as backstop — rather than trusting the terminal record alone (`skills/asl/SKILL.md:166-167`).
- **Choke point** — *Is there exactly one path through which this state can change?* All expansion validation, rendering, and plan mutation flow through `asl expand-ingest`; hand-written `.expand.md` files and hand-edited corrections are forbidden (`skills/session-expand/SKILL.md:131`).
- **Absence of evidence** — *Does "no record" mean "did not happen", or "was not observed"?* "Absence of `DONE` is **not** proof that the helper is alive" (`skills/asl/SKILL.md:165`); likewise a rubric lane nobody looked at is `unavailable`, never `all-clear` (`agent_session_loop/assurance_models.py:18-21`).

## Models this tooling applies as judgment

Cite these when the answer is a decision, not a check. There is no enforcement point; the value is that the question gets asked at all.

- **Inversion** — *Instead of "is this plan complete?", ask "how does this plan fail?"* Work backward from the failure to the assumption that produced it.
- **Via negativa** — *What does the ideal version of this remove?* An improvement that can only add layers, options, and invariants is not an ideal, it is an accretion.
- **Second-order thinking** — *And then what?* Every permanent invariant, flag, and gate has a downstream cost paid by everyone who touches the system afterward.
- **Opportunity cost** — *What is not getting done because this is?* The cost of a session is the next-best session it displaces, not the hours it takes.
- **Chesterton's fence** — *Why does this exist, before removing or changing it?* Verifying that something exists is not the same as understanding what its absence would break.
- **Reversibility (type-1 / type-2 doors)** — *If this is wrong, how expensive is undoing it?* A one-way door deserves escalation even when the decision looks defensible; a two-way door deserves a fast default.
- **Circle of competence** — *Is this answerable from the plan, the repository, and the artifacts — or is it a guess?* Outside the circle, relay the question instead of resolving it.
- **Base rates** — *How often has this category of defect recurred before?* Per-category recurrence-after-resolution is a prior about this class of problem, not a fact about this instance.

## Citations by skill

| Skill | Models cited |
| --- | --- |
| `plan-iterate` | inversion, via negativa, second-order thinking |
| `session-expand` | Chesterton's fence, map vs territory |
| `asl` | reversibility, circle of competence, absence of evidence |
