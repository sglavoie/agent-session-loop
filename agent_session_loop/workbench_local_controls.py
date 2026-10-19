from .workbench_local_core import *


class _ControlOperations(_LocalProviderOperations):
    def _dispatch_control(
        self, kind: str, call: Call, **kwargs: Any
    ) -> workbench_controls.ControlOutcome:
        """Queue one control through the service both surfaces share.

        The service carries the same best-effort wakeup the CLI producer sends,
        so this surface is not the slower way to reach a live runner, and it
        reads the run the same way every other reader does, so the activity an
        abort reports here is not a second parse of the lock.
        """
        return workbench_controls.dispatch_control(
            call.require_string("plan_dir"),
            kind,
            state_dir=self.config.state_dir,
            command_id=call.optional_string("command_id"),
            **kwargs,
        )

    @staticmethod
    def _queued(outcome: workbench_controls.ControlOutcome) -> Response:
        """Answer a control whose only promise is that it was written down."""
        return Response.success(
            202,
            reason="accepted",
            accepted=True,
            queued_command=outcome.command.to_payload(),
            signal=outcome.dispatch.signal.to_payload(),
        )

    def _handle_control_stop_after_current(self, call: Call) -> Response:
        return self._queued(
            self._dispatch_control(portable_controls.CONTROL_STOP_AFTER_CURRENT, call)
        )

    def _handle_control_select_harness(self, call: Call) -> Response:
        return self._queued(
            self._dispatch_control(
                portable_controls.CONTROL_SELECT_HARNESS,
                call,
                harness=call.require_string("harness"),
            )
        )

    def _handle_control_abort(self, call: Call) -> Response:
        outcome = self._dispatch_control(
            portable_controls.CONTROL_ABORT,
            call,
            # Passed through untouched: ``append_command`` is the only validator
            # of grace and reason, so this surface cannot accept a window the
            # CLI would reject or reject one it would accept.
            grace_seconds=call.body.get("grace_seconds"),
            reason=call.body.get("reason"),
        )
        queued = outcome.command
        activity = outcome.activity
        return Response.success(
            202,
            reason="accepted",
            accepted=True,
            run_active=activity.runner_present,
            turn_active=activity.turn_active,
            grace_seconds=queued.grace_seconds,
            # The CLI calls this field `reason`; here the top-level `reason` is
            # the HTTP response status, so the abort's own reason keeps a
            # distinct name.
            abort_reason=queued.reason,
            note=activity.note,
            queued_command=queued.to_payload(),
            signal=outcome.dispatch.signal.to_payload(),
        )

    # -- Redirect -----------------------------------------------------------

    @staticmethod
    def _redirect_payload(
        submission: workbench_controls.RedirectSubmission,
    ) -> _JSONDict:
        """Name the shared submission's facts the way this API already does.

        Every field comes from the service, which re-read the journal after the
        submission rather than building an answer from the outcome in hand, so
        the status a browser renders and the status
        ``asl control redirect-status`` computes next are one projection of the
        same bytes.
        """
        return {
            # Null exactly when nothing was persisted. Echoing the client's own
            # id for a refusal that never reached the journal would invite it to
            # ask after a correction that does not exist.
            "operation_id": submission.operation_id,
            "plan_dir": str(submission.facts.plan_dir),
            "run_id": submission.facts.run_id,
            # Null on a replay: that call submitted nothing, so it has no
            # submission outcome to report and `operation` carries the truth.
            "outcome": submission.outcome,
            # The CLI and the bridge both call this `reason`; here the top-level
            # `reason` is the HTTP response status, so a refusal keeps its own
            # name.
            "refusal": submission.reason,
            "target": submission.target,
            "waited_seconds": round(submission.waited_seconds, 3),
            "replayed": submission.replayed,
            "operation": submission.operation_payload(),
            "journal_complete": submission.journal_complete,
        }

    def _handle_control_redirect(self, call: Call) -> Response:
        """Submit one urgent correction against the turn this run is on now.

        Replay is served out of the redirect journal before the freshness proof
        is checked, and that ordering is the whole reason a browser may retry
        this request. A correction that landed moves the plan, which moves the
        action token, which would make the retry of a request whose response was
        lost look like a stale second correction. Asking the journal first makes
        the ``operation_id`` the client already holds the answer.

        What is *not* short-circuited is the request itself. The body is read
        and compared against the record that id already names, because a retry
        is only a retry when it carries the same correction: an operator whose
        response was lost still has an editable textarea, and reporting their
        edited words as a successful replay of the words the worker actually got
        is the one falsehood this path exists to avoid.
        """
        try:
            submission = workbench_controls.submit_correction(
                call.require_string("plan_dir"),
                operation_id=call.require_string("operation_id"),
                text=call.body.get("text"),
                state_dir=self.config.state_dir,
                expected_target_raw=call.body.get("expected_target"),
                wait_seconds_raw=call.body.get("wait_seconds"),
                action_token=call.optional_string("action_token"),
                run_id=call.optional_string("run_id"),
            )
        except workbench_controls.RedirectConflict as error:
            # Nothing was written, and nothing about the recorded request
            # changed. 409 rather than 400 because the body is well formed:
            # what it contradicts is a durable record, and the client resolves
            # that by reconciling the id, not by fixing a field.
            raise HttpRefusal(
                409,
                error.reason,
                error.message,
                details={
                    "plan_dir": str(error.plan_dir),
                    "operation_id": error.operation_id,
                    "conflict": error.conflict,
                    "redirect": self._redirect_payload(error.submission),
                },
                extra={"retryable": False},
            ) from error
        receipt = self._redirect_payload(submission)
        if submission.replayed:
            # 200 rather than 202: this call accepted nothing, it recognized a
            # request that was already accepted once.
            return Response.success(reason="ok", redirect=receipt)
        if not submission.persisted:
            # Nothing reached the journal, so there is nothing to ask after
            # later. Reporting this as accepted is the one falsehood the whole
            # redirect path is built to avoid.
            raise HttpRefusal(
                409,
                submission.reason or redirect_admission.REFUSE_NO_ACTIVE_TURN,
                f"redirect was refused: {submission.reason}",
                details={
                    "plan_dir": str(submission.facts.plan_dir),
                    "redirect": receipt,
                },
                extra={
                    "retryable": submission.reason
                    in redirect_admission.UNAVAILABLE_REASONS
                },
            )
        return Response.success(
            202,
            reason="accepted",
            # Persisted, which is what this surface can promise. Whether the
            # runner took it is `redirect.operation.stage`, and an
            # `unavailable` or `uncertain` outcome says so there.
            accepted=submission.accepted,
            redirect=receipt,
        )

    def _handle_redirect_status(self, call: Call) -> Response:
        """Project this plan's durable redirect state, writing nothing.

        A read, and unauthenticated like every other local read, because the
        client most in need of it is the one that just lost its own record of
        what it submitted. Every field is derived through :mod:`.redirect_cli`,
        so the stage a browser renders and the stage an operator reads out of
        ``asl control redirect-status`` are one derivation of the same facts.
        """
        view = workbench_controls.status_view(
            call.require_query("plan_dir"),
            state_dir=self.config.state_dir,
            operation_ids=call.query_values("operation_id"),
        )
        facts = view.facts
        report = view.report
        live_target = view.live_target
        operations = view.operation_payloads()
        return Response.success(
            reason="ok",
            plan_dir=str(facts.plan_dir),
            run_id=facts.run_id,
            journal_path=report.journal_path,
            owner=report.owner.liveness,
            owner_run_id=report.owner.run_id,
            live_target=(live_target.to_payload() if live_target is not None else None),
            operations=operations,
            uncertain_operation_ids=[
                operation["operation_id"]
                for operation in operations
                if operation["stage"] == redirect_cli.STAGE_UNCERTAIN
            ],
            # `journal_complete` is the honesty flag and `truncated` is its
            # projection counterpart: a torn journal means a record may be
            # missing from the source, a full window that one is missing here.
            journal_complete=report.complete,
            torn_tail=report.torn_tail,
            unreadable_lines=report.unreadable_lines,
            truncated=view.truncated,
            checked_at=report.checked_at,
            conversations=report.conversations.to_payload(),
            # The freshness proof a redirect must carry back. Handed out by the
            # read so a composer never has to guess at one.
            action_token=intervention.action_token(facts),
            permitted_actions=list(intervention.permitted_actions(facts)),
            disabled_reasons=intervention.disabled_reasons(facts),
        )

    def _handle_conversation_recovery(self, call: Call) -> Response:
        """Report which recovery a pause is waiting on, and how to authorize it.

        A read, and deliberately only a read. Performing the recovery here would
        persist the operator's one-use authorization in the one place it must
        never live: a request a browser can replay. The pause-to-decision mapping
        and both commands come from :mod:`.intervention` and
        :mod:`.redirect_cli`, so this response, the recovery card, and
        ``asl control recover-conversation`` cannot hand an operator three
        different answers about the same pause.
        """
        view = workbench_controls.recovery_view(
            call.require_query("plan_dir"), state_dir=self.config.state_dir
        )
        classified = view.decision
        return Response.success(
            reason="ok",
            plan_dir=str(view.facts.plan_dir),
            run_id=view.facts.run_id,
            recovery_required=classified.required,
            # Reported whether or not it maps to a decision: "paused, and no
            # manual recovery is what it is waiting on" is an answer, while an
            # unexplained null is a second question.
            pause_reason=classified.pause_reason,
            decision=classified.decision,
            auto_resume=classified.auto_resume,
            authorized_by=classified.authorized_by,
            inspect_command=view.inspect_command,
            authorization_command=view.authorization_command,
            # Both describe the authorization command, so both are false and
            # null where there is none. `predecessor-not-stopped` has no
            # authorization at all, and telling an operator its absent
            # authorization is one-use would read as an invitation to find it.
            one_use=view.one_use,
            note=view.note,
            conversations=view.report.conversations.to_payload(),
            # The corrections a fresh start would still carry. Reported here so
            # an operator authorizing one sees what it inherits rather than
            # discovering it in the successor's first prompt.
            retained_operations=view.retained_payloads(),
        )
