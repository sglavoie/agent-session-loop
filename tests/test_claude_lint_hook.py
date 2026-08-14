from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
HOOK_SCRIPT = REPO_ROOT / "bin/.local/bin/claude-lint-hook"


@unittest.skipUnless(shutil.which("ruff"), "ruff is not installed")
class ClaudeLintHookTests(unittest.TestCase):
    def run_hook(self, filename: str, body: str) -> subprocess.CompletedProcess[str]:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / filename
            path.write_text(body, encoding="utf-8")
            return self.invoke(json.dumps({"tool_input": {"file_path": str(path)}}))

    def invoke(self, payload: str) -> subprocess.CompletedProcess[str]:
        # The repo root is the cwd ruff resolves its rule selection from, so the
        # committed select list applies even though the edited file is in /tmp.
        return subprocess.run(
            [str(HOOK_SCRIPT)],
            cwd=REPO_ROOT,
            input=payload,
            text=True,
            capture_output=True,
            check=False,
        )

    def test_blocks_malformed_python_syntax(self) -> None:
        completed = self.run_hook("syntax.py", "def broken(:\n    pass\n")

        self.assertEqual(completed.returncode, 2)
        self.assertIn("invalid-syntax", completed.stderr)

    def test_blocks_undefined_name(self) -> None:
        completed = self.run_hook("undefined.py", "def f():\n    return missing\n")

        self.assertEqual(completed.returncode, 2)
        self.assertIn("F821", completed.stderr)

    def test_clean_file_is_silent(self) -> None:
        completed = self.run_hook("clean.py", "def f():\n    return 1\n")

        self.assertEqual(completed.returncode, 0)
        self.assertEqual(completed.stdout, "")
        self.assertEqual(completed.stderr, "")

    def test_style_finding_reports_without_blocking(self) -> None:
        completed = self.run_hook("style.py", f'VALUE = "{"x" * 120}"\n')

        self.assertEqual(completed.returncode, 0)
        self.assertIn("E501", completed.stdout)
        self.assertEqual(completed.stderr, "")

    def test_non_python_path_is_ignored(self) -> None:
        completed = self.run_hook("note.md", "def broken(:\n")

        self.assertEqual(completed.returncode, 0)
        self.assertEqual(completed.stdout, "")

    def test_malformed_payload_fails_open(self) -> None:
        completed = self.invoke("not json")

        self.assertEqual(completed.returncode, 0)


if __name__ == "__main__":
    unittest.main()
