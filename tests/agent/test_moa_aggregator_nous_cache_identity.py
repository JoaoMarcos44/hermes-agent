from types import SimpleNamespace


def test_moa_aggregator_reuses_shared_transport_key_across_tool_growth(monkeypatch):
    from agent import moa_loop
    calls = []
    monkeypatch.setattr(moa_loop, "call_llm", lambda **kwargs: calls.append(kwargs) or SimpleNamespace(choices=[]))
    monkeypatch.setattr(moa_loop, "_slot_runtime", lambda slot: {
        "provider": "openai", "model": "gpt-oss", "base_url": "https://api.openai.com/v1",
        "api_mode": "chat_completions",
    })
    client = moa_loop.MoAChatCompletions.__new__(moa_loop.MoAChatCompletions)
    client._pending_trace = None
    tools = [{"type": "function", "function": {"name": "lookup", "parameters": {"type": "object"}}}]
    first = {"messages": [{"role": "system", "content": "stable system"}, {"role": "user", "content": "go"}],
             "guidance": "volatile advice 1", "aggregator": {"provider": "openai", "model": "gpt-oss"}, "aggregator_temperature": None}
    second = {"messages": first["messages"] + [
        {"role": "assistant", "tool_calls": [{"id": "1", "function": {"name": "lookup", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "1", "content": "result"}],
        "guidance": "volatile advice 2", "aggregator": first["aggregator"], "aggregator_temperature": None}
    client._call_prepared_aggregator(first, {"tools": tools})
    client._call_prepared_aggregator(second, {"tools": tools})
    assert calls[0]["extra_body"]["prompt_cache_key"] == calls[1]["extra_body"]["prompt_cache_key"]
    assert calls[0]["extra_body"]["prompt_cache_key"]
    assert "prompt_cache_key" not in tools
    assert "prompt_cache_key" not in first
    assert "prompt_cache_key" not in second


def test_moa_aggregator_preserves_explicit_cache_key(monkeypatch):
    from agent import moa_loop
    calls = []
    monkeypatch.setattr(moa_loop, "call_llm", lambda **kwargs: calls.append(kwargs) or SimpleNamespace(choices=[]))
    monkeypatch.setattr(moa_loop, "_slot_runtime", lambda slot: {
        "provider": "openai", "model": "gpt-oss", "base_url": "https://api.openai.com/v1", "api_mode": "chat_completions",
    })
    client = moa_loop.MoAChatCompletions.__new__(moa_loop.MoAChatCompletions); client._pending_trace = None
    prepared = {"messages": [{"role": "system", "content": "stable"}], "guidance": None,
                "aggregator": {}, "aggregator_temperature": None}
    client._call_prepared_aggregator(prepared, {"tools": [], "extra_body": {"prompt_cache_key": "caller-key"}})
    assert calls[0]["extra_body"]["prompt_cache_key"] == "caller-key"
