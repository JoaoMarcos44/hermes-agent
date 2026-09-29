"""The persisted Responses snapshot follows wire trimming unless explicitly overridden."""

import json
import sqlite3
from pathlib import Path


LARGE_TOOL_TEXT = "x" * 300_000
USER_MESSAGE = "inspect the large result"


def _result(large_text):
    return {
        "final_response": "done",
        "messages": [
            {"role": "user", "content": USER_MESSAGE},
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": "audit-call",
                        "type": "function",
                        "function": {
                            "name": "read_file",
                            "arguments": json.dumps({
                                "path": "large.txt",
                                "content": large_text,
                            }),
                        },
                    }
                ],
            },
            {
                "role": "tool",
                "tool_call_id": "audit-call",
                "name": "read_file",
                "content": large_text,
            },
            {"role": "assistant", "content": "done"},
        ],
    }


def _persist_snapshot(home: Path, result, response_id: str):
    from gateway.platforms.api_server import APIServerAdapter
    from gateway.platforms.api_server_openai_routes import _ResponsesStream
    from gateway.platforms.base import PlatformConfig

    adapter = APIServerAdapter(PlatformConfig(enabled=True, extra={"port": 0}))
    try:
        writer = _ResponsesStream(
            adapter,
            None,
            response_id=response_id,
            model="test-model",
            created_at=1,
            conversation_history=[],
            user_message=USER_MESSAGE,
            instructions=None,
            conversation=None,
            store=True,
            session_id="test-session",
        )
        history = adapter._build_response_conversation_history(
            [],
            USER_MESSAGE,
            result,
            result["final_response"],
            tool_output_max_chars=adapter._history_tool_output_max_chars,
        )
        call = result["messages"][1]["tool_calls"][0]
        tool = result["messages"][2]
        writer.emitted_items = [
            {
                "type": "function_call",
                "name": call["function"]["name"],
                "call_id": call["id"],
                "arguments": call["function"]["arguments"],
            },
            {
                "type": "function_call_output",
                "call_id": tool["tool_call_id"],
                "output": [{"type": "input_text", "text": tool["content"]}],
            },
        ]
        writer.final_response_text = result["final_response"]
        wire_items = writer._final_items()
        writer.persist_snapshot(
            {"id": response_id, "output": wire_items}, history=history
        )

        db_path = Path(writer.response_store._db_path)
        assert db_path == home / "response_store.db"
        assert db_path.is_file()
        with sqlite3.connect(db_path) as connection:
            row = connection.execute(
                "SELECT data FROM responses WHERE response_id = ?", (response_id,)
            ).fetchone()
        assert row is not None
        return json.loads(row[0]), wire_items, adapter._history_tool_output_max_chars
    finally:
        adapter._response_store.close()


def test_default_persisted_snapshot_matches_wire_tool_trimming(tmp_path, monkeypatch):
    home = tmp_path / "default-home"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))

    snapshot, wire_items, history_tool_output_max_chars = _persist_snapshot(
        home, _result(LARGE_TOOL_TEXT), "default-trim"
    )
    history = snapshot["conversation_history"]
    stored_tool = next(message for message in history if message.get("role") == "tool")
    wire_output = next(
        item for item in wire_items if item.get("type") == "function_call_output"
    )
    wire_text = wire_output["output"][0]["text"]

    assert len(stored_tool["content"]) <= 1_000, (
        f"response_store.db persisted {len(stored_tool['content'])} tool-output characters verbatim "
        f"(history_tool_output_max_chars={history_tool_output_max_chars})"
    )
    assert stored_tool["content"] == wire_text

    stored_call = next(message for message in history if message.get("tool_calls"))[
        "tool_calls"
    ][0]
    wire_call = next(item for item in wire_items if item.get("type") == "function_call")
    assert json.loads(stored_call["function"]["arguments"]) == json.loads(
        wire_call["arguments"]
    )


def test_explicit_history_tool_output_limit_overrides_wire_trimming(
    tmp_path, monkeypatch
):
    for limit in (0, 1_000):
        home = tmp_path / f"explicit-{limit}"
        home.mkdir()
        (home / "config.yaml").write_text(
            "gateway:\n  api_server:\n    history_tool_output_max_chars: "
            + str(limit)
            + "\n",
            encoding="utf-8",
        )
        monkeypatch.setenv("HERMES_HOME", str(home))

        snapshot, _, configured_limit = _persist_snapshot(
            home, _result(LARGE_TOOL_TEXT), f"explicit-{limit}"
        )
        history = snapshot["conversation_history"]
        stored_tool = next(
            message for message in history if message.get("role") == "tool"
        )
        stored_call = next(message for message in history if message.get("tool_calls"))[
            "tool_calls"
        ][0]
        stored_arguments = json.loads(stored_call["function"]["arguments"])

        assert configured_limit == limit
        if limit == 0:
            assert stored_tool["content"] == LARGE_TOOL_TEXT
            assert stored_arguments["content"] == LARGE_TOOL_TEXT
        else:
            assert stored_tool["content"].startswith("x" * limit)
            assert stored_tool["content"].endswith("...[299000 more chars]")
            assert stored_arguments["content"].endswith("...[299000 more chars]")
