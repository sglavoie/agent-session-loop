# Repository guide

- Agent Session Loop (`agent_session_loop/`, the `asl` CLI), its portable
  Workbench (`asl_workbench/`), and the ASL skills (`skills/`). Extracted from
  the `~/scripts` dotfiles repo with history; that repo still owns the harness
  context files (`~/.claude/CLAUDE.md`, `~/.codex/AGENTS.md`, ...) and the
  non-ASL skills.
- `just all` (alias of `just install`) runs `install.py`, which links
  `~/.local/bin/{asl,agent-session-loop}` and every `skills/<name>` into the
  four harness skill directories as absolute symlinks into this checkout, each
  swapped with `os.replace` so nothing that execs `asl` sees a gap. It then
  renders the ASL startup card into the installed context files.
- `skills/<name>/SKILL.md` is the source of truth for skills. Run
  `just sync-skills` after any change there: it re-links the skills and runs
  `agent_session_loop/startup_context.py`, which re-renders the marked ASL
  startup card in `~/.codex/AGENTS.md`, `~/.claude/CLAUDE.md`,
  `~/.claude-personal/CLAUDE.md` (stow links into `~/scripts`, so the write is
  a diff to commit there) and in `skills/asl/references/supervisor-context.md`.
  Never hand-edit inside `<!-- ASL_CONTEXT_START ... -->`;
  `just check-startup-context` reports a stale block without rewriting it.
- `just test-fast` / `just test-all` run the shard driver `tests/run_suite.py`. A
  new test module must be added to its `FAST_MODULES` / `INTEGRATION_MODULES`
  lists, or the discovery cross-check fails the suite.
- `just lint` runs ShellCheck via `lint-shell`, then `ruff check`, the
  `ruff format --check` gate and mypy via `lint-py`. `just verify-asl-install`
  checks that the active ASL wrappers and installed skills resolve to this
  checkout.
- ASL wrapper convention: the Python wrapper `bin/.local/bin/agent-session-loop`
  (with `asl` a symlink to it) resolves `REPO_ROOT` via
  `Path(__file__).resolve().parents[3]`; keep that depth if the wrapper moves.
  Agent Mux's release gate also expects that path and
  `tests/fixtures/agent_mux_contract/manifest.json`, and runs `just test-all`,
  `just lint`, `just reliability-stress`, `just all` and
  `just verify-asl-install` here (`asl_root=` this checkout).
- Skill and README prose is test-gated by `tests/test_asl_docs_*.py`; read its
  assertions before rewording either. `agent_session_loop/AGENTS.md` still
  governs ASL behavior changes.
- The repo is indexed by CodeGraph (`.codegraph/`, gitignored). Prefer
  `codegraph explore "<symbols or question>"` over grep.
- Planning front door: `plan-iterate`, then `asl prepare` / `asl preflight` (with
  `plan-preflight-iterate` for repairs), then the split, then the `asl` skill to
  supervise `asl run`. This moves preflight defects to before the run instead of
  surfacing them as review findings during it.
