# agent-session-loop

Agent Session Loop (`asl`): run split agent session plans one session at a
time — prepare, preflight, run, supervise, review, and inspect them — plus the
portable ASL Workbench and the shared planning and supervision skills.

> **Status:** a personal tool, published as-is under the [MIT License](LICENSE).
> It is built around the author's own setup (macOS, the Claude Code, Codex and
> Copilot CLIs). Issues and pull requests are not monitored; fork freely.

## Setup

```bash
git clone https://github.com/sglavoie/agent-session-loop.git
cd agent-session-loop
just all
```

ASL needs Python 3.14 or later as `python3`; the wrapper refuses an older
interpreter with a message naming the one it found. The install links `asl`
into `~/.local/bin`, so that directory must be on your `PATH`
(`export PATH="$HOME/.local/bin:$PATH"`).

`just all` (the same as `just install`) links:

- `~/.local/bin/agent-session-loop` and `~/.local/bin/asl` to the wrapper in
  `bin/.local/bin/`
- every directory under `skills/` into the `skills/` directory of each harness
  root that already exists: `~/.claude`, `~/.codex` and `~/.copilot`, plus
  any further Claude Code profile named by `CLAUDE_CONFIG_DIR` or by
  `ASL_CLAUDE_CONFIG_DIRS` (a `:`-separated list, for example
  `export ASL_CLAUDE_CONFIG_DIRS=~/.claude-work`). A harness root that
  does not exist is reported as skipped and never created, and a root outside
  your home directory is ignored.

Each is an absolute symlink into this checkout, replaced atomically, so a
running companion, bridge or supervisor that execs `asl` mid-install never
finds it missing. An existing symlink (say, one a different checkout
installed) is retargeted; a real file or directory is never overwritten.
`just install-dry-run` shows what would change, including which harness roots
were found and which startup cards are stale, without touching anything.

Every directory under `skills/` needs a `SKILL.md`. Shared support directories
that intentionally have none (such as `skills/references/`) must be listed in
`SUPPORT_DIRS` in `install.py`; any other directory without a `SKILL.md` fails
the install as a malformed skill.

`just sync-skills` re-links the skills and renders the ASL startup card into
the context file of each discovered harness root (`~/.codex/AGENTS.md`,
`~/.claude/CLAUDE.md`, and `CLAUDE.md` in every extra Claude profile) and into
`skills/asl/references/supervisor-context.md`. The card lives inside
`<!-- ASL_CONTEXT_START ... -->` / `<!-- ASL_CONTEXT_END -->` markers, so any
other startup instruction in those files is left untouched. Run
`just check-startup-context` to report stale blocks without rewriting them.

After changing ASL or its skills, run `just verify-asl-install` to confirm
the active `asl` wrapper, the installed skill links, the installed ASL skill
and helper, and the rendered startup card in every discovered harness root
all resolve to this checkout. Set `ASL_CLAUDE_CONFIG_DIRS` in the shell that
runs it too, or the extra profiles are not checked.

The launchd jobs ASL and the Workbench install (the maintenance job and the
Workbench connector) are labelled `com.sglavoie.*`. Set
`ASL_LAUNCHD_LABEL_PREFIX` (for example `org.example`) before installing them
to use your own prefix; uninstall an existing job before changing it, or the
old one is left behind.

## Documentation

- [docs/asl-guide.md](docs/asl-guide.md) — ASL operator guide: command families, live supervision, assurance, Agent Mux integration, releasing, and testing
- [docs/agent-session-loop.md](docs/agent-session-loop.md) — the complete ASL reference
- [agent_session_loop/README.md](agent_session_loop/README.md) — package layout and internals

## Related

- [Agent Mux](https://github.com/sglavoie/agentmux) — the separate client
  (`amx`, the cockpit and its daemon) that talks to ASL over the bridge. Recipes
  such as `just paired-release-gate`, `just asl-integration` and
  `just overseer-integration` that the docs mention belong to that repository
  and run from its checkout.

## Development

### Testing

```bash
just test-fast
just test-portable
just test-all
just reliability-stress
just test-browser   # Workbench browser tests; needs Node and npm
```

Stdlib `unittest` only. CI (`.github/workflows/`) runs a portable Linux lane
and the full suite on macOS. See [Testing in the ASL guide](docs/asl-guide.md#testing)
for what each recipe covers.

### Linting

```bash
just lint
```

Runs [ShellCheck](https://www.shellcheck.net/) on all shell scripts (config in
`.shellcheckrc`), then `ruff check`, `ruff format --check`, and `mypy` on the
Python code (config in `pyproject.toml`).

## Dependencies

- macOS for ASL itself: the full suite gates on it, and the scheduled
  maintenance job and the Workbench connector are launchd jobs. The core CLI
  and the portable lane also run on Linux (CI checks that), where
  `asl maintenance install` reports that no launchd job is possible. The
  portable Workbench service targets a Linux host under a systemd user unit.
- Python 3.14+ (`asl_workbench` stays importable on 3.13 for its remote host)
- `git`, `just`
- `ruff`, `mypy`, `shellcheck` for `just lint`
- At least one harness CLI for real runs: `claude` (Claude Code), `codex` or
  `copilot`. ASL launches whichever `--harness` names; the others are optional.

## License

[MIT](LICENSE)
