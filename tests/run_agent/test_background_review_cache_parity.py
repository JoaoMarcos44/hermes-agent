"""Tests that the background review fork inherits the parent's cached system prompt.

Regression coverage for issue #25322 (and PR #17276's first root cause): the
background review's outbound HTTP request must carry the same system bytes as
the parent's so Anthropic/OpenRouter's exact-prefix cache key matches.

Without this, every review rebuilds the system prompt from scratch — fresh
``_hermes_now()`` timestamp, fresh ``session_id``, and a different skills
prompt under the (former) narrow toolset — and the prefix-cache miss costs
roughly the full uncached system-prompt cost per nudge (~26% end-to-end on
Sonnet 4.5 per the contributor's measurement).
"""

from unittest.mock import patch
import run_agent

_REAL_CONVERSATION_ROOT_ID = run_agent.AIAgent._conversation_root_id


def _make_agent_stub(agent_cls):
    """Create a minimal AIAgent-like object with just enough state for _spawn_background_review."""
    agent = object.__new__(agent_cls)
    agent.model = "test-model"
    agent.platform = "test"
    agent.provider = "openai"
    agent.session_id = "sess-123"
    agent.quiet_mode = True
    agent._memory_store = None
    agent._memory_enabled = True
    agent._user_profile_enabled = False
    agent._memory_nudge_interval = 5
    agent._skill_nudge_interval = 5
    agent.background_review_callback = None
    agent.status_callback = None
    agent._cached_system_prompt = (
        "PARENT-SYSTEM-PROMPT-BYTES — must be inherited verbatim "
        "for prefix-cache parity"
    )
    agent.ephemeral_system_prompt = (
        "WebUI session context:\n- Pinned per-request gateway context"
    )
    import datetime as _dt
    agent.session_start = _dt.datetime(2026, 1, 1, 12, 0, 0)
    agent._MEMORY_REVIEW_PROMPT = "review memory"
    agent._SKILL_REVIEW_PROMPT = "review skills"
    agent._COMBINED_REVIEW_PROMPT = "review both"
    # Non-None so the test catches a missing-kwarg regression.
    agent.enabled_toolsets = ["memory", "skills", "terminal"]
    agent.disabled_toolsets = ["spotify", "feishu_doc"]
    # Non-None so the test catches reasoning_config NOT being inherited —
    # which would put the fork into a different Anthropic cache namespace.
    agent.reasoning_config = {"enabled": True, "effort": "medium"}
    # Non-empty so tests catch prefill/provider-routing NOT being inherited —
    # prefills sit right after the system message in the request body, and
    # OpenRouter provider pins decide WHICH upstream's cache gets hit.
    agent.prefill_messages = [{"role": "user", "content": "prefill turn"}]
    agent.providers_allowed = ["anthropic"]
    agent.providers_ignored = None
    agent.providers_order = None
    agent.provider_sort = "throughput"
    agent.provider_require_parameters = False
    agent.provider_data_collection = None
    return agent


class _SyncThread:
    """Drop-in replacement for threading.Thread that runs the target inline."""

    def __init__(self, *, target=None, daemon=None, name=None):
        self._target = target

    def start(self):
        if self._target:
            self._target()


def _make_recorder_class(captured=None, record_on_run=()):
    """Build a Recorder class standing in for the review-fork AIAgent.

    Keeps the stub attribute list in ONE place: when
    ``_spawn_background_review`` starts touching a new fork attribute, only
    this factory needs the extra stub — not one copy per test.

    ``captured`` (dict): if given, ``__init__`` stores the full constructor
    kwargs under ``captured["init_kwargs"]`` so tests can assert on both
    kwarg values and kwarg *presence*.
    ``record_on_run``: instance attribute names copied into ``captured`` when
    ``run_conversation`` fires — for values the production code assigns
    after construction.
    """

    class _Recorder:
        def __init__(self, *args, **kwargs):
            if captured is not None:
                captured["init_kwargs"] = dict(kwargs)
            self._cached_system_prompt = None
            self._memory_write_origin = None
            self._memory_write_context = None
            self._memory_store = None
            self._memory_enabled = None
            self._user_profile_enabled = None
            self._memory_nudge_interval = None
            self._skill_nudge_interval = None
            self.suppress_status_output = None
            self.session_start = None
            self.session_id = None
            self.tools = None
            self.valid_tool_names = set()
            self._tool_snapshot_generation = 0
            self.ephemeral_system_prompt = kwargs.get("ephemeral_system_prompt")
            self._inherited_cache_scope = None
            self._cached_conversation_root = None
            self._gateway_session_key = None

        def _conversation_root_id(self):
            return _REAL_CONVERSATION_ROOT_ID(self)

        def run_conversation(self, *args, **kwargs):
            if captured is not None:
                for _name in record_on_run:
                    captured[_name] = getattr(self, _name)
            raise RuntimeError(
                "stop after recording — don't actually call the API"
            )

        def shutdown_memory_provider(self):
            pass

        def close(self):
            pass

    return _Recorder


def test_review_fork_inherits_parent_cached_system_prompt():
    """The review fork's _cached_system_prompt must equal the parent's byte-for-byte.

    Anthropic's prefix cache keys on exact bytes; any divergence (timestamp
    minute tick, fresh session_id, narrower skills_prompt) shifts the key
    and forces a full re-cache. Inheriting the parent's cached prompt is
    the cheap, mechanical fix.
    """
    import run_agent

    agent = _make_agent_stub(run_agent.AIAgent)

    captured = {}
    parent_prompt = agent._cached_system_prompt

    _Recorder = _make_recorder_class()

    with patch.object(run_agent, "AIAgent", _Recorder), \
         patch("threading.Thread", _SyncThread):
        # The production code assigns _cached_system_prompt AFTER __init__,
        # so wrap the recorder's __setattr__ to see that post-construction
        # write from _spawn_background_review.
        orig_setattr = _Recorder.__setattr__

        def _spy_setattr(self, name, value):
            if name == "_cached_system_prompt":
                captured["written_prompt"] = value
            orig_setattr(self, name, value)

        with patch.object(_Recorder, "__setattr__", _spy_setattr):
            agent._spawn_background_review(
                messages_snapshot=[],
                review_memory=True,
                review_skills=False,
            )

    assert "written_prompt" in captured, (
        "_spawn_background_review never assigned _cached_system_prompt on the review agent"
    )
    assert captured["written_prompt"] == parent_prompt, (
        f"Review fork's _cached_system_prompt diverged from parent's. "
        f"Got {captured['written_prompt']!r}, expected {parent_prompt!r}. "
        "This breaks Anthropic/OpenRouter prefix-cache parity (#25322)."
    )


def test_review_fork_inherits_parent_ephemeral_system_prompt():
    """The fork must send the parent's complete effective system prompt.

    Gateway session context is appended through ``ephemeral_system_prompt`` at
    API-call time, outside ``_cached_system_prompt``.  Copying only the cached
    base therefore makes every background review diverge at the gateway block
    and miss the parent's warm prefix cache.
    """
    import run_agent

    agent = _make_agent_stub(run_agent.AIAgent)
    captured = {}
    _Recorder = _make_recorder_class(
        captured,
        record_on_run=("_cached_system_prompt", "ephemeral_system_prompt"),
    )

    with patch.object(run_agent, "AIAgent", _Recorder), \
         patch("threading.Thread", _SyncThread):
        agent._spawn_background_review(
            messages_snapshot=[],
            review_memory=True,
            review_skills=False,
        )

    # Pairwise asserts: stronger than comparing a locally re-joined
    # "effective" prompt (which would re-implement the production join and
    # silently keep passing if the separator ever changed — and would compare
    # equal for cached="A\n\nB"/ephemeral="" vs cached="A"/ephemeral="B").
    assert captured["_cached_system_prompt"] == agent._cached_system_prompt
    assert captured["ephemeral_system_prompt"] == agent.ephemeral_system_prompt


def test_review_fork_inherits_prefill_and_provider_routing():
    """Non-routed fork must inherit prefill messages and OpenRouter pins.

    Prefill messages are inserted right after the system message at
    API-call time, so omitting them diverges the fork's request body from
    the parent's warm prefix at message index 1. OpenRouter provider pins
    (providers_allowed/order/sort/...) decide which UPSTREAM provider serves
    the request — prompt caches live per upstream, so an unpinned fork can
    be routed to a different upstream and miss even a byte-identical prefix.
    """
    import run_agent

    agent = _make_agent_stub(run_agent.AIAgent)
    captured = {}
    _Recorder = _make_recorder_class(captured)

    with patch.object(run_agent, "AIAgent", _Recorder), \
         patch("threading.Thread", _SyncThread):
        agent._spawn_background_review(
            messages_snapshot=[],
            review_memory=True,
            review_skills=False,
        )

    init_kwargs = captured.get("init_kwargs", {})
    assert init_kwargs.get("prefill_messages") == agent.prefill_messages
    # Must be a DEEP copy: the fork's unicode-error recovery
    # (_sanitize_messages_surrogates) mutates prefill dicts in place, so
    # aliased dicts would let the fork rewrite the parent's prefill bytes
    # — silently breaking the parent's own warm prefix.
    assert (
        init_kwargs["prefill_messages"][0] is not agent.prefill_messages[0]
    ), "fork prefill aliases the parent's dicts (needs deepcopy)"
    assert init_kwargs.get("providers_allowed") == agent.providers_allowed
    assert init_kwargs.get("provider_sort") == agent.provider_sort


def test_review_fork_pins_session_start_and_session_id():
    """Defensive complement to cached-system-prompt inheritance.

    Even though ``_cached_system_prompt`` inheritance short-circuits the
    normal rebuild path, pinning ``session_start`` and ``session_id`` to
    the parent's guarantees byte-identical output from any code path that
    re-renders parts of the system prompt (compression, plugin hooks).
    """
    import run_agent

    agent = _make_agent_stub(run_agent.AIAgent)

    captured = {}
    _Recorder = _make_recorder_class(
        captured, record_on_run=("session_start", "session_id")
    )

    with patch.object(run_agent, "AIAgent", _Recorder), \
         patch("threading.Thread", _SyncThread):
        agent._spawn_background_review(
            messages_snapshot=[],
            review_memory=True,
            review_skills=False,
        )

    assert captured.get("session_start") == agent.session_start, (
        "Review fork did not inherit parent's session_start — "
        "system-prompt rebuild paths would diverge."
    )
    assert captured.get("session_id") == agent.session_id, (
        "Review fork did not inherit parent's session_id — "
        "system-prompt rebuild paths would diverge."
    )






def test_routed_review_fork_does_not_inherit_reasoning_config():
    """Routed aux path: the fork must NOT inherit the parent's reasoning_config.

    When ``auxiliary.background_review.{provider,model}`` routes the review
    to a different model, cache parity is moot (the cache is cold on that
    model regardless) and the parent's effort vocabulary may be invalid for
    the routed model/provider (OpenRouter ``extra_body.reasoning.effort`` is
    forwarded unclamped; codex_responses passes ``max``/``ultra`` through
    unmapped except on gpt-5.6/xAI). The routed fork must fall back to
    provider defaults, mirroring the ``not _routed`` gate on
    ``_cached_system_prompt`` inheritance.
    """
    import run_agent
    import agent.background_review as bg_review

    agent_stub = _make_agent_stub(run_agent.AIAgent)

    captured = {}
    _Recorder = _make_recorder_class(captured)

    routed_runtime = {
        "provider": "openrouter",
        "model": "aux-cheap-model",
        "api_key": "test-key",
        "base_url": None,
        "api_mode": None,
        "credential_pool": None,
        "request_overrides": {},
        "max_tokens": None,
        "command": None,
        "args": [],
        "routed": True,
    }

    with patch.object(run_agent, "AIAgent", _Recorder), \
         patch.object(bg_review, "_resolve_review_runtime",
                      return_value=routed_runtime), \
         patch("threading.Thread", _SyncThread):
        agent_stub._spawn_background_review(
            messages_snapshot=[],
            review_memory=True,
            review_skills=False,
        )

    init_kwargs = captured.get("init_kwargs", {})
    assert "reasoning_config" not in init_kwargs, (
        f"Routed review fork was passed the parent's reasoning_config "
        f"({init_kwargs.get('reasoning_config')!r}). On the routed path the "
        "cache is cold (no parity benefit) and the parent's effort value may "
        "be invalid for the routed model/provider — it must be omitted so "
        "the fork uses provider defaults."
    )
    # The whole cache-parity kwarg family shares the same ``not _routed``
    # gate — a future refactor hoisting any of them out of the gate must
    # fail here, not silently ship parent-only context to a foreign model.
    for _gated in (
        "ephemeral_system_prompt",
        "prefill_messages",
        "providers_allowed",
        "provider_sort",
    ):
        assert _gated not in init_kwargs, (
            f"Routed review fork was passed parent-only kwarg {_gated!r}; "
            "cache-parity inheritance must stay behind the not-routed gate."
        )


def test_review_fork_inherited_tools_survive_compaction_refresh():
    """Inherited tools survive mid-review compaction refresh (#103579).

    Acceptance criterion 1 requires the fork to advertise the same tools[] as
    the parent when targeting the same cache scope. Mid-review compaction
    boundaries invoke refresh_agent_mcp_tools(content_aware=True), which re-reads
    the live registry and drops memory provider tools unless the snapshot
    generation staleness check refuses the rebuild.
    """
    import run_agent
    from agent.background_review import build_cache_parity_fork
    from tools.mcp_tool_agent import refresh_agent_mcp_tools

    agent = _make_agent_stub(run_agent.AIAgent)
    parent_tools = [
        {"type": "function", "function": {"name": "terminal_command"}},
        {"type": "function", "function": {"name": "read_file"}},
        {"type": "function", "function": {"name": "memory"}},
        {"type": "function", "function": {"name": "fact_store"}},
        {"type": "function", "function": {"name": "fact_feedback"}},
    ]
    agent.tools = parent_tools

    _Recorder = _make_recorder_class()

    with patch.object(run_agent, "AIAgent", _Recorder):
        fork, _rt, routed = build_cache_parity_fork(agent, max_iterations=5)
        assert not routed
        assert fork.tools == parent_tools
        # Deep copy: the fork's later in-place tool edits must not leak into the parent's array.
        assert fork.tools is not parent_tools and fork.tools[0] is not parent_tools[0]

        # Simulate mid-review compaction boundary tool refresh
        added = refresh_agent_mcp_tools(fork, content_aware=True)
        assert added == set()
        assert [t["function"]["name"] for t in fork.tools] == [
            "terminal_command", "read_file", "memory", "fact_store", "fact_feedback"
        ]
        assert fork.valid_tool_names == {
            "terminal_command", "read_file", "memory", "fact_store", "fact_feedback"
        }


def test_unrouted_review_fork_inherits_empty_tool_surface():
    """Empty parent tools[] is a valid snapshot and must be copied and frozen (#103579).

    If no tools pass availability when the parent is constructed (parent.tools = []),
    the unrouted fork must inherit an empty list and freeze _tool_snapshot_generation.
    This guarantees late MCP/plugin tools discovered during fork construction or
    mid-review compaction do not break cache parity against the parent's empty surface.
    """
    import run_agent
    from agent.background_review import build_cache_parity_fork
    from tools.mcp_tool_agent import refresh_agent_mcp_tools

    agent = _make_agent_stub(run_agent.AIAgent)
    agent.tools = []

    _BaseRecorder = _make_recorder_class()

    class _RecorderWithLateTool(_BaseRecorder):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            # Simulate a late tool appearing in the constructor result before inheritance
            self.tools = [{"type": "function", "function": {"name": "newly_available"}}]
            self.valid_tool_names = {"newly_available"}

    with patch.object(run_agent, "AIAgent", _RecorderWithLateTool):
        fork, _rt, routed = build_cache_parity_fork(agent, max_iterations=5)
        assert not routed
        assert fork.tools == []
        assert fork.tools is not agent.tools
        assert fork.valid_tool_names == set()

        # Compaction refresh must refuse rebuild on frozen snapshot
        added = refresh_agent_mcp_tools(fork, content_aware=True)
        assert added == set()
        assert fork.tools == []
        assert fork.valid_tool_names == set()


def test_same_model_review_fork_inherits_parent_cache_scope():
    """Same-model review fork inherits the parent's resolved cache scope (#109964).

    When _persist_disabled=True and _session_db=None are set for persistence
    detachment, declared_conversation_scope would return None and
    resolve_prompt_cache_scope would skip the lineage walk, falling back to the
    physical session_id. For same-model review, the fork explicitly inherits the
    parent's already-resolved scope without touching the database, preserves
    _gateway_session_key, and caches the conversation root so Nous Portal tags
    and ambient affinity remain in complete parity.
    """
    import run_agent
    import agent.background_review as bg_review
    from agent.background_review import build_cache_parity_fork
    from agent.prompt_cache_scope import declared_conversation_scope, resolve_prompt_cache_scope
    from agent.portal_tags import (
        get_affinity_scope,
        get_conversation_context,
        nous_portal_tags,
        reset_affinity_scope,
        reset_conversation_context,
        set_affinity_scope,
        set_conversation_context,
    )

    class DummySessionDB:
        def __init__(self, lineage=None):
            self._lineage = lineage or []

        def is_explicit_fork_child(self, sid):
            return False

        def get_compression_lineage(self, sid):
            return self._lineage

        def get_session(self, sid):
            return {"source": "telegram"}

        def latest_conversation_boundary(self, key, source):
            return 1

        def get_conversation_root(self, sid):
            return self._lineage[0] if self._lineage else sid

    _Recorder = _make_recorder_class()

    # 1. Gateway parent with declared key
    db_gw = DummySessionDB(lineage=["live-sess-1"])
    agent_gw = _make_agent_stub(run_agent.AIAgent)
    agent_gw.session_id = "live-sess-1"
    agent_gw.platform = "telegram"
    agent_gw._gateway_session_key = "telegram:chat-42"
    agent_gw._session_db = db_gw

    parent_scope_gw = resolve_prompt_cache_scope(agent_gw)
    parent_decl_gw = declared_conversation_scope(agent_gw)
    assert parent_scope_gw.startswith("gwk_")
    assert parent_decl_gw == parent_scope_gw
    assert agent_gw._conversation_root_id() == "live-sess-1"

    with patch.object(run_agent, "AIAgent", _Recorder):
        fork_gw, _rt, routed = build_cache_parity_fork(agent_gw, max_iterations=5)
        assert not routed
        assert fork_gw._persist_disabled is True
        assert fork_gw._session_db is None
        assert fork_gw._gateway_session_key == "telegram:chat-42"
        assert fork_gw._cached_conversation_root == "live-sess-1"
        assert fork_gw._inherited_cache_scope == parent_scope_gw
        assert declared_conversation_scope(fork_gw) == parent_scope_gw
        assert resolve_prompt_cache_scope(fork_gw) == parent_scope_gw
        assert fork_gw._conversation_root_id() == "live-sess-1"

    # Ambient turn parity for gateway parent vs fork
    t1 = set_conversation_context(agent_gw._conversation_root_id())
    a1 = set_affinity_scope(declared_conversation_scope(agent_gw))
    try:
        parent_sticky = get_affinity_scope() or get_conversation_context() or agent_gw.session_id
        parent_tags = [t for t in nous_portal_tags() if t.startswith("conversation=")]
    finally:
        reset_affinity_scope(a1)
        reset_conversation_context(t1)

    t2 = set_conversation_context(fork_gw._conversation_root_id())
    a2 = set_affinity_scope(declared_conversation_scope(fork_gw))
    try:
        fork_sticky = get_affinity_scope() or get_conversation_context() or fork_gw.session_id
        fork_tags = [t for t in nous_portal_tags() if t.startswith("conversation=")]
    finally:
        reset_affinity_scope(a2)
        reset_conversation_context(t2)

    assert fork_sticky == parent_sticky == parent_scope_gw
    assert fork_tags == parent_tags == ["conversation=live-sess-1"]

    # 2. Rotated parent with compression lineage (lineage root != physical session_id)
    db_rot = DummySessionDB(lineage=["root-sess-100", "rotated-sess-101"])
    agent_rot = _make_agent_stub(run_agent.AIAgent)
    agent_rot.session_id = "rotated-sess-101"
    agent_rot.platform = "cli"
    agent_rot._gateway_session_key = None
    agent_rot._session_db = db_rot

    parent_scope_rot = resolve_prompt_cache_scope(agent_rot)
    assert parent_scope_rot == "root-sess-100"
    assert declared_conversation_scope(agent_rot) is None
    assert agent_rot._conversation_root_id() == "root-sess-100"

    with patch.object(run_agent, "AIAgent", _Recorder):
        fork_rot, _rt, routed = build_cache_parity_fork(agent_rot, max_iterations=5)
        assert not routed
        assert fork_rot._persist_disabled is True
        assert fork_rot._session_db is None
        assert fork_rot._gateway_session_key is None
        assert fork_rot._cached_conversation_root == "root-sess-100"
        assert fork_rot._inherited_cache_scope == "root-sess-100"
        # Declared scope is None (does NOT pollute affinity scope with raw session ID)
        assert declared_conversation_scope(fork_rot) is None
        # Prompt cache scope resolves to inherited root
        assert resolve_prompt_cache_scope(fork_rot) == "root-sess-100"
        # Conversation root preserved across detached DB
        assert fork_rot._conversation_root_id() == "root-sess-100"

    # Ambient turn parity for rotated parent vs fork
    t3 = set_conversation_context(agent_rot._conversation_root_id())
    a3 = set_affinity_scope(declared_conversation_scope(agent_rot))
    try:
        parent_rot_sticky = get_affinity_scope() or get_conversation_context() or agent_rot.session_id
        parent_rot_tags = [t for t in nous_portal_tags() if t.startswith("conversation=")]
    finally:
        reset_affinity_scope(a3)
        reset_conversation_context(t3)

    t4 = set_conversation_context(fork_rot._conversation_root_id())
    a4 = set_affinity_scope(declared_conversation_scope(fork_rot))
    try:
        fork_rot_sticky = get_affinity_scope() or get_conversation_context() or fork_rot.session_id
        fork_rot_tags = [t for t in nous_portal_tags() if t.startswith("conversation=")]
    finally:
        reset_affinity_scope(a4)
        reset_conversation_context(t4)

    assert fork_rot_sticky == parent_rot_sticky == "root-sess-100"
    assert fork_rot_tags == parent_rot_tags == ["conversation=root-sess-100"]

    # 3. Routed aux path (different model): must NOT inherit parent's cache scope
    routed_runtime = {
        "provider": "openrouter",
        "model": "aux-cheap-model",
        "api_key": "test-key",
        "base_url": None,
        "api_mode": None,
        "credential_pool": None,
        "request_overrides": {},
        "max_tokens": None,
        "command": None,
        "args": [],
        "routed": True,
    }

    with patch.object(run_agent, "AIAgent", _Recorder), \
         patch.object(bg_review, "_resolve_review_runtime", return_value=routed_runtime):
        fork_routed, _rt, routed = build_cache_parity_fork(agent_gw, max_iterations=5)
        assert routed is True
        assert getattr(fork_routed, "_inherited_cache_scope", None) is None
        assert getattr(fork_routed, "_cached_conversation_root", None) is None
        assert getattr(fork_routed, "_gateway_session_key", None) is None
        assert declared_conversation_scope(fork_routed) is None
        assert resolve_prompt_cache_scope(fork_routed) == fork_routed.session_id
