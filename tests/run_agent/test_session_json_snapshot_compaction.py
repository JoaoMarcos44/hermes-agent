"""Behavior contracts for opt-in per-session JSON snapshot persistence."""

import json
from datetime import datetime
from pathlib import Path

from agent.session_persistence import SessionPersistenceMixin


class _SnapshotAgent(SessionPersistenceMixin):
    def __init__(self, logs_dir: Path) -> None:
        self._session_json_enabled = True
        self._session_messages = []
        self._last_compaction_in_place = False
        self._last_compression_attempt_in_place = None
        self.logs_dir = logs_dir
        self.session_id = "snapshot-contract"
        self.model = "test/model"
        self.base_url = "https://example.invalid/v1"
        self.platform = "cli"
        self.session_start = datetime.now()
        self._cached_system_prompt = "test system prompt"
        self.tools = []
        self.verbose_logging = False


def _snapshot(agent: _SnapshotAgent, messages: list[dict]) -> dict:
    agent._session_messages = messages
    agent._save_session_log()
    path = agent.logs_dir / "session_snapshot-contract.json"
    return json.loads(path.read_text(encoding="utf-8"))


def _messages(count: int) -> list[dict]:
    return [{"role": "assistant", "content": f"message-{index}"} for index in range(count)]


def test_in_place_compaction_rewrites_smaller_json_snapshot(tmp_path):
    agent = _SnapshotAgent(tmp_path)
    _snapshot(agent, _messages(60))

    agent._last_compaction_in_place = True
    compacted = [{"role": "assistant", "content": "compacted summary"}] + _messages(4)
    current = _snapshot(agent, compacted)

    assert current["message_count"] == len(compacted)
    assert current["messages"] == compacted


def test_partial_history_without_compaction_keeps_larger_snapshot(tmp_path):
    agent = _SnapshotAgent(tmp_path)
    _snapshot(agent, _messages(60))

    current = _snapshot(agent, _messages(5))

    assert current["message_count"] == 60
    assert current["messages"] == _messages(60)
