# ruff: noqa: F401, F403, F405, I001
"""Assurance ledger identity and schema tests."""

from __future__ import annotations

# ruff: noqa: F401, F403, F405
from tests.asl_assurance_ledger_test_support import *


class IssueIdTests(LedgerTestCase):
    def test_a_new_fingerprint_takes_the_next_number(self) -> None:
        first = ledger.append_issue_observation(
            _observation(), state_dir=self.state_dir
        )
        second = ledger.append_issue_observation(
            _observation(finding=_finding(invariant_key="commit.guard-held")),
            state_dir=self.state_dir,
        )
        self.assertEqual(["ASI-1", "ASI-2"], [first.issue_id, second.issue_id])

    def test_a_recurring_fingerprint_reuses_its_first_id(self) -> None:
        first = ledger.append_issue_observation(
            _observation(), state_dir=self.state_dir
        )
        recurrence = ledger.append_issue_observation(
            _observation(run_id=OTHER_RUN_ID, observed_at=LATER_TIMESTAMP),
            state_dir=self.state_dir,
        )
        self.assertEqual(first.issue_id, recurrence.issue_id)
        self.assertEqual(2, self.read().next_issue_number)

    def test_an_id_is_not_reused_when_its_observation_line_is_corrupted(self) -> None:
        ledger.append_issue_observation(_observation(), state_dir=self.state_dir)
        ledger.append_issue_observation(
            _observation(finding=_finding(invariant_key="commit.guard-held")),
            state_dir=self.state_dir,
        )
        ledger.append_record(
            ledger.KIND_ISSUE_DISPOSITION,
            assurance_models_inbox.Disposition(
                issue_id="ASI-2", kind="resolve", recorded_at=TIMESTAMP
            ),
            state_dir=self.state_dir,
        )
        lines = self.ledger_path.read_text(encoding="utf-8").splitlines()
        lines[1] = "{corrupted"
        self.write_ledger_text("\n".join(lines) + "\n")
        view = self.read()
        self.assertEqual(3, view.next_issue_number)
        allocated = ledger.append_issue_observation(
            _observation(finding=_finding(invariant_key="scope.escape")),
            state_dir=self.state_dir,
        )
        self.assertEqual("ASI-3", allocated.issue_id)

    def test_concurrent_threads_never_mint_the_same_id(self) -> None:
        self.state_dir.mkdir(parents=True, exist_ok=True)
        workers = 12
        barrier = threading.Barrier(workers)
        lock = threading.Lock()
        allocated: list[str] = []
        failures: list[BaseException] = []

        def append(index: int) -> None:
            try:
                barrier.wait()
                record = ledger.append_issue_observation(
                    _observation(finding=_finding(invariant_key=f"lane.key-{index}")),
                    state_dir=self.state_dir,
                )
                with lock:
                    allocated.append(record.issue_id)
            except BaseException as error:  # noqa: BLE001 - reported below
                with lock:
                    failures.append(error)

        threads = [
            threading.Thread(target=append, args=(index,)) for index in range(workers)
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual([], failures)
        self.assertEqual(
            [f"ASI-{number}" for number in range(1, workers + 1)],
            sorted(allocated, key=assurance_models_primitives.issue_id_number),
        )
        view = self.read()
        self.assertEqual(workers, len(view.records))
        self.assertEqual((), view.rejected)

    def test_concurrent_processes_never_mint_the_same_id(self) -> None:
        self.state_dir.mkdir(parents=True, exist_ok=True)
        workers = 5
        processes = [
            subprocess.Popen(
                [
                    sys.executable,
                    "-c",
                    _WORKER_SOURCE,
                    str(self.state_dir),
                    f"proc.key-{index}",
                ],
                cwd=REPO_ROOT,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            for index in range(workers)
        ]
        allocated = []
        for process in processes:
            stdout, stderr = process.communicate(timeout=120)
            self.assertEqual(0, process.returncode, stderr)
            allocated.append(stdout.strip())
        self.assertEqual(
            [f"ASI-{number}" for number in range(1, workers + 1)],
            sorted(allocated, key=assurance_models_primitives.issue_id_number),
        )
        view = self.read()
        self.assertEqual(workers, len(view.records))
        self.assertEqual((), view.rejected)
        self.assertEqual(list(range(1, workers + 1)), [r.seq for r in view.records])


class SchemaVersionTests(LedgerTestCase):
    def _v1_line(self, kind: str, payload, issue_id: str | None = None) -> str:
        return self._line(1, kind, payload, issue_id)

    def _line(
        self, version: int, kind: str, payload, issue_id: str | None = None
    ) -> str:
        return json.dumps(
            {
                "version": version,
                "kind": kind,
                "recorded_at": TIMESTAMP,
                "issue_id": issue_id,
                "payload": payload.to_dict(),
            }
        )

    def test_new_writes_use_v3(self) -> None:
        ledger.append_issue_observation(_observation(), state_dir=self.state_dir)
        envelope = json.loads(self.ledger_path.read_text(encoding="utf-8"))
        self.assertEqual(3, ledger.LEDGER_SCHEMA_VERSION)
        self.assertEqual(3, envelope["version"])
        self.assertEqual(3, self.read().records[0].version)

    def test_a_complete_v1_history_reads_back_unchanged(self) -> None:
        observation = _observation()
        merge = assurance_models_inbox.IssueMerge(
            source_issue_id="ASI-2", target_issue_id="ASI-1", recorded_at=TIMESTAMP
        )
        lines = [
            self._v1_line("issue_observed", observation, issue_id="ASI-1"),
            self._v1_line("audit_pending", _summary()),
            self._v1_line(
                "issue_disposition",
                assurance_models_inbox.Disposition(
                    issue_id="ASI-1", kind="resolve", recorded_at=TIMESTAMP
                ),
            ),
            self._v1_line("issue_merged", merge),
            self._v1_line(
                "human_closeout",
                assurance_models_inbox.Closeout(
                    run_id=RUN_ID, outcome="accepted", recorded_at=TIMESTAMP
                ),
            ),
        ]
        self.write_ledger_text("\n".join(lines) + "\n")
        view = self.read()
        self.assertEqual((), view.rejected)
        self.assertEqual([1] * len(lines), [record.version for record in view.records])
        self.assertEqual(observation.to_dict(), view.records[0].payload.to_dict())
        self.assertEqual({"ASI-2": "ASI-1"}, view.merge_edges)

    def test_a_line_claiming_a_kind_its_version_predates_is_an_unknown_kind(
        self,
    ) -> None:
        ruling = assurance_models_inbox.IssueKeptDistinct(
            issue_ids=("ASI-1", "ASI-2"),
            identity_digests=(DIGEST, OTHER_DIGEST),
            recorded_at=TIMESTAMP,
        )
        for version, kind, payload in (
            (1, "issue_identity_bound", _binding()),
            (2, "issue_kept_distinct", ruling),
        ):
            with self.subTest(version=version):
                self.write_ledger_text(self._line(version, kind, payload) + "\n")
                view = self.read()
                self.assertEqual((), view.records)
                self.assertEqual(ledger.REJECT_UNKNOWN_KIND, view.rejected[0].reason)
                self.assertIn(f"v{version}", view.rejected[0].detail or "")

    def test_a_version_this_build_cannot_read_is_still_rejected(self) -> None:
        self.write_ledger_text(self._line(4, "audit_pending", _summary()) + "\n")
        self.assertEqual(
            ledger.REJECT_UNSUPPORTED_VERSION, self.read().rejected[0].reason
        )

    def test_appending_after_v1_history_never_rewrites_it(self) -> None:
        self.write_ledger_text(
            self._v1_line("issue_observed", _observation(), issue_id="ASI-1") + "\n"
        )
        original = self.ledger_path.read_bytes()
        ledger.append_identity_binding(_binding(), state_dir=self.state_dir)
        data = self.ledger_path.read_bytes()
        self.assertTrue(data.startswith(original))
        self.assertEqual([1, 3], [record.version for record in self.read().records])


class IdentityBindingTests(LedgerTestCase):
    def observe(self, invariant_key: str = "retry.budget-respected") -> str:
        record = ledger.append_issue_observation(
            _observation(finding=_finding(invariant_key=invariant_key)),
            state_dir=self.state_dir,
        )
        assert record.issue_id is not None
        return record.issue_id

    def test_a_binding_round_trips_through_the_file(self) -> None:
        self.observe()
        binding = _binding(note="Allocated with the first sighting.")
        record = ledger.append_identity_binding(binding, state_dir=self.state_dir)
        self.assertEqual(ledger.KIND_ISSUE_IDENTITY_BOUND, record.kind)
        self.assertIsNone(record.issue_id)
        view = self.read()
        self.assertEqual((), view.rejected)
        self.assertEqual(binding.to_dict(), view.records[-1].payload.to_dict())
        self.assertEqual({"issue.asi-1": "ASI-1"}, view.identity_bindings)
        self.assertEqual("ASI-1", view.issue_id_for_identity_key("issue.asi-1"))

    def test_the_stored_line_is_deterministic_and_canonically_sorted(self) -> None:
        self.observe()
        ledger.append_identity_binding(_binding(), state_dir=self.state_dir)
        line = self.ledger_path.read_text(encoding="utf-8").splitlines()[-1]
        envelope = json.loads(line)
        self.assertEqual(
            json.dumps(envelope, sort_keys=True, separators=(",", ":")), line
        )
        self.assertEqual(_binding().to_dict(), envelope["payload"])

    def test_an_unbound_key_names_nothing(self) -> None:
        self.observe()
        self.assertIsNone(self.read().issue_id_for_identity_key("issue.asi-1"))

    def test_a_key_follows_the_merge_that_moved_its_issue(self) -> None:
        self.observe()
        self.observe("commit.guard-held")
        ledger.append_identity_binding(
            _binding(identity_key="issue.asi-2", issue_id="ASI-2"),
            state_dir=self.state_dir,
        )
        ledger.append_record(
            ledger.KIND_ISSUE_MERGED,
            assurance_models_inbox.IssueMerge(
                source_issue_id="ASI-2",
                target_issue_id="ASI-1",
                recorded_at=LATER_TIMESTAMP,
            ),
            state_dir=self.state_dir,
        )
        self.assertEqual("ASI-1", self.read().issue_id_for_identity_key("issue.asi-2"))

    def test_a_merged_away_key_may_be_rebound_to_the_issue_that_owns_it(self) -> None:
        self.observe()
        self.observe("commit.guard-held")
        ledger.append_record(
            ledger.KIND_ISSUE_MERGED,
            assurance_models_inbox.IssueMerge(
                source_issue_id="ASI-2", target_issue_id="ASI-1", recorded_at=TIMESTAMP
            ),
            state_dir=self.state_dir,
        )
        record = ledger.append_identity_binding(
            _binding(identity_key="issue.asi-2", issue_id="ASI-1", source="merge"),
            state_dir=self.state_dir,
        )
        self.assertEqual("ASI-1", record.payload.issue_id)
        self.assertEqual("ASI-2", record.payload.named_issue_id)

    def test_the_same_binding_twice_is_refused(self) -> None:
        self.observe()
        ledger.append_identity_binding(_binding(), state_dir=self.state_dir)
        with self.assertRaises(LedgerError) as caught:
            ledger.append_identity_binding(_binding(), state_dir=self.state_dir)
        self.assertEqual(ledger.ERROR_IDENTITY_ALREADY_BOUND, caught.exception.reason)
        self.assertEqual(2, len(self.read().records))

    def test_a_key_cannot_be_repointed_at_another_issue(self) -> None:
        self.observe()
        self.observe("commit.guard-held")
        ledger.append_identity_binding(_binding(), state_dir=self.state_dir)
        with self.assertRaises(LedgerError) as caught:
            ledger.append_identity_binding(
                _binding(issue_id="ASI-2", source="reconcile"),
                state_dir=self.state_dir,
            )
        self.assertEqual(ledger.ERROR_IDENTITY_KEY_CONFLICT, caught.exception.reason)
        self.assertEqual({"issue.asi-1": "ASI-1"}, self.read().identity_bindings)

    def test_a_binding_to_an_issue_that_was_never_minted_is_refused(self) -> None:
        self.observe()
        with self.assertRaises(LedgerError) as caught:
            ledger.append_identity_binding(
                _binding(identity_key="issue.asi-9", issue_id="ASI-9"),
                state_dir=self.state_dir,
            )
        self.assertEqual(ledger.ERROR_UNKNOWN_ISSUE, caught.exception.reason)
        self.assertEqual(1, len(self.read().records))

    def test_a_key_that_names_no_minted_issue_is_refused(self) -> None:
        self.observe()
        with self.assertRaises(LedgerError) as caught:
            ledger.append_identity_binding(
                _binding(identity_key="issue.asi-9", source="reconcile"),
                state_dir=self.state_dir,
            )
        self.assertEqual(ledger.ERROR_UNKNOWN_ISSUE, caught.exception.reason)

    def test_a_binding_to_a_merged_away_issue_is_refused_not_redirected(self) -> None:
        self.observe()
        self.observe("commit.guard-held")
        ledger.append_record(
            ledger.KIND_ISSUE_MERGED,
            assurance_models_inbox.IssueMerge(
                source_issue_id="ASI-2", target_issue_id="ASI-1", recorded_at=TIMESTAMP
            ),
            state_dir=self.state_dir,
        )
        with self.assertRaises(LedgerError) as caught:
            ledger.append_identity_binding(
                _binding(identity_key="issue.asi-2", issue_id="ASI-2"),
                state_dir=self.state_dir,
            )
        self.assertEqual(ledger.ERROR_ISSUE_NOT_CANONICAL, caught.exception.reason)
        self.assertEqual({}, self.read().identity_bindings)

    def test_the_generic_writer_refuses_the_binding_kind(self) -> None:
        self.observe()
        with self.assertRaises(LedgerError) as caught:
            ledger.append_record(
                ledger.KIND_ISSUE_IDENTITY_BOUND, _binding(), state_dir=self.state_dir
            )
        self.assertEqual(ledger.ERROR_INVALID_KIND, caught.exception.reason)
        self.assertIn("append_identity_binding", str(caught.exception))

    def test_a_binding_payload_of_the_wrong_type_is_refused(self) -> None:
        with self.assertRaises(LedgerError) as caught:
            ledger.append_identity_binding(_summary(), state_dir=self.state_dir)
        self.assertEqual(ledger.ERROR_INVALID_PAYLOAD, caught.exception.reason)

    def test_an_id_is_not_minted_over_a_key_a_stored_binding_claims(self) -> None:
        self.observe()
        self.write_ledger_text(
            self.ledger_path.read_text(encoding="utf-8")
            + json.dumps(
                {
                    "version": 2,
                    "kind": "issue_identity_bound",
                    "recorded_at": TIMESTAMP,
                    "issue_id": None,
                    "payload": _binding(
                        identity_key="issue.asi-7", source="reconcile"
                    ).to_dict(),
                }
            )
            + "\n"
        )
        self.assertEqual(8, self.read().next_issue_number)

    def test_a_binding_larger_than_the_cap_is_refused(self) -> None:
        self.observe()
        with self.assertRaises(assurance_models_primitives.AssuranceError):
            _binding(note="n" * (assurance_models_vocab.MAX_TEXT_CHARS + 1))

    def test_an_unknown_payload_key_is_rejected_not_ignored(self) -> None:
        payload = _binding().to_dict()
        payload["issue_ids"] = ["ASI-1"]
        self.write_ledger_text(
            json.dumps(
                {
                    "version": 2,
                    "kind": "issue_identity_bound",
                    "recorded_at": TIMESTAMP,
                    "issue_id": None,
                    "payload": payload,
                }
            )
            + "\n"
        )
        view = self.read()
        self.assertEqual((), view.records)
        self.assertEqual(ledger.REJECT_INVALID_RECORD, view.rejected[0].reason)
        self.assertIn("'issue_ids'", view.rejected[0].detail or "")

    def test_a_binding_must_not_carry_an_envelope_issue_id(self) -> None:
        self.write_ledger_text(
            json.dumps(
                {
                    "version": 2,
                    "kind": "issue_identity_bound",
                    "recorded_at": TIMESTAMP,
                    "issue_id": "ASI-1",
                    "payload": _binding().to_dict(),
                }
            )
            + "\n"
        )
        view = self.read()
        self.assertEqual((), view.records)
        self.assertEqual(ledger.REJECT_INVALID_RECORD, view.rejected[0].reason)
