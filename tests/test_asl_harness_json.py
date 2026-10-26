"""Decoding a harness's JSON stream lines into writable text."""

from __future__ import annotations

import unittest

from agent_session_loop import harness_claude, harness_json

ASL_TEST_PORTABLE = True


class DecodeHarnessLineTests(unittest.TestCase):
    """A decoded line holds only text a strict UTF-8 write accepts."""

    def test_a_lone_surrogate_anywhere_becomes_a_replacement_character(self) -> None:
        line = '{"a\\udc00": ["cut \\ud83d", {"b": "\\uDE00 end"}], "n": 1}'

        decoded = harness_json.decode_harness_line(line)

        self.assertEqual(
            {"a\ufffd": ["cut \ufffd", {"b": "\ufffd end"}], "n": 1}, decoded
        )

    def test_a_surrogate_pair_is_kept_as_the_character_it_spells(self) -> None:
        self.assertEqual(
            {"text": "hi \U0001f600"},
            harness_json.decode_harness_line('{"text": "hi \\ud83d\\ude00"}'),
        )

    def test_pathological_nesting_reads_as_a_malformed_line(self) -> None:
        with self.assertRaises(ValueError):
            harness_json.decode_harness_line("[" * 1_000_000)


class ClaudeStreamDecodingTests(unittest.TestCase):
    """The Claude renderer survives lines its decoder used to pass through."""

    def test_a_lone_surrogate_renders_as_writable_text(self) -> None:
        processor = harness_claude._ClaudeStreamJsonOutputProcessor()

        rendered = processor.process_chunk(
            "stdout", '{"type": "system", "subtype": "init", "model": "op\\ud83d"}\n'
        )

        rendered.encode("utf-8")
        self.assertEqual("op\ufffd", processor.observed_model)

    def test_an_over_deep_line_is_passed_through_as_plain_text(self) -> None:
        processor = harness_claude._ClaudeStreamJsonOutputProcessor()
        line = "[" * 1_000_000

        self.assertEqual(f"{line}\n", processor.process_chunk("stdout", f"{line}\n"))


if __name__ == "__main__":
    unittest.main()
