from .workbench_local_core import *


class _LaunchOperations(_LocalProviderOperations):
    def _handle_launch(self, call: Call) -> Response:
        return self._spawn_run(call, reply_text=None)

    def _handle_reply(self, call: Call) -> Response:
        # Read before the guard is taken so a request with no reply at all is
        # refused as malformed rather than counted as a launch attempt.
        return self._spawn_run(call, reply_text=call.require_string("reply_text"))

    def _handle_question_reply(self, call: Call) -> Response:
        """Answer a live mailbox question without spawning another run."""
        try:
            outcome = workbench_actions.reply_to_question(
                call.require_string("plan_dir"),
                text=call.require_string("reply_text"),
                operation_id=call.bounded_id(
                    "operation_id", maximum=MAX_LAUNCH_ID_CHARS
                ),
                state_dir=self.config.state_dir,
                action_token=call.optional_string("action_token"),
                question_id=call.optional_string("question_id"),
                run_id=call.optional_string("run_id"),
            )
        except OperationError as error:
            raise HttpRefusal(
                error.status, error.reason, error.message, details=error.details
            ) from error
        except intervention.InterventionError as error:
            raise HttpRefusal(
                409, error.reason, error.message, details=dict(error.details)
            ) from error
        except workbench_actions.ActionRefused as error:
            raise HttpRefusal(error.status, error.reason, error.message) from error
        except (OSError, ValueError) as error:
            raise HttpRefusal(409, "question-reply-failed", str(error)) from error
        return Response.success(
            202,
            reason="accepted",
            operation=outcome.operation,
            replayed=outcome.replayed,
        )

    def _spawn_run(self, call: Call, *, reply_text: str | None) -> Response:
        """Start one run for a plan, at most once per plan and per ``launch_id``.

        The two spawning operations share this path because they share the
        failure an operator actually hits: a browser that reconnects, or an
        impatient second click, resending a launch that already succeeded. Four
        things stand between that and a second process, in this order:

        1. Durable admission. ``workbench_actions`` records the submission in
           the plan's own operation journal under an exclusive lock, so a
           resent ``launch_id`` is recognised as the same submission even by a
           server that did not answer the first one -- and a submission that
           arrives while another is genuinely in flight is refused rather than
           spawned. This is also the write that makes an interrupted launch
           accountable afterwards, because dispatch is recorded before the
           process is started rather than after.
        2. ``launch_id`` replay. A resent request carrying the id of one that
           already ran gets that run's own answer back, so a retry is the same
           request rather than a second one.
        3. The ownership pre-check below, which reports a live run as a conflict
           instead of spawning a process that dies on the plan lock. The plan
           lock remains the authority on execution; admission only keeps a
           second attempt from being started in front of it.
        4. The per-plan guard, which makes 2 and 3 mean anything on a threaded
           server by serializing check-and-spawn for one plan.

        A client that supplies no ``launch_id`` gets a minted one, so its
        submission is still accounted for even though nothing can recognize a
        retry of it as a retry.

        All four live in ``workbench_actions``, which is also what the bridge
        calls. The two surfaces used to admit, spawn, and settle by their own
        arrangements; a browser click and a supervisor's command now differ
        only in how they arrived.
        """
        plan_dir = call.require_string("plan_dir")
        launch_id = call.bounded_id("launch_id", maximum=MAX_LAUNCH_ID_CHARS)
        profile = coerce_profile_payload(call.body.get("profile"))
        action = (
            workbench_action_rules.ACTION_LAUNCH
            if reply_text is None
            else workbench_action_rules.ACTION_RESUME
        )
        try:
            canonical = plans_files.canonicalize_path(plan_dir)
        except (OSError, ValueError) as error:
            raise HttpRefusal(
                400,
                workbench_launch.LAUNCH_REASON_INVALID_PLAN,
                f"invalid plan_dir: {error}",
                details={"plan_dir": plan_dir},
            ) from error
        with self.launches.guard(canonical):
            try:
                outcome = workbench_actions.perform(
                    canonical,
                    action=action,
                    operation_id=launch_id,
                    reply_text=reply_text,
                    profile=profile,
                    state_dir=self.config.state_dir,
                    guard=self._launch_conflict,
                )
            except OperationError as error:
                raise HttpRefusal(
                    error.status, error.reason, error.message, details=error.details
                ) from error
            except intervention.InterventionError as error:
                raise HttpRefusal(
                    409, error.reason, error.message, details=dict(error.details)
                ) from error
            except workbench_actions.ActionRefused as error:
                raise HttpRefusal(
                    error.status,
                    error.reason,
                    error.message,
                    details={"plan_dir": str(canonical)},
                ) from error
            except workbench_launch.LaunchError as error:
                raise HttpRefusal(
                    400, error.reason, error.message, details={"plan_dir": plan_dir}
                ) from error
            if outcome.replayed:
                return self._answer_prior_submission(outcome, canonical, launch_id)
            assert outcome.launch is not None  # a carried-out spawn has one
            payload = outcome.launch.to_payload()
            self.launches.record_pid(canonical, outcome.launch.pid)
            if launch_id is not None:
                self.launches.record(canonical, launch_id, payload)
        return Response.success(
            reason="ok",
            launch=payload,
            launch_id=launch_id,
            replayed=False,
            operation=outcome.operation,
        )

    def _answer_prior_submission(
        self,
        outcome: workbench_actions.ActionOutcome,
        plan_dir: Path,
        launch_id: str | None,
    ) -> Response:
        """Answer a submission the journal has already seen.

        A replay gets this server's own receipt when it happens to hold one,
        and the journal row otherwise. The distinction is worth keeping rather
        than collapsing: only the process that answered the first request can
        say what pid it started, so a cross-process replay reports *that the
        submission is already recorded* instead of inventing a launch payload
        for a process it never saw.
        """
        recorded = (
            self.launches.recorded(plan_dir, launch_id)
            if launch_id is not None
            else None
        )
        admission = outcome.admission
        return Response.success(
            reason="ok",
            launch=recorded,
            launch_id=launch_id,
            replayed=True,
            operation=(
                admission.to_payload() if admission is not None else outcome.operation
            ),
        )

    def _launch_conflict(self, facts: intervention.RunFacts) -> tuple[str, str] | None:
        """Name what already owns this plan, or ``None`` when nothing does.

        Two checks, because neither sees the other's window. Once a child has
        taken the plan lock, the shared service reports a live run. Before it
        has -- the seconds a reconnecting browser is most likely to resend in --
        nothing durable says so yet, and the pid this server spawned is the only
        evidence a launch is already under way.

        The facts are the ones the shared action already read, rather than a
        second reading of its own: a guard that answers about a different
        instant than the operation it guards is a guard with a gap in it.

        A paused run is deliberately not a conflict: resuming one is exactly
        what the reply operation is for.
        """
        plan_dir = facts.plan_dir
        if facts.run_active:
            return (
                "run-already-active",
                "a run already holds this plan"
                + (f": {facts.run_id}" if facts.run_id else ""),
            )
        pid = self.launches.pid(plan_dir)
        if pid is not None:
            if state.is_pid_running(pid):
                return (
                    "launch-already-in-flight",
                    f"this server already started a run for this plan (pid {pid})",
                )
            # Forget a process that has exited. Remembering it costs nothing
            # today and everything the day the operating system reuses its
            # number: a long-dead pid that reads as alive again would refuse a
            # launch that nothing is actually running.
            self.launches.forget_pid(plan_dir)
        return None
