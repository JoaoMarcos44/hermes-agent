"""Relay custody: a durable pending input or a terminal receipt must precede an ACK."""

from __future__ import annotations

import asyncio
import logging
import time

from gateway.platforms.inbound_receipt import discard_inbound
from hermes_state_relay import RELAY_RECEIPT_RETENTION_SECONDS

logger = logging.getLogger("gateway.relay.adapter")


class RelayInboundDeliveryMixin:
    def _completed_deliveries(self):
        store = getattr(self, "_session_store", None)
        authority = store.__dict__ if store is not None else self.__dict__
        completed = authority.setdefault("_completed_relay_inbound", {})
        self._seen_inbound = completed
        return completed

    def _retained_deliveries(self):
        # Replacement transports share the store while an old model task is still alive. Keep
        # RAM custody visible across that replacement; a process restart starts with an empty map.
        store = getattr(self, "_session_store", None)
        authority = store.__dict__ if store is not None else self.__dict__
        retained = authority.setdefault("_retained_relay_inbound", {})
        self._retained_inbound = retained
        return retained

    def _delivery_claims(self):
        store = getattr(self, "_session_store", None)
        authority = store.__dict__ if store is not None else self.__dict__
        claims = authority.setdefault("_relay_inbound_claims", {})
        self._inflight_inbound = claims
        return claims

    async def _is_delivery_seen(self, dedupe_key: str) -> bool:
        completed = self._completed_deliveries()
        expiry = completed.get(dedupe_key)
        if expiry is not None and expiry > time.time():
            return True
        completed.pop(dedupe_key, None)
        store = getattr(self, "_session_store", None)
        checker = getattr(store, "is_relay_delivery_settled", None)
        if callable(checker) and await self._offload_discord_context_io(checker, dedupe_key):
            completed[dedupe_key] = time.time() + RELAY_RECEIPT_RETENTION_SECONDS
            self._evict_oldest(completed, self._SEEN_INBOUND_MAX)
            return True
        return False

    async def _claim_inbound_dedupe(self, dedupe_key):
        if dedupe_key is None:
            return True, None
        inflight = self._delivery_claims()
        while True:
            retained = self._retained_deliveries()
            receipt = retained.get(dedupe_key)
            if receipt is not None:
                if receipt.done() and not receipt.cancelled() and receipt.result() is False:
                    retained.pop(dedupe_key, None)
                else:
                    return False, None
            # Claim before awaiting the store: two misses must not both become owners.
            pending = inflight.get(dedupe_key)
            if pending is not None:
                if await asyncio.shield(pending):
                    return False, None
                continue
            claim = asyncio.get_running_loop().create_future()
            inflight[dedupe_key] = claim
            try:
                seen = await self._is_delivery_seen(dedupe_key)
            except BaseException:
                self._finish_inbound_dedupe(dedupe_key, claim, admitted=False)
                raise
            if seen:
                self._finish_inbound_dedupe(dedupe_key, claim, admitted=True)
                return False, None
            return True, claim

    def _finish_inbound_dedupe(self, dedupe_key, claim, *, admitted: bool) -> None:
        """Release admission followers; this says nothing about durable consumption."""
        if dedupe_key is None or claim is None:
            return
        inflight = self._delivery_claims()
        if inflight.get(dedupe_key) is claim:
            inflight.pop(dedupe_key, None)
        if not claim.done():
            claim.set_result(admitted)

    async def _settle_consumed_delivery(self, dedupe_key, buffer_id=None) -> bool:
        durable = False
        if dedupe_key is not None:
            # A storage outage must not execute completed effects again in this process. Replays
            # retry only the durable write/ACK; a crash before the write remains at-least-once.
            completed = self._completed_deliveries()
            completed[dedupe_key] = time.time() + RELAY_RECEIPT_RETENTION_SECONDS
            self._evict_oldest(completed, self._SEEN_INBOUND_MAX)
            store = getattr(self, "_session_store", None)
            marker = getattr(store, "mark_relay_delivery_settled", None)
            if callable(marker):
                durable = await self._offload_discord_context_io(marker, dedupe_key) is True
            if durable:
                cleanup = getattr(self, "_cleanup_durable_delivery", None)
                if callable(cleanup):
                    await cleanup(dedupe_key)
        if buffer_id and durable:
            await self._ack_passthrough_buffer(buffer_id)
        return durable

    async def _ack_completed_replay(self, dedupe_key, buffer_id) -> None:
        retained = self._retained_deliveries()
        receipt = retained.get(dedupe_key)
        if receipt is not None:
            if receipt.done() and not receipt.cancelled() and receipt.result() is True:
                if await self._settle_consumed_delivery(dedupe_key, buffer_id):
                    if retained.get(dedupe_key) is receipt:
                        retained.pop(dedupe_key, None)
                    return
            # An in-process replay can ACK only if the original has durable inbox custody.
            store = getattr(self, "_session_store", None)
            getter = getattr(store, "get_relay_delivery_pending", None)
            if buffer_id and callable(getter):
                pending = await self._offload_discord_context_io(getter, dedupe_key)
                if pending is not None:
                    await self._ack_passthrough_buffer(buffer_id)
            return
        if await self._is_delivery_seen(dedupe_key):
            await self._settle_consumed_delivery(dedupe_key, buffer_id)

    def _attach_delivery_receipt(self, event, dedupe_key, claim, buffer_id=None):
        receipt = asyncio.get_running_loop().create_future()
        event._inbound_receipts.append(receipt)
        retained = self._retained_deliveries()
        if dedupe_key is not None:
            retained[dedupe_key] = receipt

        async def settle():
            durable = False
            try:
                consumed = await receipt
                if consumed:
                    durable = await self._settle_consumed_delivery(dedupe_key, buffer_id)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.warning("relay delivery settlement failed; buffer remains unacked", exc_info=True)
            finally:
                completed = receipt.done() and not receipt.cancelled() and receipt.result() is True
                marker = getattr(getattr(self, "_session_store", None), "mark_relay_delivery_settled", None)
                # A bounded completed cache cannot evict proof of effects whose receipt write
                # failed. Keep that terminal Future until a replay/worker commits the receipt.
                if (not completed or durable or not callable(marker)) and retained.get(dedupe_key) is receipt:
                    retained.pop(dedupe_key, None)
                wake = getattr(self, "_wake_delivery_recovery", None)
                if callable(wake):
                    wake()

        task = asyncio.create_task(settle(), name="relay-inbound-settlement")
        tasks = self.__dict__.setdefault("_settlement_tasks", set())
        tasks.add(task)
        task.add_done_callback(tasks.discard)
        return receipt

    async def _cancel_delivery_settlements(self):
        tasks = tuple(self.__dict__.setdefault("_settlement_tasks", set()))
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        # A replacement adapter may have added its own work to the shared map. Each watcher's
        # finally releases only its receipt; never clear another live transport's custody.

    async def _on_inbound(self, event) -> bool:
        # Returning False gives the adapter ACK ownership; the receiver stays free for interrupts,
        # approval replies, and independent messages while a model turn is still running.
        buffer_id = getattr(event, "_relay_buffer_id", None)
        dedupe_key = getattr(event, "_relay_dedupe_key", None) or self._inbound_dedupe_key(event)
        if buffer_id and dedupe_key is None:
            dedupe_key = f"inbound_buffer:{buffer_id}"
        should_process, claim = await self._claim_inbound_dedupe(dedupe_key)
        if not should_process:
            await self._ack_completed_replay(dedupe_key, buffer_id)
            return False
        receipt = None
        try:
            self._capture_scope(event)
            await self._remember_discord_context(event.source)
            self._stamp_slack_session_thread(event)
            if await self._consume_prompt_response(event):
                await self._settle_consumed_delivery(dedupe_key, buffer_id)
                self._finish_inbound_dedupe(dedupe_key, claim, admitted=True)
                claim = None
                return False
            await self._localize_inbound_media(event)
            durable = bool(getattr(event, "_relay_durable_pending", False))
            if buffer_id and not durable:
                durable = await self._stage_durable_delivery(event, dedupe_key, buffer_id)
            if getattr(event, "_relay_delivery_already_consumed", False):
                if buffer_id:
                    await self._ack_passthrough_buffer(buffer_id)
                self._finish_inbound_dedupe(dedupe_key, claim, admitted=True)
                claim = None
                return False
            receipt = self._attach_delivery_receipt(
                event, dedupe_key, claim, None if durable else buffer_id,
            )
            if buffer_id and durable:
                await self._ack_passthrough_buffer(buffer_id)
            await self.handle_message(event)
            if event._gateway_accepted is not True and not receipt.done():
                discard_inbound(event)
            self._finish_inbound_dedupe(dedupe_key, claim, admitted=event._gateway_accepted is True)
            claim = None
        finally:
            if claim is not None:
                admitted = event._gateway_accepted is True
                if receipt is not None and not admitted:
                    discard_inbound(event)
                self._finish_inbound_dedupe(dedupe_key, claim, admitted=admitted)
        return False
