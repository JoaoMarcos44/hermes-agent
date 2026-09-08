from types import SimpleNamespace

import agent.background_review as background_review


def test_skill_only_review_does_not_whitelist_memory(monkeypatch):
    seen = {}

    def definitions(*, enabled_toolsets, quiet_mode):
        seen["toolsets"] = enabled_toolsets
        tools = [{"function": {"name": "skill_manage"}}]
        if "memory" in enabled_toolsets:
            tools.append({"function": {"name": "memory"}})
        return tools

    monkeypatch.setattr("model_tools.get_tool_definitions", definitions)
    fork = SimpleNamespace(_memory_enabled=True, _user_profile_enabled=True)

    allowed, _ = background_review._review_tool_whitelist(
        fork, {}, review_memory=False, review_skills=True
    )

    assert seen["toolsets"] == ["skills"]
    assert "memory" not in allowed
    assert "skill_manage" in allowed


def test_combined_review_keeps_memory_tool_available(monkeypatch):
    seen = {}

    def definitions(*, enabled_toolsets, quiet_mode):
        seen["toolsets"] = enabled_toolsets
        return [{"function": {"name": name}} for name in ("skill_manage", "memory")]

    monkeypatch.setattr("model_tools.get_tool_definitions", definitions)
    fork = SimpleNamespace(_memory_enabled=True, _user_profile_enabled=False)

    allowed, _ = background_review._review_tool_whitelist(
        fork, {}, review_memory=True, review_skills=True
    )

    assert seen["toolsets"] == ["memory", "skills"]
    assert {"memory", "skill_manage"} <= allowed
