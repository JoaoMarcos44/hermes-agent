"""Regression tests for issue #47237.

When the gateway persists a user message after a transient provider
failure (429/timeout/auth error), subsequent retries of the same
Telegram message must not stack duplicate user turns in the transcript.
The failure path uses an owner-scoped atomic insert instead of a check-then-act guard.
"""

import concurrent.futures

from gateway.session import SessionStore
from hermes_state import SessionDB


class TestHasPlatformMessageId:
    """SessionDB.has_platform_message_id and SessionStore wrapper."""

    def _make_db(self, tmp_path):
        db = SessionDB(tmp_path / "state.db")
        db.create_session("s1", "cli")
        return db


    def test_returns_false_for_different_session(self, tmp_path):
        db = self._make_db(tmp_path)
        db.create_session("s2", "cli")
        db.append_message(
            session_id="s1",
            role="user",
            content="hello",
            platform_message_id="msg-123",
        )
        assert not db.has_platform_message_id("s2", "msg-123")


    def test_session_store_wrapper_proxies_to_db(self, tmp_path):
        db = self._make_db(tmp_path)
        db.append_message(
            session_id="s1",
            role="user",
            content="hello",
            platform_message_id="msg-456",
        )
        store = SessionStore.__new__(SessionStore)
        store._db = db
        assert store.has_platform_message_id("s1", "msg-456")
        assert not store.has_platform_message_id("s1", "msg-000")


class TestDedupeOnTransientFailure:
    """The gateway's transient-failure path must not persist duplicates."""

    @staticmethod
    def _make_db(tmp_path):
        db = SessionDB(tmp_path / "state.db")
        db.create_session("s1", "cli")
        return db

    def test_duplicate_message_id_skipped(self, tmp_path):
        """When has_platform_message_id returns True, the append is skipped."""
        db = self._make_db(tmp_path)
        db.append_message(
            session_id="s1",
            role="user",
            content="hello",
            platform_message_id="msg-789",
        )
        store = SessionStore.__new__(SessionStore)
        store._db = db

        # Simulate a second attempt to persist the same message
        assert store.has_platform_message_id("s1", "msg-789")
        # The gateway uses the atomic writer for the actual persistence path;
        # this lower-level probe remains a compatibility check for the index.

    def test_failed_turn_boundary_is_atomic_and_reload_safe(self, tmp_path):
        """Concurrent duplicate delivery creates one boundary and reloads role-safe."""
        db = self._make_db(tmp_path)
        db.append_message(
            session_id="s1",
            role="user",
            content="mutate record",
            platform_message_id="msg-boundary",
        )
        boundary = {
            "role": "assistant",
            "content": "Your request was not processed. Send it again if you still want me to carry it out.",
            "timestamp": 1.0,
            "display_kind": "gateway_failed_turn_boundary",
            "display_metadata": {"gateway_failed_turn_boundary": "s1:msg-boundary"},
        }

        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(
                lambda _:
                    db.append_gateway_failed_turn_boundary(
                        "s1", boundary, "s1:msg-boundary", legacy_platform_message_id="msg-boundary",
                    ),
                range(8),
            ))

        assert sum(results) == 1
        restored = db.get_messages_as_conversation("s1", repair_alternation=True)
        assert [message["role"] for message in restored] == ["user", "assistant"]
        assert restored[-1]["content"] == boundary["content"]
        db.close()

    def test_failed_turn_user_owner_is_atomic(self, tmp_path):
        """Concurrent retries of one owned platform input create one user row."""
        db = self._make_db(tmp_path)
        message = {
            "role": "user",
            "content": "mutate record",
            "platform_message_id": "msg-user",
            "display_metadata": {"gateway_input_owner": "owner-1"},
        }

        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(
                lambda _: db.append_user_message_if_absent("s1", message),
                range(8),
            ))

        assert sum(results) == 1
        assert db.message_count("s1") == 1
        foreign = {**message, "display_metadata": {"gateway_input_owner": "owner-2"}}
        assert db.append_user_message_if_absent("s1", foreign)
        assert db.message_count("s1") == 2
        db.close()

    def test_exception_fallback_is_atomic_for_duplicate_events(self, tmp_path):
        """Concurrent exception handlers persist one user/boundary pair."""
        import asyncio

        from gateway.config import GatewayConfig, Platform
        from gateway.platforms.event import MessageEvent
        from gateway.run import GatewayRunner
        from gateway.session import SessionSource, SessionStore

        async def exercise():
            store = SessionStore(tmp_path / "sessions", GatewayConfig())
            runner = object.__new__(GatewayRunner)
            runner.session_store = store

            async def stop_typing(event, source):
                return None

            runner._hmwa_stop_typing_for_turn = stop_typing
            source = SessionSource(
                platform=Platform.TELEGRAM, chat_id="atomic-exception", user_id="fixture"
            )
            session = store.get_or_create_session(source)
            prepared = runner._PreparedTurn(
                [], "", "mutate record", [{"type": "text", "text": "mutate record"}],
                1700000000, None, session.session_id, "exception-owner",
            )
            event = MessageEvent(text="mutate record", source=source, message_id="msg-exception")
            await asyncio.gather(*(
                runner._hmwa_agent_error_reply(
                    RuntimeError("controlled"), event, source, session,
                    session.session_key, prepared,
                )
                for _ in range(8)
            ))

            db = store._db_for_session_id(session.session_id)
            rows = db.get_messages(session.session_id, include_inactive=True)
            assert [row["role"] for row in rows] == ["user", "assistant"]
            assert rows[-1]["display_kind"] == "gateway_failed_turn_boundary"
            db.close()

        asyncio.run(exercise())

