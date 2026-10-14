"""Mutating fake review harness for the ``--review-harness`` lifecycle tests."""

from __future__ import annotations

import json
import stat
from pathlib import Path

from agent_session_loop import plans_content_hash, plans_models, plans_readiness
from tests.review_lifecycle_base import RunReviewHarnessLifecycleBase
from tests.support import (
    _structured_review_finding,
)


class MutatingFakeClaudeMixin(RunReviewHarnessLifecycleBase):
    """Installs a fake claude review harness that mutates the worktree."""

    def _write_pycache_review_plan(self) -> None:
        self._write_plan(("complete",), readiness=None)
        self._commit_gitignore("__pycache__/\n")
        self._write_pending_review_session()
        plans_readiness.write_readiness_metadata(
            self.plan_dir,
            plans_models.ReadinessMetadata(
                state="loop-ready",
                readiness_sha256=plans_content_hash.stable_readiness_sha256(
                    self.plan_dir
                ),
                checked_at="2026-06-19T00:00:00Z",
            ),
        )
        self._install_mutating_fake_claude(("agent_session_loop/__pycache__/x.pyc",))

    def _install_mutating_fake_claude(
        self,
        mutate_paths: tuple[str, ...] = ("README.md",),
        *,
        invalid_first_attempt: bool = False,
    ) -> Path:
        """Replace the claude fake harness with one that mutates the cwd repo.

        The replacement marks the first pending session complete, pre-stamps
        ``final-review.md`` so the generated contract passes, and writes
        ``mutated by review\\n`` to every path in ``mutate_paths`` (relative to
        the cwd). The mutation guard should detect those changes and pause the
        run unless every path is excluded.

        ``invalid_first_attempt`` leaves the worktree alone on the first review
        turn and emits an artifact that fails deep validation, so the mutation
        only lands on the in-run retry turn.
        """
        invalid_artifact = (
            json.dumps(
                {
                    "version": 2,
                    "outcome": "findings",
                    "findings": [
                        _structured_review_finding(
                            verification_checks=[
                                {"type": "bogus", "name": "not-a-check"}
                            ]
                        )
                    ],
                }
            )
            + "\n"
        )
        script = self.fake_claude
        script.write_text(
            "\n".join(
                [
                    "#!/usr/bin/env python3",
                    "import re",
                    "import sys",
                    "from pathlib import Path",
                    "",
                    "plan_dir = None",
                    "last_message = None",
                    "for index, arg in enumerate(sys.argv):",
                    "    if arg == '--add-dir' and index + 1 < len(sys.argv):",
                    "        plan_dir = Path(sys.argv[index + 1])",
                    "    if arg == '-o' and index + 1 < len(sys.argv):",
                    "        last_message = Path(sys.argv[index + 1])",
                    "if plan_dir is None:",
                    "    print('missing --add-dir', file=sys.stderr)",
                    "    sys.exit(2)",
                    "",
                    "retry_notes = sum(",
                    "    path.read_text(encoding='utf-8').count(",
                    "        'review verdict validation failed'",
                    "    )",
                    "    for path in plan_dir.glob('session-*.md')",
                    ")",
                    f"emit_invalid = {invalid_first_attempt!r} and retry_notes == 0",
                    "",
                    "# Mutate files inside cwd to trigger the guard.",
                    "if not emit_invalid:",
                    f"    for rel in {tuple(mutate_paths)!r}:",
                    "        mutated = Path.cwd() / rel",
                    "        mutated.parent.mkdir(parents=True, exist_ok=True)",
                    "        mutated.write_text('mutated by review\\n', "
                    "encoding='utf-8')",
                    "",
                    "session_re = re.compile(",
                    "    r'^session-([1-9][0-9]*(?:\\.[1-9][0-9]*)*)\\.md$'",
                    ")",
                    "def sort_key(path):",
                    "    match = session_re.match(path.name)",
                    "    if match is None:",
                    "        return ()",
                    "    return tuple(int(c) for c in match.group(1).split('.'))",
                    "pending = []",
                    "for path in sorted(plan_dir.glob('session-*.md'), key=sort_key):",
                    "    if session_re.match(path.name) is None:",
                    "        continue",
                    "    text = path.read_text(encoding='utf-8')",
                    "    if '- **Status**: pending' in text:",
                    "        pending.append(path)",
                    "if pending:",
                    "    target = pending[0]",
                    "    text = target.read_text(encoding='utf-8')",
                    "    text = text.replace(",
                    "        '- **Status**: pending',",
                    "        '- **Status**: complete',",
                    "        1,",
                    "    )",
                    "    target.write_text(text, encoding='utf-8')",
                    "",
                    "if emit_invalid:",
                    "    review_body = (",
                    "        '## Findings\\n'",
                    "        '- High issue in agent_session_loop/runner.py.\\n'",
                    "        '- Context in tests/test_agent_session_loop.py.\\n'",
                    "        '- Context in docs/agent-session-loop.md.\\n'",
                    "        'Status: 1 Findings (1 high, 0 med, 0 low)\\n'",
                    "    )",
                    f"    review_json = {invalid_artifact!r}",
                    "else:",
                    "    review_body = (",
                    "        '## Findings\\n'",
                    "        '- Reviewed agent_session_loop/runner.py for mutation "
                    "guard behavior.\\n'",
                    "        '- Checked tests/test_agent_session_loop.py for review "
                    "lifecycle coverage.\\n'",
                    "        '- Compared docs/agent-session-loop.md for operator "
                    "documentation impact.\\n'",
                    "        'Status: All clear, no findings\\n'",
                    "    )",
                    "    review_json = (",
                    '        \'{"version": 2, "outcome": "all-clear", '
                    '"findings": []}\\n\'',
                    "    )",
                    "(plan_dir / 'final-review.md').write_text(",
                    "    review_body, encoding='utf-8',",
                    ")",
                    "(plan_dir / 'final-review.json').write_text(",
                    "    review_json, encoding='utf-8',",
                    ")",
                    "",
                    "if last_message is not None:",
                    "    last_message.parent.mkdir(parents=True, exist_ok=True)",
                    "    last_message.write_text(",
                    "        'Runner status: complete\\n', encoding='utf-8',",
                    "    )",
                    "",
                    "sys.exit(0)",
                    "",
                ]
            ),
            encoding="utf-8",
        )
        script.chmod(script.stat().st_mode | stat.S_IXUSR)
        return script
