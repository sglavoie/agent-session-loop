from .workbench_interactions_support import *


class InteractionContractTests(unittest.TestCase):
    """What the source has to be true of, before any of it is clicked."""

    def test_every_control_in_the_markup_names_an_action_this_app_knows(
        self,
    ) -> None:
        # A button wired to a name nothing answers for is a button that cannot
        # be greyed out, cannot be guarded against a second click, and leaves
        # no receipt. It would look identical to a working one.
        markup = INDEX_HTML.read_text(encoding="utf-8")
        declared = _declared_actions()
        used = set(re.findall(r'data-operation-action="([^"]+)"', markup))
        self.assertTrue(used, "index.html must mark its mutating controls")
        self.assertEqual(
            set(),
            used - set(declared),
            "a control names an action with no entry in ACTIONS",
        )

    def test_every_action_names_an_operation_the_service_publishes(self) -> None:
        # The whole availability mechanism rests on this: the word the table
        # uses is the word `/api/capabilities` answers about. A typo here would
        # mean a control that is *always* enabled, because an unknown operation
        # is reported available.
        declared = _declared_actions()
        self.assertGreaterEqual(
            len(declared), 14, "the ACTIONS table parsed as suspiciously small"
        )
        for name, fields in declared.items():
            with self.subTest(action=name):
                self.assertIn(
                    fields.get("operation"),
                    workbench_http.OPERATIONS,
                    "ACTIONS names an operation the HTTP surface does not have",
                )
                self.assertTrue(
                    fields.get("subject"),
                    "every action needs a subject to be described by",
                )

    def test_an_action_with_a_precondition_carries_its_own_explanation(
        self,
    ) -> None:
        # "Disabled" is not an explanation. The three preconditions are
        # different facts about the run -- it is live, it is idle, it is
        # waiting for you -- and each has to say which one failed.
        for name, fields in _declared_actions().items():
            with self.subTest(action=name):
                if fields.get("requires"):
                    self.assertTrue(
                        fields.get("stale"),
                        "an action with a precondition needs a stale sentence",
                    )

    def test_every_mutation_goes_through_the_one_guarded_path(self) -> None:
        # One submit path is what makes the double-submit guard, the receipt,
        # the focus restoration and the capability re-read universal rather
        # than something each caller remembers. The exemption is named, so a
        # second one has to be argued for rather than added.
        sources = {
            path.name: path.read_text(encoding="utf-8")
            for path in sorted(MODULES_ROOT.glob("*.js"))
            if path.name not in {"api.js", "operations.js"}
        }
        corpus = "\n".join(sources.values())
        guarded_specs = "\n".join(
            re.findall(r"runMutation\(\{(.*?)\n\s*\}\);", corpus, re.DOTALL)
        )
        for route in workbench_http.ROUTES:
            if not route.mutating or route.path in BESPOKE_MUTATIONS:
                continue
            if f'"{route.path}"' not in corpus:
                # Not every route the surface offers is reachable from a
                # control; `/api/plan/delete` is the pre-archive spelling the
                # shell no longer calls. Absence is fine, a second path is not.
                continue
            with self.subTest(route=route.path):
                self.assertIn(
                    f'"{route.path}"',
                    guarded_specs,
                    "a mutating route must be submitted as a runMutation spec",
                )
        # And never as a plain read. `apiRequest` returns a parsed body with no
        # outcome attached, so a mutation sent through it cannot tell a result
        # from a 202 -- which is exactly the overclaim this session removed.
        for name, source in sources.items():
            for target in re.findall(r'apiRequest\(\s*"([^"]+)"', source):
                with self.subTest(module=name, target=target):
                    self.assertNotIn(
                        target,
                        {
                            route.path
                            for route in workbench_http.ROUTES
                            if route.mutating
                        }
                        - BESPOKE_MUTATIONS,
                        "a mutating route may not be sent as an unclassified read",
                    )

    def test_only_the_shared_path_and_the_redirect_composer_submit(self) -> None:
        # `submit` is the classifier that separates a result from a recorded
        # 202 from an unreachable link. A module calling it directly would be a
        # module deciding for itself what an answer means.
        for path in sorted(MODULES_ROOT.glob("*.js")):
            if path.name in {"api.js", "operations.js"}:
                continue
            with self.subTest(module=path.name):
                self.assertNotIn(
                    "\n  submit,\n",
                    path.read_text(encoding="utf-8"),
                    "only operations.js may classify a submission",
                )

    def test_a_recovery_card_that_acts_is_the_same_action_as_the_bar(
        self,
    ) -> None:
        # The Recovery Center offers a second door onto two of the controls.
        # A door that skipped the availability pass would be a button offered
        # to someone who has just been told something went wrong, refused only
        # after they pressed it -- the worst place to make that mistake.
        source = _module_source("recovery.js")
        declared = _declared_actions()
        table = re.search(
            r"const RECOVERY_CARD_ACTIONS = \{(.*?)\n\};", source, re.DOTALL
        )
        self.assertIsNotNone(table, "recovery cards must name the actions they are")
        assert table is not None
        for name in set(re.findall(r':\s*"([a-z-]+)"', table.group(1))):
            with self.subTest(action=name):
                self.assertIn(name, declared)
        self.assertIn('setAttribute("data-operation-action", name)', source)
        self.assertIn("applyActionAvailability(host)", source)

    def test_a_stored_receipt_carries_an_identity_and_no_prose(self) -> None:
        # The privacy promise and the recovery promise pull in opposite
        # directions, and this is where they are reconciled: the id is written
        # down because a reload cannot reconstruct it, and nothing else is,
        # because a reload losing the words is the point.
        source = OPERATIONS_MODULE.read_text(encoding="utf-8")
        receipt = re.search(r"const receipt = \{(.*?)\n  \};", source, re.DOTALL)
        self.assertIsNotNone(receipt, "rememberReceipt must build one flat record")
        assert receipt is not None
        fields = set(re.findall(r"\n    ([a-z_]+):", receipt.group(1)))
        self.assertEqual(
            RECEIPT_FIELDS,
            fields,
            "a receipt may carry an identity and an outcome, and no prose",
        )

    def test_receipts_are_the_only_thing_written_to_session_storage_here(
        self,
    ) -> None:
        source = OPERATIONS_MODULE.read_text(encoding="utf-8")
        keys = set(re.findall(r"sessionStorage\.setItem\(\s*([A-Z_]+)", source))
        self.assertEqual({"OPERATION_RECEIPTS_STORAGE_KEY"}, keys)
        self.assertNotIn("localStorage", source)

    def test_the_browser_harness_names_the_archive_this_fixture_seeds(
        self,
    ) -> None:
        # The Playwright spec asserts against archive ids by name. They come
        # from one seeder, so a fixture that renamed an entry would otherwise
        # fail there as a mysterious missing row rather than here as a drift.
        manifest = json.loads(
            (REPO_ROOT / "tests" / "workbench_browser" / "scenarios.json").read_text(
                encoding="utf-8"
            )
        )
        archive = manifest["archive"]
        expired, retained = SHELL_ARCHIVE_ENTRIES
        self.assertEqual(expired["archive_id"], archive["expired"]["archive_id"])
        self.assertEqual(retained["archive_id"], archive["retained"]["archive_id"])
        self.assertEqual(len(SHELL_ARCHIVE_ENTRIES), archive["entry_count"])
        self.assertEqual(1, archive["expired_count"])

    def test_the_interface_has_somewhere_to_draw_an_open_operation(self) -> None:
        # A receipt with nothing to render into is a receipt nobody reads. Both
        # panels are asserted as markup rather than as behaviour because the
        # browser suite cannot run everywhere and an absent panel would make
        # every recovery assertion below vacuously pass there.
        markup = INDEX_HTML.read_text(encoding="utf-8")
        for element_id in (
            "wb-operations-panel",
            "wb-operations-list",
            "wb-archive-review",
            "wb-archive-review-list",
            "wb-archive-review-ack",
        ):
            with self.subTest(element=element_id):
                self.assertIn(f'id="{element_id}"', markup)
