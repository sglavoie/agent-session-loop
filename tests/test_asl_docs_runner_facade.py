"""Runner-facade docs regression tests."""

from __future__ import annotations

import re
import unittest

from agent_session_loop import (
    runner,
)
from tests.support import (
    REPO_ROOT,
)

#: The ASL operator guide moved out of the repository README; it carries the
#: operator-facing summary these tests used to read from `README.md`.
_MAINTAINED_PROSE_GLOBS = (
    "AGENTS.md",
    "README.md",
    "agent_session_loop/*.md",
    "agent_session_loop/*.py",
    "docs/*.md",
    "skills/asl/scripts/*.py",
    "tests/*.py",
)

#: `runner.py` in prose is a filename, never an attribute reference.
_RUNNER_FILENAME_SUFFIX = "py"

#: Matches `runner.<name>` and `agent_session_loop.runner.<name>` but not
#: `some_other_runner.<name>`: a dot may precede the word, another identifier
#: character may not.
_RUNNER_MENTION_RE = re.compile(r"(?<![A-Za-z0-9_])runner\.([A-Za-z_][A-Za-z0-9_]*)")

#: The `runner.<name>` spellings that survive the gate without resolving on the
#: facade, allowed by file *and* by name and by nothing else. Each is prose
#: deliberately *about* the extraction rather than a claim about where behaviour
#: lives now: `X` and `_x` are the placeholder the retired alias convention is
#: described with, and `mailbox` and `MAX_VERIFICATION_REPAIR_ATTEMPTS` are two
#: of the 226 aliases named as retired at the point they are said to be retired.
_HISTORICAL_RUNNER_MENTIONS: dict[str, frozenset[str]] = {
    "agent_session_loop/AGENTS.md": frozenset({"X"}),
    "agent_session_loop/README.md": frozenset({"X", "mailbox"}),
    "agent_session_loop/runner.py": frozenset({"X"}),
    "tests/test_asl_docs_runner_facade.py": frozenset({"X", "mailbox"}),
    "tests/test_asl_run_loop.py": frozenset({"_x"}),
    "tests/test_asl_session_verification.py": frozenset(
        {"MAX_VERIFICATION_REPAIR_ATTEMPTS"}
    ),
}


#: Present-tense verbs that make their subject the *owner* of behaviour. The
#: gate below reads them off the runner's filename, which is how a paragraph
#: goes on assigning work to a 68-line facade without ever spelling a
#: `runner.<name>` the other gate would catch. `is`, `imports`, `defines` and
#: `contains` are deliberately absent: each is a true statement about the
#: facade rather than a claim about where behaviour lives.
_OWNERSHIP_VERBS = (
    "appends",
    "builds",
    "calls",
    "constructs",
    "creates",
    "decides",
    "drives",
    "emits",
    "executes",
    "handles",
    "holds",
    "keeps",
    "manages",
    "owns",
    "performs",
    "publishes",
    "retains",
    "runs",
    "writes",
)

#: Verbs that name where something *rests*. Saying an effect "stays in" the
#: runner's own file is the same claim as making that file its owner, said from
#: the other end.
_RESTING_VERBS = ("belongs", "happens", "lives", "remains", "sits", "stays")

#: How maintained prose names the loop without naming a module. These are the
#: subjects the pre-extraction idioms below attach to.
_UNQUALIFIED_SUBJECTS = ("the loop", "the run loop", "the runner")

#: The pre-extraction predicates: each says the named collaborator has not been
#: given an owner yet. Every one of them was true while the extraction was
#: running and false once the collaborator landed somewhere, which is exactly
#: the sentence nobody goes back to rewrite.
_UNMOVED_PREDICATES = (
    "still builds",
    "still drives",
    "still holds",
    "still keeps",
    "still owns",
    "still writes",
)


def _phrase(*words: str) -> str:
    """Join regex fragments so a phrase matches across a wrapped line."""
    return r"\s+".join(words)


def _alternation(words: tuple[str, ...]) -> str:
    return "(?:" + "|".join(_phrase(*word.split(" ")) for word in words) + ")"


#: `runner.py`, with or without the backticks or double-backticks that markdown
#: and reStructuredText spell it in.
_RUNNER_FILE = r"`{0,2}runner\.py`{0,2}"

#: The ownership claims this gate rejects, each as a phrase pattern. Two are
#: filename-level -- the runner's own filename as the subject of an ownership
#: verb, and as the place behaviour rests -- and four are unqualified English,
#: naming no symbol at all. Together they cover the two shapes the
#: `runner.<name>` gate above cannot see.
_STALE_OWNERSHIP_PATTERNS = tuple(
    re.compile(pattern)
    for pattern in (
        _phrase(
            _RUNNER_FILE, r"(?:still\s+)?" + _alternation(_OWNERSHIP_VERBS) + r"\b"
        ),
        _phrase(r"\b" + _alternation(_RESTING_VERBS), "in", _RUNNER_FILE),
        _phrase(
            r"\b" + _alternation(_UNQUALIFIED_SUBJECTS),
            r"(?:that\s+)?" + _alternation(_UNMOVED_PREDICATES) + r"\b",
        ),
        _phrase(r"\bstill", "owned", "by", _alternation(_UNQUALIFIED_SUBJECTS) + r"\b"),
        _phrase(
            r"\b(?:has|have|had)", "not", "left", _alternation(_UNQUALIFIED_SUBJECTS)
        )
        + r"\b",
        _phrase(
            r"\bstill",
            _alternation(_RESTING_VERBS),
            "in",
            _alternation(_UNQUALIFIED_SUBJECTS),
        )
        + r"\b",
        # The runner named as a resting place needs no `still` to be a claim
        # about now, since the facade holds two entry points and nothing else.
        # `no longer` and `never` are the two ways prose says the opposite.
        r"(?<!no longer )(?<!never )"
        + _phrase(r"\b" + _alternation(_RESTING_VERBS), "in", "the", r"runner\b"),
    )
)

#: The stale ownership phrases a file is allowed to keep, by file *and* by the
#: exact phrase, so a new claim of a different shape still fails inside an
#: already-exempt file. Empty is the intended state: every phrase above is a
#: claim about the present, and prose deliberately about the extraction's
#: history says so in the past tense instead of earning a row here.
_HISTORICAL_OWNERSHIP_CLAIMS: dict[str, frozenset[str]] = {}


class RunnerFacadeDocsTests(unittest.TestCase):
    """Lock the end state of the runner extraction in the package README.

    The architecture section described `runner.py` as a module that keeps a
    re-export alias for every symbol an extraction moved out of it. Session 38
    ended both halves of that -- the runner is a facade, and the aliases are
    gone -- so the two sentences that promised an alias are pinned here as
    absences, and what replaced them is pinned as text. The alias promise was
    stated twice, in the `verification_transitions` paragraph and again in the
    `run_coordination` one, which is why the absence is asserted on the phrase
    rather than on either paragraph.

    Pinning sentences catches only the sentences somebody thought to pin, which
    is how prose across the package went on attributing `_commit_turn`,
    `_parse_retry_at`, `_terminal_blocker` and six more to a module that no
    longer defines any of them. The `runner.<name>` tests are the first
    mechanical half: every such spelling in maintained prose must resolve on
    the facade or be listed as history. The escape hatch is bounded from the
    other side by `tests/test_asl_runner_shrink.py`, which pins the facade's
    public surface, so widening what this gate allows means widening the runner.

    A spelling gate reads spellings, though, and the claims that outlived it
    named no symbol at all: the README went on saying the runner's *filename*
    built a `TurnExecutor` and kept every verification effect, and boundary
    docstrings went on describing collaborators as ones the loop had yet to
    give an owner. `_STALE_OWNERSHIP_PATTERNS` is the second mechanical half,
    matching the two shapes those took -- the filename as a subject or a
    resting place, and the unqualified pre-extraction idioms -- so a
    reintroduced claim fails whether or not it spells a symbol.
    """

    def _runner_mentions(self) -> dict[str, set[str]]:
        """Every `runner.<name>` spelling in maintained prose, by relative path."""
        mentions: dict[str, set[str]] = {}
        for pattern in _MAINTAINED_PROSE_GLOBS:
            for path in sorted(REPO_ROOT.glob(pattern)):
                names = {
                    match.group(1)
                    for match in _RUNNER_MENTION_RE.finditer(
                        path.read_text(encoding="utf-8")
                    )
                } - {_RUNNER_FILENAME_SUFFIX}
                if names:
                    mentions[path.relative_to(REPO_ROOT).as_posix()] = names
        return mentions

    def test_maintained_prose_names_no_retired_runner_symbol(self) -> None:
        stale: list[str] = []
        for relative, names in sorted(self._runner_mentions().items()):
            allowed = _HISTORICAL_RUNNER_MENTIONS.get(relative, frozenset())
            stale.extend(
                f"{relative}: runner.{name}"
                for name in sorted(names)
                if not hasattr(runner, name) and name not in allowed
            )

        self.assertEqual(
            [],
            stale,
            msg=(
                "maintained prose attributes a symbol to `runner.py` that the "
                "facade does not define; name the module that owns it, or add "
                "the mention to _HISTORICAL_RUNNER_MENTIONS if the sentence is "
                "deliberately about the extraction's history"
            ),
        )

    def test_the_historical_mention_allowlist_stays_earned(self) -> None:
        mentions = self._runner_mentions()

        for relative, names in sorted(_HISTORICAL_RUNNER_MENTIONS.items()):
            with self.subTest(path=relative):
                self.assertIn(
                    relative,
                    mentions,
                    msg=(
                        f"{relative} no longer mentions `runner.<name>` at all; "
                        "delete its _HISTORICAL_RUNNER_MENTIONS row"
                    ),
                )
                for name in sorted(names):
                    self.assertIn(
                        name,
                        mentions[relative],
                        msg=(
                            f"{relative} no longer mentions `runner.{name}`; "
                            "drop it from _HISTORICAL_RUNNER_MENTIONS so the "
                            "allowlist keeps naming only live exemptions"
                        ),
                    )
                    self.assertFalse(
                        hasattr(runner, name),
                        msg=(
                            f"`runner.{name}` resolves on the facade, so it is "
                            "the public surface rather than history and needs "
                            "no exemption"
                        ),
                    )

    def test_the_gate_rejects_a_reintroduced_runner_owner_reference(self) -> None:
        # The three shapes the stale references actually took: a bare private
        # name, a constant, and a fully qualified dotted path. Each sentence is
        # assembled rather than written out so this file does not trip its own
        # gate, and `qualifier` proves the pattern reads through the package
        # prefix while `other` proves it does not fire on a different module.
        module = "runner"
        for name, qualifier, other in (
            ("_commit_turn", "", "turn_execution"),
            ("TRUSTED_RUNNER_SIGNAL_SOURCES", "", "turn_execution"),
            ("classify_harness_failure", "agent_session_loop.", "harness_failures"),
        ):
            with self.subTest(name=name):
                sentence = (
                    f"the guards run before ``{qualifier}{module}.{name}``, "
                    f"unlike ``mock_{module}.{name}`` and ``{other}.{name}``"
                )
                self.assertEqual(
                    [name],
                    [m.group(1) for m in _RUNNER_MENTION_RE.finditer(sentence)],
                )
                self.assertFalse(hasattr(runner, name))

    def _ownership_claims(self) -> dict[str, list[str]]:
        """Every stale ownership phrase in maintained prose, by relative path.

        Whitespace is normalized first because these phrases wrap: the claim
        the README stated on one line, a module docstring states across two.
        """
        claims: dict[str, list[str]] = {}
        for pattern in _MAINTAINED_PROSE_GLOBS:
            for path in sorted(REPO_ROOT.glob(pattern)):
                text = " ".join(path.read_text(encoding="utf-8").split())
                found = sorted(
                    {
                        " ".join(match.group(0).split())
                        for stale in _STALE_OWNERSHIP_PATTERNS
                        for match in stale.finditer(text)
                    }
                )
                if found:
                    claims[path.relative_to(REPO_ROOT).as_posix()] = found
        return claims

    def test_maintained_prose_assigns_no_behaviour_to_the_runner(self) -> None:
        stale: list[str] = []
        for relative, phrases in sorted(self._ownership_claims().items()):
            allowed = _HISTORICAL_OWNERSHIP_CLAIMS.get(relative, frozenset())
            stale.extend(
                f"{relative}: {phrase}" for phrase in phrases if phrase not in allowed
            )

        self.assertEqual(
            [],
            stale,
            msg=(
                "maintained prose assigns behaviour to `runner.py` or to a "
                "collaborator it says has no owner yet; name the module that "
                "owns the work, or say in the past tense that the runner "
                "used to"
            ),
        )

    def test_the_historical_ownership_allowlist_stays_earned(self) -> None:
        claims = self._ownership_claims()

        for relative, phrases in sorted(_HISTORICAL_OWNERSHIP_CLAIMS.items()):
            with self.subTest(path=relative):
                for phrase in sorted(phrases):
                    self.assertIn(
                        phrase,
                        claims.get(relative, []),
                        msg=(
                            f"{relative} no longer says {phrase!r}; drop it "
                            "from _HISTORICAL_OWNERSHIP_CLAIMS so the "
                            "allowlist keeps naming only live exemptions"
                        ),
                    )

    def test_the_gate_rejects_a_reintroduced_ownership_claim(self) -> None:
        # The four shapes the surviving claims actually took, each rebuilt from
        # fragments so this file does not trip its own gate: the runner's
        # filename as the subject of an ownership verb, the same claim said
        # from the resting end, a collaborator the loop is said to hold, and
        # one said not to have moved yet.
        module = "runner"
        loop = "the loop"
        for claim, verb in (
            (f"`{module}.py` %s a `TurnExecutor` per turn", "builds"),
            (f"every effect %s in `{module}.py`", "stays"),
            (f"the two collaborators {loop} %s enter as seams", "still owns"),
            ("the one thing that %s the runner yet", "has not left"),
            (f"the dispatch %s the {module}", "lives in"),
        ):
            sentence = claim % verb
            with self.subTest(sentence=sentence):
                self.assertTrue(
                    any(stale.search(sentence) for stale in _STALE_OWNERSHIP_PATTERNS),
                    msg=f"the ownership gate does not reject: {sentence!r}",
                )

    def test_the_gate_accepts_the_facades_own_true_sentences(self) -> None:
        # The gate must not fire on what the README truthfully says about the
        # facade, or the only way to satisfy it would be to stop describing the
        # runner at all. Each of these is live prose in `agent_session_loop`.
        module = "runner"
        for sentence in (
            f"What is left of `{module}.py` is a facade",
            f"`run_errors.py` imports nothing from the package and `{module}.py` "
            "imports `RunError` back from it",
            f"`{module}.py` defines 2 public module-level names",
            f"Session 37 moved it out of `{module}.py` whole rather than in pieces",
            f"the {module} keeps no module-level alias for the moved symbols",
            f"the guard no longer lives in {'the loop'}",
            f"the guard no longer lives in the {module}",
        ):
            with self.subTest(sentence=sentence):
                self.assertEqual(
                    [],
                    [
                        match.group(0)
                        for stale in _STALE_OWNERSHIP_PATTERNS
                        for match in stale.finditer(sentence)
                    ],
                    msg=f"the ownership gate falsely rejects: {sentence!r}",
                )

    def _normalized_readme(self) -> str:
        readme = (REPO_ROOT / "agent_session_loop" / "README.md").read_text(
            encoding="utf-8"
        )
        return " ".join(readme.split())

    def test_readme_describes_the_runner_as_a_facade(self) -> None:
        normalized = self._normalized_readme()

        for marker in (
            "What is left of `runner.py` is a facade",
            "a docstring, its imports, and two one-line delegations",
            "`run_from_args`, which supplies `run_loop.run_locked` as "
            "`run_entry`'s `LockedRun` seam, and `doctor_from_args`, which "
            "forwards to `run_doctor`",
            "`cli.py` reaches them through `handler_ref` strings rather than "
            "through an import",
            "nothing else here is a boundary: the boundaries are the modules "
            "above, each owning its own behaviour, its own tests, and its own "
            "patch targets",
        ):
            self.assertIn(
                marker,
                normalized,
                msg=f"asl README omits runner-facade marker: {marker!r}",
            )

    def test_readme_retires_the_alias_convention_with_its_reason(self) -> None:
        normalized = self._normalized_readme()

        for marker in (
            "The alias convention is retired with the bulk",
            "by Session 37 there were 226 of them, and they were the reason a "
            "runner holding no logic at all was still 516 lines",
            "a re-export table is bulk no boundary owns",
            "a name reachable at two spellings is a name whose owner a reader "
            "has to go and look up",
            '`mock.patch.object(runner, "X")` degrades into a silent no-op the '
            "moment no code reads `runner.X`, so a test can keep passing while "
            "asserting nothing",
            "Session 38 deleted all 226 and rewrote every reference to name "
            "the module that owns the symbol",
            "`RunError` is the single deliberate exception and it is an "
            "`import` rather than an alias",
            "`runner.RunError` has to stay the *same class object* "
            "`run_errors.RunError` raises",
            "`RUNNER_ALIAS_FLOOR` is kept empty rather than deleted so its two "
            "rules read as a permanent no-new-aliases gate",
        ):
            self.assertIn(
                marker,
                normalized,
                msg=f"asl README omits alias-retirement marker: {marker!r}",
            )

    def test_readme_no_longer_promises_a_runner_alias(self) -> None:
        normalized = self._normalized_readme()

        self.assertNotIn(
            "the runner keeps a module-level alias for each moved symbol",
            normalized,
            msg=(
                "the README still promises a runner re-export alias; Session "
                "38 retired all 226, so no extraction leaves one behind"
            ),
        )
        self.assertNotIn(
            "it still imports `mailbox` for the callers that stub `runner.mailbox`",
            normalized,
            msg=(
                "the README still claims the runner imports `mailbox` for "
                "`runner.mailbox` stubs; those callers now patch `mailbox`"
            ),
        )


if __name__ == "__main__":
    unittest.main()
