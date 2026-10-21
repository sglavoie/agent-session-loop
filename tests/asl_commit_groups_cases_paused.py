# mypy: ignore-errors
from .asl_commit_groups_support import *


class PausedWorkGroupScopeTests(unittest.TestCase):
    """The scope that separates a paused session's commit from its accepted one.

    A session can owe two commits over its lifetime: the work retained at a
    recoverable pause, and the work accepted after the repair passes. Both
    name the same session and the same repositories, so the scope is the only
    field left to tell their identities apart.
    """

    def make_group(
        self,
        scope: str = commit_groups.GROUP_SCOPE_SESSION_PAUSED,
        session_id: str | None = "12",
    ) -> dict[str, object]:
        return commit_groups.create_group(
            scope=scope,
            session_id=session_id,
            targets=[target("SVC"), target("OAK")],
            initial_heads={"SVC": "s0", "OAK": "o0"},
        )

    def reidentify(self, group: dict[str, object]) -> dict[str, object]:
        """Restamp identity and `group_id` after mutating a durable group.

        Without this a mutated field trips the identity or hash check first,
        and the scope rule under test never runs.
        """
        identity = {
            "scope": group["scope"],
            "session_id": group["session_id"],
            "ordered_aliases": group["ordered_aliases"],
        }
        group["identity"] = identity
        group["group_id"] = hashlib.sha256(
            json.dumps(identity, sort_keys=True, separators=(",", ":")).encode("ascii")
        ).hexdigest()
        return group

    def test_the_paused_scope_is_created_and_validated(self) -> None:
        group = self.make_group()

        identity = group["identity"]
        assert isinstance(identity, dict)
        self.assertEqual("session-paused", group["scope"])
        self.assertEqual("session-paused", identity["scope"])
        self.assertEqual(group, commit_groups.validate_group(group))

    def test_paused_and_accepted_groups_for_one_session_do_not_collide(self) -> None:
        # `_upsert_commit_group` keys on `group_id`, so an identity shared with
        # the accepted commit would overwrite that completed row in place.
        paused = self.make_group(commit_groups.GROUP_SCOPE_SESSION_PAUSED)
        accepted = self.make_group(commit_groups.GROUP_SCOPE_SESSION)

        self.assertEqual(paused["session_id"], accepted["session_id"])
        self.assertEqual(paused["ordered_aliases"], accepted["ordered_aliases"])
        self.assertNotEqual(paused["group_id"], accepted["group_id"])

    def test_a_paused_group_without_a_session_id_is_refused(self) -> None:
        with self.assertRaisesRegex(
            commit_groups.CommitGroupError, "exactly one session id"
        ):
            self.make_group(session_id=None)

        stripped = self.make_group()
        stripped["session"] = None
        stripped["session_id"] = None
        with self.assertRaisesRegex(
            commit_groups.CommitGroupError, "exactly one session id"
        ):
            commit_groups.validate_group(self.reidentify(stripped))

    def test_the_relaxed_invariant_still_refuses_the_old_mismatches(self) -> None:
        # Relaxing "scope == session" to "scope names a session" must widen the
        # rule by exactly one scope and no further.
        with self.assertRaisesRegex(
            commit_groups.CommitGroupError, "exactly one session id"
        ):
            self.make_group(commit_groups.GROUP_SCOPE_END)
        with self.assertRaisesRegex(
            commit_groups.CommitGroupError, "scope must be one of"
        ):
            self.make_group("session-resumed")

        unknown = self.make_group()
        unknown["scope"] = "session-resumed"
        with self.assertRaisesRegex(
            commit_groups.CommitGroupError, "scope must be one of"
        ):
            commit_groups.validate_group(self.reidentify(unknown))

    def test_a_carried_message_survives_the_round_trip_outside_identity(self) -> None:
        # The text a group commits verbatim has to outlive the process that
        # rendered it -- a later run recovering the group has no other route
        # back to it -- while staying out of the identity that names which
        # commit is owed.
        message = "chore: retain unverified work from session 12\n\nMarked.\n"
        carried = commit_groups.create_group(
            scope=commit_groups.GROUP_SCOPE_SESSION_PAUSED,
            session_id="12",
            targets=[target("SVC"), target("OAK")],
            initial_heads={"SVC": "s0", "OAK": "o0"},
            verbatim_message=message,
        )
        self.assertEqual(message, carried["verbatim_message"])
        self.assertEqual(carried, commit_groups.validate_group(carried))
        self.assertEqual(self.make_group()["group_id"], carried["group_id"])
        self.assertNotIn("verbatim_message", carried["identity"])

    def test_a_group_that_named_no_message_reads_as_generated(self) -> None:
        # Every group made before the field existed, and every accepted or
        # end-of-run group made after it, commits a generated message.
        self.assertIsNone(self.make_group()["verbatim_message"])
        legacy = self.make_group()
        del legacy["verbatim_message"]
        self.assertEqual(legacy, commit_groups.validate_group(legacy))

    def test_an_unusable_carried_message_is_refused(self) -> None:
        # A key that is present but not a committable message is evidence a
        # reader would otherwise pass to `git commit` as the message.
        for unusable in ("", "   ", 17, ["chore: x"]):
            with self.subTest(verbatim_message=unusable):
                group = self.make_group()
                group["verbatim_message"] = unusable
                with self.assertRaisesRegex(
                    commit_groups.CommitGroupError, "verbatim message is invalid"
                ):
                    commit_groups.validate_group(group)

    def test_loader_replays_a_paused_group(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state_path = root / "state.json"
            event_path = root / "events.jsonl"
            group = self.make_group()
            state_path.write_text(
                json.dumps({"commit_groups": [group]}), encoding="utf-8"
            )
            event_path.write_text(
                json.dumps(
                    {
                        "name": events.COMMIT_GROUP_STARTED_EVENT,
                        "payload": {"phase": "started", "group": group},
                    }
                )
                + "\n"
                + json.dumps(
                    {
                        "name": "commit_finished",
                        "payload": {
                            "group_id": group["group_id"],
                            "repository_key": "SVC",
                            "success": True,
                            "commit_sha": "s1",
                            "commit_index": 1,
                        },
                    }
                )
                + "\n",
                encoding="utf-8",
            )

            recovered = commit_groups.load_unresolved_group(
                state_path=state_path,
                event_path=event_path,
            )

        self.assertIsNotNone(recovered)
        assert recovered is not None
        self.assertEqual("session-paused", recovered["scope"])
        self.assertEqual(group["group_id"], recovered["group_id"])
        self.assertEqual(["OAK"], recovered["remaining_aliases"])


class CommitGroupScopeSelectionTests(unittest.TestCase):
    """Which scope `_run_commit_group` stamps on a group it creates."""

    def run_group(
        self, *, session_id: str | None, scope: str | None = None
    ) -> dict[str, object]:
        """Drive one empty group so only the scope decision is exercised.

        With no targets no commit subprocess is launched and no progress line
        is rendered, which is what keeps this test portable.
        """
        persisted: list[dict[str, object]] = []
        with mock.patch.object(
            commit_settlement,
            "_persist_commit_group",
            side_effect=lambda **kwargs: persisted.append(dict(kwargs["group"])),
        ):
            commit_settlement._run_commit_group(
                event_path=Path("events.jsonl"),
                payload={"commits": []},
                progress=mock.Mock(),
                verification_context=mock.Mock(),
                run_dir=Path("run"),
                run_id="r1",
                session_id=session_id,
                targets=[],
                generation=mock.Mock(),
                scope=scope,
            )
        return persisted[0]

    def test_scope_defaults_to_todays_derivation(self) -> None:
        self.assertEqual("session", self.run_group(session_id="12")["scope"])
        self.assertEqual("end", self.run_group(session_id=None)["scope"])

    def test_an_explicit_scope_is_stamped_on_the_group(self) -> None:
        group = self.run_group(
            session_id="12", scope=commit_groups.GROUP_SCOPE_SESSION_PAUSED
        )

        self.assertEqual("session-paused", group["scope"])
        self.assertEqual(
            commit_groups.create_group(
                scope=commit_groups.GROUP_SCOPE_SESSION_PAUSED,
                session_id="12",
                targets=[],
                initial_heads={},
            )["group_id"],
            group["group_id"],
        )
