#!/usr/bin/env bash
# Repeat the load- and calendar-sensitive scenarios in isolation, across
# timezones and under full-suite load. Run through `just reliability-stress`;
# see the comment above that recipe for why each phase exists.
#
# Usage: scripts/reliability-stress.sh [REPS] [SHARDS]
set -euo pipefail

reps="${1:-10}"
shards="${2:-4}"
# A non-number would make the loops below run zero times and report ok, and
# `seq 1 0` counts down on macOS, so anything but a positive integer is a
# usage error rather than a vacuous pass.
for count in "$reps" "$shards"; do
    if [[ ! "$count" =~ ^[1-9][0-9]*$ ]]; then
        echo "usage: $0 [REPS] [SHARDS] (positive integers; got '$count')" >&2
        exit 2
    fi
done
export ASL_CLI_TRACE=0

# Scenarios that were observed failing under load or as the calendar moved,
# plus the fault-injection scenarios whose whole subject is what survives an
# interruption. Extend both lists as new fault scenarios land.
#
# The repair publication window is here because its correctness is a
# statement about durability ordering -- the rollback image is fsynced
# before the first destination byte moves, and the next locked caller
# restores it. Contention is what makes a mis-ordered write observable, so
# a machine under load is the honest place to repeat it.
scenarios=(
    tests.test_asl_cli_json_plans_listing.JsonPlansListingCliTests.test_plans_cli_stale_and_never_run_filters
    tests.test_asl_cli_json_plans_listing.JsonPlansListingCliTests.test_plans_stale_filter_pins_calendar_edges_against_injected_clock
    tests.test_asl_cli_json_plans_listing.JsonPlansListingCliTests.test_plans_stale_filter_defaults_to_the_wall_clock
    tests.test_asl_e2e_review_reopen.ReviewReopenEndToEndTests.test_completed_review_reopens_and_rereviews_through_cli
    tests.test_asl_e2e_review_reopen.ReviewReopenEndToEndTests.test_autopilot_reopens_exact_legacy_review_history_after_normalization
    tests.test_asl_review_repair_publication_window.ReviewRepairPublicationWindowTests
)
date_scenarios=(
    tests.test_asl_cli_json_plans_listing.JsonPlansListingCliTests.test_plans_cli_stale_and_never_run_filters
    tests.test_asl_cli_json_plans_listing.JsonPlansListingCliTests.test_plans_stale_filter_pins_calendar_edges_against_injected_clock
    tests.test_asl_cli_json_plans_listing.JsonPlansListingCliTests.test_plans_stale_filter_defaults_to_the_wall_clock
)

# Crash, race and cancellation. Each of these is a claim that holds when the
# machine is hostile: a forced stop that must land through a wedged pipe and
# a wedged protocol call, a pid that must be authenticated rather than
# assumed, two threads that must not both win one latch, several producers
# that must not lose one another's records, an expiry and an acceptance that
# must not both answer one request, and a dead supervisor whose accepted
# correction must be stranded rather than re-decided. Contention is the only
# condition under which most of these can fail at all, which is why they are
# repeated here rather than trusted from a single unloaded pass.
# Remote recovery also has to retain settlements across queue pressure and a
# dropped link. Cursor writers and replay/follow transitions cover the other
# boundaries recently hardened: concurrent publication must not move a cursor
# backward, and replacing or appending a log during replay must not lose text.
fault_scenarios=(
    tests.test_asl_redirect_adversarial.SlowStorageTests
    tests.test_asl_redirect_adversarial.BlockedPipeTests
    tests.test_asl_redirect_adversarial.CompletionRaceTests
    tests.test_asl_redirect_adversarial.ConcurrentFeedbackTests
    tests.test_asl_redirect_adversarial.StaleProcessIdentityTests
    tests.test_asl_redirect_adversarial.ExpiredSubmissionTests
    tests.test_asl_redirect_adversarial.StoppedSupervisorTests
    tests.test_asl_redirect_control.EscalationTimingTests
    tests.test_asl_redirect_control_acceptance.BlockedTransportTests
    tests.test_asl_redirect_control_live.LiveWorkerTerminationTests
    tests.test_asl_redirect_continuation_journal.SerializationTests
    tests.test_asl_redirect_continuation_settle.SettleRedirectedTurnTests
    tests.test_asl_control_mailbox_cli_supervise.MailboxCliTestsPart8.test_overlapping_cursor_writers_cannot_regress_the_saved_sequence
    tests.asl_tail_cases_recovery.TailFollowerRecoveryTests.test_active_replay_restarts_on_changed_replacement
    tests.asl_tail_cases_recovery.TailFollowerRecoveryTests.test_recent_replay_follows_a_line_appended_after_its_read
    tests.asl_tail_cases_recovery.TailFollowerRecoveryTests.test_initial_replay_follows_a_line_appended_after_its_read
)

mkdir -p .cache/reliability-stress
log_dir="$(mktemp -d "$PWD/.cache/reliability-stress/run.XXXXXX")"
keep_logs=0
suite_pid=""
shard_pids=()

# Children first, so each one is still ours to find when its parent goes.
kill_tree() {
    local child
    for child in $(pgrep -P "$1" 2>/dev/null); do
        kill_tree "$child"
    done
    kill -TERM "$1" 2>/dev/null || true
}

# An interrupted or failed run must not leave the load suite or a stress shard
# running against a deleted log directory. Failed and interrupted runs retain
# their complete logs for inspection and CI upload. The pid variables are
# cleared once each job is reaped, so a recycled pid is never signalled.
cleanup() {
    local status=$?
    trap - EXIT INT TERM
    local pid
    for pid in $suite_pid ${shard_pids[@]+"${shard_pids[@]}"}; do
        kill_tree "$pid"
    done
    if [ "$status" -ne 0 ] || [ "$keep_logs" -ne 0 ]; then
        echo "reliability-stress: logs retained at $log_dir"
    else
        rm -rf "$log_dir"
    fi
    exit "$status"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

# Print each failing log whole and only the tail of every other one, which is
# enough to see where the passing runs stood.
report_failure() {
    local log failing
    for failing in "$@"; do
        echo "--- $failing (failed)"
        cat "$failing"
    done
    for log in "$log_dir"/*.log; do
        for failing in "$@"; do
            [ "$log" = "$failing" ] && continue 2
        done
        echo "--- $log (last 5 lines)"
        tail -n 5 "$log"
    done
}

echo "== isolated repetitions (x$reps) =="
for rep in $(seq 1 "$reps"); do
    if ! python3 -m unittest "${scenarios[@]}" "${fault_scenarios[@]}" > "$log_dir/isolated-$rep.log" 2>&1; then
        report_failure "$log_dir/isolated-$rep.log"
        exit 1
    fi
done

# Whole-degree, three-quarter-hour, and southern-hemisphere DST offsets. A
# date-sensitive path that reached for a naive local clock reads differently
# under each of these; all of them must agree.
echo "== date-sensitive scenarios across timezones =="
for tz in UTC America/Los_Angeles Asia/Kathmandu Pacific/Chatham; do
    echo "-- TZ=$tz"
    tz_log="$log_dir/tz-${tz//\//-}.log"
    if ! TZ="$tz" python3 -m unittest "${date_scenarios[@]}" > "$tz_log" 2>&1; then
        report_failure "$tz_log"
        exit 1
    fi
done

# The full suite runs here to saturate the machine, and this gate judges only
# the scenarios racing against it. The suite already shards across every core
# on its own, so stacking shards on top starves subprocess signal delivery --
# `just test-all` is the suite's own gate and runs it unloaded. Its outcome is
# still reported rather than swallowed: a regression that only the loaded run
# reveals should be visible here even though it does not fail this recipe.
echo "== $shards concurrent shards (x$reps) under full-suite load =="
python3 -m tests.run_suite --all > "$log_dir/suite.log" 2>&1 &
suite_pid=$!
for shard in $(seq 1 "$shards"); do
    (
        for rep in $(seq 1 "$reps"); do
            python3 -m unittest "${scenarios[@]}" "${fault_scenarios[@]}" || exit 1
        done
    ) > "$log_dir/shard-$shard.log" 2>&1 &
    shard_pids+=("$!")
done
failed_logs=()
for shard in $(seq 1 "$shards"); do
    wait "${shard_pids[$((shard - 1))]}" || failed_logs+=("$log_dir/shard-$shard.log")
done
shard_pids=()
suite_status=0
wait "$suite_pid" || suite_status=$?
suite_pid=""
if [ "${#failed_logs[@]}" -ne 0 ]; then
    report_failure "${failed_logs[@]}"
    exit 1
fi
if [ "$suite_status" -ne 0 ]; then
    keep_logs=1
    echo "-- background load suite exited $suite_status (reported, not gated):"
    tail -n 40 "$log_dir/suite.log"
    echo "-- gate on it with: just test-all"
fi

# Budgets last and alone: everything above has finished, so the machine is
# quiet and a measured overrun means the CLI got slower, not that the stress
# run was still competing with itself. Only `scenarios` is measured here --
# the fault scenarios assert against injected clocks and bounded waits, so a
# wall-clock reading of them would measure the test's own patience.
echo "== end-to-end time budgets (quiet machine) =="
budget_status=0
ASL_E2E_TIME_BUDGETS=1 python3 -m unittest "${scenarios[@]}" > "$log_dir/budgets.log" 2>&1 || budget_status=$?
cat "$log_dir/budgets.log"
if [ "$budget_status" -ne 0 ]; then
    exit "$budget_status"
fi

echo "reliability-stress: ok"
