# mypy: disable-error-code="attr-defined,call-arg,has-type,name-defined"
# ruff: noqa: F821
"""Guidance delivery methods for a Claude stream session."""

from __future__ import annotations

from . import claude_stream_support as _support

globals().update(
    {name: value for name, value in vars(_support).items() if not name.startswith("__")}
)
del _support


class _ClaudeStreamGuidance:
    def steer(
        self, request: guidance_delivery.GuidanceRequest
    ) -> guidance_delivery.GuidanceOutcome:
        """Offer one guidance record to this turn and report what is known.

        Called repeatedly for the same record while it stays unconfirmed: the
        first call arms both routes, and every later call reports whichever
        evidence has since arrived. It never blocks waiting for that evidence,
        because the caller is a poll tick holding the live-delivery endpoint
        lock that end-of-turn has to acquire.
        """
        with self._state_lock:
            offer = self._offers.get(request.guidance_id)
            first_attempt = offer is None
            if offer is None:
                offer = _Offer(request.guidance_id)
                self._offers[request.guidance_id] = offer
            confirmed_by = offer.confirmed_by
            stdin_closed = self._stdin_closed
            write_failed = offer.write_failed
            interrupted = self._interrupt_requested

        if confirmed_by is not None:
            return guidance_delivery.DELIVERED

        if interrupted:
            # The turn is being stopped, so neither route can still deliver:
            # a message written now would be echoed by a CLI draining its queue
            # on the way out, and a hook armed now has no further tool call to
            # inject into. A receipt already on disk is different -- the hook
            # writes one only after handing the text to a call that then ran --
            # so it still counts, and everything else waits for the next turn.
            if self._receipt_confirms(offer):
                return guidance_delivery.DELIVERED
            return guidance_delivery.deferred(guidance_delivery.REASON_STALE_TURN)

        if first_attempt:
            self._arm_hook(request)
            if stdin_closed:
                # The turn's own result already closed the conversation; the
                # hook is the only route left, and it can still fire while the
                # process winds down.
                return self._confirmation_outcome(offer, detail="stdin-closed")
            try:
                self._write(
                    user_message(request.text),
                    echo_body=request.text,
                    guidance_id=request.guidance_id,
                )
            except OSError as error:
                with self._state_lock:
                    offer.write_failed = True
                return self._confirmation_outcome(
                    offer,
                    failed=True,
                    detail=f"{type(error).__name__}: {error}",
                )
            return self._confirmation_outcome(offer)

        return self._confirmation_outcome(offer, failed=write_failed)

    def _arm_hook(self, request: guidance_delivery.GuidanceRequest) -> None:
        spool = self._spool
        if spool is None:
            return
        with contextlib.suppress(OSError):
            spool.offer(
                guidance_id=request.guidance_id,
                seq=request.seq,
                text=request.text,
            )

    def _confirmation_outcome(
        self,
        offer: _Offer,
        *,
        failed: bool = False,
        detail: str | None = None,
    ) -> guidance_delivery.GuidanceOutcome:
        """Report the evidence that exists right now, and nothing more."""
        if self._receipt_confirms(offer):
            return guidance_delivery.DELIVERED
        with self._state_lock:
            if offer.confirmed_by is not None:
                return guidance_delivery.DELIVERED
        if failed and not self._hook_still_armed(offer):
            # Neither route survives: the pipe is gone and the hook has nothing
            # left to claim. This attempt is over.
            return guidance_delivery.deferred(
                guidance_delivery.REASON_TRANSPORT_UNAVAILABLE,
                detail=detail,
            )
        return guidance_delivery.awaiting_confirmation(detail=detail)

    def _receipt_confirms(self, offer: _Offer) -> bool:
        """Whether the hook has reported injecting this offer, recording it once."""
        spool = self._spool
        if spool is None or not spool.receipt(offer.guidance_id):
            return False
        with self._state_lock:
            if offer.confirmed_by is None:
                offer.confirmed_by = "pretooluse-hook"
        return True

    def _hook_still_armed(self, offer: _Offer) -> bool:
        spool = self._spool
        if spool is None:
            return False
        pending = spool.root / _PENDING / f"{offer.guidance_id}.json"
        claimed = spool.root / _CLAIMED / f"{offer.guidance_id}.json"
        return pending.exists() or claimed.exists()
