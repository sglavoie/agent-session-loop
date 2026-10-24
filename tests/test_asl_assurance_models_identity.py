# mypy: disable-error-code="attr-defined,name-defined"
# ruff: noqa: F403, F405
"""ASL assurance identity and normalized-finding tests."""

# ruff: noqa: F403, F405
from tests.asl_assurance_models_support import *


class IssueIdTests(unittest.TestCase):
    def test_canonical_and_shorthand_forms_agree(self) -> None:
        self.assertEqual("ASI-42", assurance_models_primitives.parse_issue_id("ASI-42"))
        self.assertEqual("ASI-42", assurance_models_primitives.parse_issue_id("42"))
        self.assertEqual("ASI-42", assurance_models_primitives.parse_issue_id(42))
        self.assertEqual(42, assurance_models_primitives.issue_id_number("ASI-42"))

    def test_ambiguous_and_invalid_forms_are_rejected(self) -> None:
        for value in ("ASI-0", "0", "-1", "ASI-007", "asi-42", "ASI-", "", 0, True):
            with (
                self.subTest(value=value),
                self.assertRaises(AssuranceError),
            ):
                assurance_models_primitives.parse_issue_id(value)


class IdentityKeyTests(unittest.TestCase):
    def test_a_key_is_minted_from_an_allocated_issue_id(self) -> None:
        self.assertEqual(
            "issue.asi-42", assurance_models_primitives.identity_key_for_issue("ASI-42")
        )
        self.assertEqual(
            "issue.asi-42", assurance_models_primitives.identity_key_for_issue(42)
        )
        self.assertEqual(
            "ASI-42",
            assurance_models_primitives.issue_id_for_identity_key("issue.asi-42"),
        )

    def test_keys_outside_the_minted_space_are_rejected(self) -> None:
        for value in (
            "issue.asi-0",
            "issue.asi-007",
            "issue.ASI-42",
            "ASI-42",
            "issue.asi-",
            "issue.asi-42 ",
            "asi-42",
            "",
            42,
            True,
            None,
        ):
            with self.subTest(value=value), self.assertRaises(AssuranceError):
                assurance_models_primitives.parse_identity_key(value)

    def test_an_oversized_key_is_rejected_before_it_is_matched(self) -> None:
        with self.assertRaisesRegex(AssuranceError, "identity key"):
            assurance_models_primitives.parse_identity_key(
                f"issue.asi-{'1' * assurance_models_vocab.MAX_TOKEN_CHARS}"
            )


class IssueIdentityBindingTests(unittest.TestCase):
    def _binding(self, **overrides) -> assurance_models_inbox.IssueIdentityBinding:
        payload = {
            "identity_key": "issue.asi-1",
            "issue_id": "ASI-1",
            "source": "allocation",
            "bound_at": TIMESTAMP,
        }
        payload.update(overrides)
        return assurance_models_inbox.IssueIdentityBinding(**payload)

    def test_a_binding_round_trips_through_its_parser(self) -> None:
        binding = self._binding(source="merge", issue_id="ASI-9", note="reviewed")
        self.assertEqual(
            binding,
            assurance_models_inbox.parse_issue_identity_binding(binding.to_dict()),
        )

    def test_the_key_names_the_issue_it_was_minted_with(self) -> None:
        binding = self._binding(
            identity_key="issue.asi-2", issue_id="ASI-1", source="merge"
        )
        self.assertEqual("ASI-2", binding.named_issue_id)
        self.assertEqual("ASI-1", binding.issue_id)

    def test_an_allocation_binding_must_name_its_own_issue(self) -> None:
        with self.assertRaisesRegex(AssuranceError, "claims to be the key allocated"):
            self._binding(identity_key="issue.asi-2", issue_id="ASI-1")

    def test_a_merge_binding_may_point_a_key_at_another_issue(self) -> None:
        binding = self._binding(
            identity_key="issue.asi-2", issue_id="ASI-1", source="merge"
        )
        self.assertEqual("issue.asi-2", binding.identity_key)

    def test_an_unknown_source_is_rejected(self) -> None:
        with self.assertRaises(AssuranceError):
            self._binding(source="renamed")

    def test_a_malformed_timestamp_is_rejected(self) -> None:
        with self.assertRaises(AssuranceError):
            self._binding(bound_at="2026-08-10")

    def test_an_unknown_payload_key_is_rejected(self) -> None:
        payload = self._binding().to_dict()
        payload["reason"] = "the agent decided so"

        with self.assertRaisesRegex(AssuranceError, "unknown key"):
            assurance_models_inbox.parse_issue_identity_binding(payload)

    def test_an_oversized_note_is_rejected(self) -> None:
        with self.assertRaises(AssuranceError):
            self._binding(note="n" * (assurance_models_vocab.MAX_TEXT_CHARS + 1))


class NormalizedFindingTextTests(unittest.TestCase):
    def test_a_localization_pointer_normalizes_to_the_file_it_names(self) -> None:
        self.assertEqual(
            "agent_session_loop/runner.py",
            assurance_models_findings.normalize_localization(
                "agent_session_loop/runner.py:8129"
            ),
        )
        self.assertEqual(
            "agent_session_loop/runner.py",
            assurance_models_findings.normalize_localization(
                "agent_session_loop/runner.py:8129:14"
            ),
        )
        self.assertEqual(
            "agent_session_loop/runner.py",
            assurance_models_findings.normalize_localization(
                "agent_session_loop/runner.py"
            ),
        )

    def test_two_sightings_of_one_file_normalize_together(self) -> None:
        self.assertEqual(
            assurance_models_findings.normalize_localization("skills/asl/SKILL.md:12"),
            assurance_models_findings.normalize_localization(
                "skills/asl/SKILL.md:4001"
            ),
        )

    def test_a_version_in_a_filename_survives_normalization(self) -> None:
        self.assertEqual(
            "agent_session_loop/state_v2.py",
            assurance_models_findings.normalize_localization(
                "agent_session_loop/state_v2.py"
            ),
        )

    def test_summary_tokens_are_lowercased_sorted_and_deduplicated(self) -> None:
        self.assertEqual(
            ("after", "budget", "exhausted", "ran", "retry"),
            assurance_models_findings.normalize_summary_tokens(
                "A retry ran after the budget was exhausted; the retry ran."
            ),
        )

    def test_rewording_a_summary_keeps_the_shared_tokens(self) -> None:
        first = set(
            assurance_models_findings.normalize_summary_tokens(
                "A retry ran after the budget was exhausted."
            )
        )
        second = set(
            assurance_models_findings.normalize_summary_tokens(
                "The retry budget was exhausted and a retry still ran."
            )
        )
        self.assertEqual({"budget", "exhausted", "retry", "ran"}, first & second)

    def test_a_negation_is_never_dropped_as_a_stopword(self) -> None:
        for word in ("not", "no", "never", "none", "nor", "without", "nothing"):
            with self.subTest(word=word):
                self.assertNotIn(word, assurance_models_findings.SUMMARY_STOPWORDS)
        self.assertNotEqual(
            assurance_models_findings.normalize_summary_tokens("The guard fired."),
            assurance_models_findings.normalize_summary_tokens(
                "The guard did not fire."
            ),
        )

    def test_punctuation_and_path_separators_split_summary_tokens(self) -> None:
        self.assertEqual(
            ("guard", "py", "runner", "scope"),
            assurance_models_findings.normalize_summary_tokens(
                "`runner.py` scope-guard"
            ),
        )

    def test_an_empty_summary_yields_no_tokens(self) -> None:
        self.assertEqual(
            (), assurance_models_findings.normalize_summary_tokens("   -- ,, ")
        )

    def test_an_invariant_key_splits_into_sorted_segments(self) -> None:
        self.assertEqual(
            ("budget", "respected", "retry"),
            assurance_models_findings.normalize_invariant_tokens(
                "retry.budget-respected"
            ),
        )

    def test_renaming_one_segment_keeps_the_others(self) -> None:
        self.assertEqual(
            {"budget", "retry"},
            set(
                assurance_models_findings.normalize_invariant_tokens(
                    "retry.budget-respected"
                )
            )
            & set(
                assurance_models_findings.normalize_invariant_tokens(
                    "retry.budget-clamped"
                )
            ),
        )

    def test_unrelated_invariants_share_no_segment(self) -> None:
        self.assertEqual(
            set(),
            set(
                assurance_models_findings.normalize_invariant_tokens(
                    "retry.budget-respected"
                )
            )
            & set(
                assurance_models_findings.normalize_invariant_tokens("scope.no-escape")
            ),
        )

    def test_no_stopword_is_dropped_from_an_invariant_key(self) -> None:
        self.assertEqual(
            ("do", "not", "the", "thing"),
            assurance_models_findings.normalize_invariant_tokens("the.do-not-thing"),
        )
