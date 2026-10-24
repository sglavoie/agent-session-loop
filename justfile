# List available recipes
default:
    @just --list

# Install this checkout as the active ASL
all: install

# Install this checkout as the active ASL (wrappers, skills, startup card)
install:
    ./install.py
    python3 -m agent_session_loop.startup_context

# Show what `just install` would change (links and startup cards), touching nothing
install-dry-run:
    ./install.py --dry-run
    -python3 -m agent_session_loop.startup_context --check

# Refresh the installed skill links and the rendered startup context
sync-skills:
    ./install.py --skills-only
    python3 -m agent_session_loop.startup_context

# Report stale generated startup-context blocks without rewriting them
check-startup-context:
    python3 -m agent_session_loop.startup_context --check

# Verify active ASL wrappers and installed skills resolve to this checkout
verify-asl-install:
    python3 -m agent_session_loop.install_verify

# Run fast unit/schema/parser tests
test-fast:
    python3 -m tests.run_suite --fast

# Run the subset that needs no external process: no git, no shell script, no
# launchd, no harness executable. This is the lane a non-macOS CI runner can
# gate on honestly; the macOS lane runs `test-all` against the real tools.
test-portable:
    python3 -m tests.run_suite --portable

# Run the complete test suite as isolated parallel shards
test-all:
    python3 -m tests.run_suite --all

# Reliability stress gate. Repeats the load- and calendar-sensitive scenarios
# in isolation, then again as concurrent shards while the full suite loads the
# machine, then sweeps the date-sensitive ones across timezones, and finally
# enforces the end-to-end time budgets on a quiet machine.
#
# The correctness scenarios synchronize on run state and pin their own clock,
# so neither contention nor the calendar may change their outcome. Time budgets
# are measured separately and deliberately, because contention legitimately
# changes those.
#
# The crash, race and cancellation scenarios are a third list rather than more
# entries in the first, because they are the one kind of check a loaded machine
# is *supposed* to be able to break and must not: a kill that has to land on a
# deadline, a latch two threads reach at once, a journal several producers append
# to. They repeat in isolation and under load with the others, and they are kept
# out of the budget phase, which measures CLI latency and has nothing to say
# about a deadline a test pins against its own injected clock.
reliability-stress reps="10" shards="4":
    scripts/reliability-stress.sh {{ reps }} {{ shards }}

# Run shell linter
lint-shell:
    ./lint.sh

# Run Python linter, format check, and type checker
lint-py:
    ruff check .
    ruff format --check .
    mypy

# Run all linters
lint: lint-shell lint-py
