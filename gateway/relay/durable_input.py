"""Durable relay input snapshots and bounded recovery outside the model turn."""

from __future__ import annotations

import asyncio
import copy
import dataclasses
import hashlib
import json
import logging
import os
from pathlib import Path
import shutil
import time
import uuid
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from gateway.platforms.event import MessageEvent

logger = logging.getLogger("gateway.relay.adapter")


def gateway_input_owner(event) -> str:
    """Stable transcript ownership, including inputs without a platform message id."""
    owner = getattr(event, "_relay_input_owner", None)
    if isinstance(owner, str) and owner:
        return owner
    source = event.source
    namespace = [source.platform.value, source.profile, source.scope_id,
                 source.chat_id, source.thread_id, str(event.message_id)]
    owner = (str(uuid.uuid5(uuid.NAMESPACE_URL, json.dumps(namespace)))
             if event.message_id else str(uuid.uuid4()))
    return owner


def replay_event_payload(event: MessageEvent, *, buffer_id=None) -> dict:
    """Snapshot normalized fields, excluding raw transport frames and process owners."""
    event._relay_input_owner = gateway_input_owner(event)
    data = {}
    for field in dataclasses.fields(event):
        name = field.name
        if name.startswith("_") or name in {"raw_message", "source"}:
            continue
        value = getattr(event, name)
        if name == "message_type":
            value = value.value
        elif name == "timestamp":
            value = value.isoformat()
        data[name] = value
    source = {
        field.name: getattr(event.source, field.name)
        for field in dataclasses.fields(event.source)
        if not field.name.startswith("_") and field.name != "delivered_via_upstream_relay"
    }
    source["platform"] = event.source.platform.value
    payload = {
        "version": 1, "admitted_at": time.time_ns(), "buffer_id": buffer_id,
        "input_owner": gateway_input_owner(event), "event": data, "source": source,
        "via_relay": event.source.delivered_via_upstream_relay is True,
    }
    # Fail before ACK on unsupported metadata. Detach nested collections from later merges.
    return json.loads(json.dumps(payload, ensure_ascii=False, allow_nan=False))


def event_from_replay_payload(payload: dict) -> MessageEvent:
    """Decode private gateway-owned state; this is never an ingress wire decoder."""
    from datetime import datetime
    from gateway.config import Platform
    from gateway.platforms.event import MessageEvent, MessageType
    from gateway.session import SessionSource

    if payload.get("version") != 1:
        raise ValueError("unsupported durable relay input version")
    data = copy.deepcopy(payload["event"])
    source_data = dict(payload["source"])
    source_data["platform"] = Platform(source_data["platform"])
    source_data["delivered_via_upstream_relay"] = payload.get("via_relay") is True
    source = SessionSource(**source_data)
    data["message_type"] = MessageType(data["message_type"])
    data["timestamp"] = datetime.fromisoformat(data["timestamp"])
    event = MessageEvent(source=source, **data)
    event._relay_input_owner = payload["input_owner"]
    event._relay_durable_pending = True
    event._relay_durable_replay = True
    event._relay_buffer_id = payload.get("buffer_id")
    return event


def _inbox_directory(store, key: str) -> Path:
    digest = hashlib.sha256(key.encode()).hexdigest()
    return Path(store.sessions_dir) / "relay_inbox" / digest


def _fsync_directory(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def pin_event_payload(store, key: str, event, *, buffer_id=None) -> dict:
    """Keep immutable attachments outside cache cleanup before committing the capsule."""
    payload = replay_event_payload(event, buffer_id=buffer_id)
    urls = payload["event"]["media_urls"]
    originals = list(urls)
    directory = _inbox_directory(store, key)
    for index, raw in enumerate(urls):
        source = Path(raw)
        if "://" in raw or not source.is_file():
            if "://" not in raw:
                raise FileNotFoundError(f"durable relay attachment unavailable: {raw}")
            continue
        directory.mkdir(parents=True, exist_ok=True)
        target = directory / f"{index}{source.suffix}"
        # Admission claims ensure a single writer; preserve a pre-existing committed pin.
        if not target.exists():
            temporary = directory / f".{index}-{uuid.uuid4().hex}.tmp"
            try:
                with source.open("rb") as reader, temporary.open("xb") as writer:
                    shutil.copyfileobj(reader, writer)
                    writer.flush()
                    os.fsync(writer.fileno())
                os.replace(temporary, target)
            finally:
                temporary.unlink(missing_ok=True)
        urls[index] = str(target)
    if directory.exists():
        _fsync_directory(directory)
        _fsync_directory(directory.parent)
        _fsync_directory(directory.parent.parent)
    payload["media_original_urls"] = originals
    return payload


def materialize_replay_event(store, key: str, payload: dict) -> MessageEvent:
    """Give preprocessing disposable cache copies, keeping inbox originals intact."""
    from hermes_constants import get_hermes_home

    event = event_from_replay_payload(payload)
    pinned_directory = _inbox_directory(store, key)
    working_directory = Path(get_hermes_home()) / "cache" / "documents"
    originals = payload.get("media_original_urls", [])
    for index, raw in enumerate(event.media_urls):
        source = Path(raw)
        if source.parent != pinned_directory:
            continue
        working_directory.mkdir(parents=True, exist_ok=True)
        target = working_directory / f"relay-{uuid.uuid4().hex}{source.suffix}"
        shutil.copyfile(source, target)
        event.media_urls[index] = str(target)
        for previous in (raw, originals[index] if index < len(originals) else raw):
            if event.text and previous in event.text:
                event.text = event.text.replace(previous, str(target))
    event._relay_dedupe_key = key
    return event


class RelayDurableInputMixin:
    """One store-owned recovery worker retries unresolved capsules without awaiting models."""

    async def _stage_durable_delivery(self, event, key, buffer_id=None) -> bool:
        if getattr(event, "_relay_durable_pending", False):
            return True
        store = getattr(self, "_session_store", None)
        stage = getattr(store, "stage_relay_delivery", None)
        self._canonicalize(event.source)
        if store is None or not callable(stage) or key is None:
            return False
        pending = await self._offload_discord_context_io(store.get_relay_delivery_pending, key)
        if pending is None:
            payload = await self._offload_discord_context_io(
                lambda: pin_event_payload(store, key, event, buffer_id=buffer_id))
            if await self._offload_discord_context_io(stage, key, payload) is not True:
                return False
            pending = await self._offload_discord_context_io(store.get_relay_delivery_pending, key)
        if pending is None:
            if await self._is_delivery_seen(key):
                event._relay_delivery_already_consumed = True
                await self._cleanup_durable_delivery(key)
            else:
                # Never execute a changed retransmission when the immutable committed snapshot
                # could not be read. The connector and recovery worker can both retry later.
                raise RuntimeError("durable relay snapshot is not readable")
        else:
            restored = await self._offload_discord_context_io(
                materialize_replay_event, store, key, pending)
            for field in dataclasses.fields(event):
                if not field.name.startswith("_"):
                    setattr(event, field.name, getattr(restored, field.name))
            event._relay_input_owner = pending["input_owner"]
            event._relay_durable_replay = True
            self._durable_resume_keys()[key] = self._event_session_key(event)
        event._relay_durable_pending = True
        event._relay_dedupe_key = key
        return True

    async def _cleanup_durable_delivery(self, key) -> None:
        store = getattr(self, "_session_store", None)
        if store is None or key is None:
            return
        def cleanup():
            if store.get_relay_delivery_pending(key) is None and store.is_relay_delivery_settled(key):
                shutil.rmtree(_inbox_directory(store, key), ignore_errors=True)
                return True
            return False
        if await self._offload_discord_context_io(cleanup):
            self._durable_resume_keys().pop(key, None)

    def _durable_resume_keys(self):
        store = getattr(self, "_session_store", None)
        authority = store.__dict__ if store is not None else self.__dict__
        return authority.setdefault("_relay_durable_resume_keys", {})

    def _has_durable_resume_for(self, session_key) -> bool:
        return session_key in self._durable_resume_keys().values()

    async def _load_durable_resume_sessions(self, store) -> None:
        keys, after = {}, None
        while True:
            batch = await self._offload_discord_context_io(
                lambda: store.pending_relay_deliveries(limit=100, after=after))
            if not batch:
                break
            after = (batch[-1][1]["admitted_at"], batch[-1][0])
            for key, payload in batch:
                event = await self._offload_discord_context_io(event_from_replay_payload, payload)
                keys[key] = self._event_session_key(event)
        shared = self._durable_resume_keys()
        shared.clear()
        shared.update(keys)

    def _wake_delivery_recovery(self) -> None:
        wake = self.__dict__.get("_delivery_recovery_wake")
        if wake is not None:
            wake.set()

    async def _start_delivery_recovery(self) -> None:
        store = getattr(self, "_session_store", None)
        if store is None or not callable(getattr(store, "pending_relay_deliveries", None)):
            return
        await self._stop_delivery_recovery()
        await self._load_durable_resume_sessions(store)
        token = object()
        store.__dict__["_relay_recovery_owner"] = token
        self._delivery_recovery_owner = token
        self._delivery_recovery_wake = asyncio.Event()
        self._delivery_recovery_task = asyncio.create_task(
            self._recover_durable_deliveries(store, token), name="relay-input-recovery")

    async def _stop_delivery_recovery(self) -> None:
        task = self.__dict__.pop("_delivery_recovery_task", None)
        store = getattr(self, "_session_store", None)
        token = self.__dict__.pop("_delivery_recovery_owner", None)
        if store is not None and store.__dict__.get("_relay_recovery_owner") is token:
            store.__dict__.pop("_relay_recovery_owner", None)
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def _recover_durable_deliveries(self, store, token) -> None:
        while store.__dict__.get("_relay_recovery_owner") is token:
            try:
                after = None
                while store.__dict__.get("_relay_recovery_owner") is token:
                    batch = await self._offload_discord_context_io(
                        lambda: store.pending_relay_deliveries(limit=100, after=after))
                    if not batch:
                        break
                    after = (batch[-1][1]["admitted_at"], batch[-1][0])
                    for key, payload in batch:
                        if store.__dict__.get("_relay_recovery_owner") is not token:
                            return
                        receipt = self._retained_deliveries().get(key)
                        if receipt is not None:
                            if receipt.done() and not receipt.cancelled() and receipt.result() is True:
                                await self._ack_completed_replay(key, payload.get("buffer_id"))
                            continue
                        if await self._is_delivery_seen(key):
                            # Completed work with a temporarily failed receipt write only retries I/O.
                            await self._settle_consumed_delivery(key, payload.get("buffer_id"))
                            continue
                        event = await self._offload_discord_context_io(event_from_replay_payload, payload)
                        self._canonicalize(event.source)
                        runner = getattr(self, "gateway_runner", None)
                        if runner is None:
                            runner = getattr(getattr(self, "_message_handler", None), "__self__", None)
                        if runner is not None and callable(getattr(runner, "_is_session_running", None)):
                            session_key = runner._session_key_for_source(event.source)
                            if runner._is_session_running(session_key):
                                continue
                        event = await self._offload_discord_context_io(
                            materialize_replay_event, store, key, payload)
                        await self._on_inbound(event)
                await self._load_durable_resume_sessions(store)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.warning("durable relay recovery deferred", exc_info=True)
            if store.__dict__.get("_relay_recovery_owner") is not token:
                return
            wake = self._delivery_recovery_wake
            wake.clear()
            try:
                await asyncio.wait_for(wake.wait(), 1.0)
            except asyncio.TimeoutError:
                pass
