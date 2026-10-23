# agent-session-loop

Agent Session Loop (`asl`): run split agent session plans one session at a
time — prepare, preflight, run, supervise, review, and inspect them — plus the
portable ASL Workbench and the shared planning and supervision skills.

Extracted from the `~/scripts` dotfiles repository with its history. The
dotfiles repository keeps the harness context files and the non-ASL skills;
this one owns everything ASL.

## Setup

```bash
cd ~/1_dev_projects/sglavoie_agent-session-loop
just all
```

`just all` (the same as `just install`) links:

- `~/.local/bin/agent-session-loop` and `~/.local/bin/asl` to the wrapper in
  `bin/.local/bin/`
- every directory under `skills/` into `~/.claude/skills`,
  `~/.claude-personal/skills`, `~/.codex/skills` and `~/.copilot/skills`

Each is an absolute symlink into this checkout, replaced atomically, so a
running companion, bridge or supervisor that execs `asl` mid-install never
finds it missing. An existing symlink (say, one a different checkout
installed) is retargeted; a real file or directory is never overwritten.
`just install-dry-run` shows what would change.

Every directory under `skills/` needs a `SKILL.md`. Shared support directories
that intentionally have none (such as `skills/references/`) must be listed in
`SUPPORT_DIRS` in `install.py`; any other directory without a `SKILL.md` fails
the install as a malformed skill.

`just sync-skills` re-links the skills and renders the ASL startup card into
the installed Codex and Claude context files (`~/.codex/AGENTS.md`,
`~/.claude/CLAUDE.md`, `~/.claude-personal/CLAUDE.md`) and into
`skills/asl/references/supervisor-context.md`. The card lives inside
`<!-- ASL_CONTEXT_START ... -->` / `<!-- ASL_CONTEXT_END -->` markers, so any
other startup instruction in those files is left untouched. Run
`just check-startup-context` to report stale blocks without rewriting them.

After changing ASL or its skills, run `just verify-asl-install` to confirm
the active `asl` wrapper, the installed skill links, the installed ASL skill
and helper, and the rendered startup card in the active Codex and both Claude
context files all resolve to this checkout.

## Documentation

- [docs/asl-guide.md](docs/asl-guide.md) — ASL operator guide: command families, live supervision, assurance, Agent Mux integration, releasing, and testing
- `docs/agent-session-loop.md` — the complete ASL reference
- `agent_session_loop/README.md` — package layout and internals

## Development

### Testing

```bash
just test-fast
just test-portable
just test-all
just reliability-stress
```

Stdlib `unittest` only. The GitHub workflows are disabled on purpose: tests and
lint run locally. See [Testing in the ASL guide](docs/asl-guide.md#testing) for
what each recipe covers.

### Linting

```bash
just lint
```

Runs [ShellCheck](https://www.shellcheck.net/) on all shell scripts (config in
`.shellcheckrc`), then `ruff check`, `ruff format --check`, and `mypy` on the
Python code (config in `pyproject.toml`).

## Dependencies

- Python 3.14+ (`asl_workbench` stays importable on 3.13 for the Pi)
- `git`, `just`
- `ruff`, `mypy`, `shellcheck` for `just lint`
- The harness CLIs ASL launches (`claude`, `codex`, `copilot`) for real runs
