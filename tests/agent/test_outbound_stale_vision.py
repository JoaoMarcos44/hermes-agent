"""Send-path eviction of stale vision_analyze / screenshot tool payloads.

Issue #89296: compression only retires older image-bearing tool results when
prune/compress fires, so OpenAI-style screenshots are re-serialized on every
later turn until a 413. ``evict_stale_outbound_tool_images`` is the
unconditional per-call chokepoint.

Issue #113517: that chokepoint must trigger on the provider limit and retire
whole batches; a keep-newest count or a single fixed batch rewrites the
Anthropic prompt-cache prefix every turn or lets the outbound payload grow
past the limit.
"""

from __future__ import annotations

from agent.agent_runtime_helpers import sanitize_api_messages
from agent.context_compressor import (
    _IMAGE_EVICTION_BATCH,
    _MAX_KEEP_TOOL_IMAGES,
    _OUTBOUND_IMAGE_BUDGET_BYTES,
    _OUTBOUND_IMAGE_LIMIT,
    _outbound_image_retire_count,
    _tool_content_has_images,
    evict_stale_outbound_tool_images,
)


def _image_tool(i: int, *, blob: str = "A" * 80) -> list[dict]:
    return [
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": f"call_{i}",
                    "type": "function",
                    "function": {
                        "name": "vision_analyze",
                        "arguments": f'{{"image_url":"shot{i}.png"}}',
                    },
                }
            ],
        },
        {
            "role": "tool",
            "tool_call_id": f"call_{i}",
            "content": [
                {"type": "text", "text": f"Image attached natively shot {i}"},
                {
                    "type": "image_url",
                    "image_url": {"url": f"data:image/png;base64,{blob}{i}"},
                },
            ],
        },
    ]


def _history_with_screenshots(n: int, *, blob: str = "A" * 80) -> list[dict]:
    msgs: list[dict] = [{"role": "user", "content": "look at these"}]
    for i in range(n):
        msgs.extend(_image_tool(i, blob=blob))
    msgs.append({"role": "user", "content": "compare them"})
    return msgs


def _image_bearing_tool_ids(messages: list[dict]) -> list[str]:
    return [
        m["tool_call_id"]
        for m in messages
        if m.get("role") == "tool" and _tool_content_has_images(m.get("content"))
    ]


class TestOutboundImageRetireCount:
    def test_nothing_to_drop_at_or_below_limit(self):
        assert _outbound_image_retire_count([]) == 0
        assert _outbound_image_retire_count([10] * _OUTBOUND_IMAGE_LIMIT) == 0

    def test_whole_batches_until_count_fits(self):
        sizes = [10] * 60
        for n in range(61):
            retire = _outbound_image_retire_count(sizes[:n])
            kept = n - retire
            assert kept <= _OUTBOUND_IMAGE_LIMIT
            if n <= _OUTBOUND_IMAGE_LIMIT:
                assert retire == 0
            elif retire < n:
                assert retire % _IMAGE_EVICTION_BATCH == 0

    def test_frontier_is_constant_inside_a_batch_window(self):
        sizes = [10] * 44
        first_kept = []
        for n in range(_OUTBOUND_IMAGE_LIMIT + 1, _OUTBOUND_IMAGE_LIMIT + 3 * _IMAGE_EVICTION_BATCH + 1):
            retire = _outbound_image_retire_count(sizes[:n])
            first_kept.append(retire)
        windows = [
            first_kept[i : i + _IMAGE_EVICTION_BATCH]
            for i in range(0, 3 * _IMAGE_EVICTION_BATCH, _IMAGE_EVICTION_BATCH)
        ]
        for window in windows:
            assert len(set(window)) == 1, window
        assert first_kept[0] < first_kept[_IMAGE_EVICTION_BATCH] < first_kept[2 * _IMAGE_EVICTION_BATCH]

    def test_byte_budget_forces_batches_even_when_count_fits(self):
        huge = [_OUTBOUND_IMAGE_BUDGET_BYTES // 2 + 1] * 9
        assert len(huge) <= _OUTBOUND_IMAGE_LIMIT
        retire = _outbound_image_retire_count(huge)
        kept = 9 - retire
        assert kept <= _OUTBOUND_IMAGE_LIMIT
        assert sum(huge[:kept]) <= _OUTBOUND_IMAGE_BUDGET_BYTES
        assert retire >= _IMAGE_EVICTION_BATCH

    def test_part_weights_count_as_api_blocks(self):
        sizes = [10] * 20
        weights = [2] * 20
        retire = _outbound_image_retire_count(sizes, weights_newest_first=weights)
        kept = 20 - retire
        assert sum(weights[:kept]) <= _OUTBOUND_IMAGE_LIMIT
        assert retire >= _IMAGE_EVICTION_BATCH

    def test_reserved_images_consume_the_ceiling(self):
        sizes = [10] * 10
        assert len(sizes) <= _OUTBOUND_IMAGE_LIMIT
        retire = _outbound_image_retire_count(sizes, reserved_count=15)
        kept = 10 - retire
        assert kept >= _MAX_KEEP_TOOL_IMAGES
        assert kept < 10
        assert kept + 15 <= _OUTBOUND_IMAGE_LIMIT

    def test_reserved_over_limit_never_strips_newest_tool_images(self):
        """User uploads may fill the API ceiling; the newest screenshots must still reach the model."""
        sizes = [10] * 4
        retire = _outbound_image_retire_count(sizes, reserved_count=_OUTBOUND_IMAGE_LIMIT + 1)
        kept = 4 - retire
        assert kept == _MAX_KEEP_TOOL_IMAGES
        assert retire == 1

    def test_floor_does_not_shelter_a_violation_tool_content_can_fix(self):
        """The keep floor only overrides the limit when reserved uploads make it unreachable."""
        # Surviving tool blocks alone exceed the block limit: one 25-block tool_result must go.
        assert _outbound_image_retire_count([10], weights_newest_first=[25]) == 1
        # Tool bytes + reserved bytes over budget while reserved alone fits: retire past the floor.
        mb = 1_000_000
        assert _outbound_image_retire_count([5 * mb] * 4, reserved_bytes=20 * mb) == 4
        # Reserved bytes alone already blow the budget: no retirement can fix it, floor wins.
        assert _outbound_image_retire_count([5 * mb] * 4, reserved_bytes=_OUTBOUND_IMAGE_BUDGET_BYTES + 1) == 1


class TestOutboundStaleVisionEviction:
    def test_sanitize_alone_keeps_every_screenshot(self):
        """The previous send chokepoint does not close #89296 by itself."""
        history = _history_with_screenshots(5)
        sanitized = sanitize_api_messages(history)
        assert _image_bearing_tool_ids(sanitized) == [f"call_{i}" for i in range(5)]

    def test_nothing_is_evicted_below_the_provider_limit(self):
        """The common case must be append-only: no rewrite, so the cached prefix survives."""
        history = _history_with_screenshots(_OUTBOUND_IMAGE_LIMIT)
        outbound = sanitize_api_messages(history)
        assert evict_stale_outbound_tool_images(outbound) == 0
        assert _image_bearing_tool_ids(outbound) == [
            f"call_{i}" for i in range(_OUTBOUND_IMAGE_LIMIT)
        ]

    def test_eviction_retires_a_batch_once_over_the_limit(self):
        n = _OUTBOUND_IMAGE_LIMIT + 1
        history = _history_with_screenshots(n)
        outbound = sanitize_api_messages(history)
        pruned = evict_stale_outbound_tool_images(outbound)
        assert pruned == _IMAGE_EVICTION_BATCH
        kept = _image_bearing_tool_ids(outbound)
        assert kept == [f"call_{i}" for i in range(_IMAGE_EVICTION_BATCH, n)]

        oldest = next(m for m in outbound if m.get("tool_call_id") == "call_0")
        assert isinstance(oldest["content"], list)
        assert not _tool_content_has_images(oldest["content"])
        assert any(
            isinstance(part, dict)
            and part.get("type") == "text"
            and "screenshot removed" in str(part.get("text", ""))
            for part in oldest["content"]
        )

    def test_outbound_never_exceeds_limit_across_batch_windows(self):
        for n in (
            _OUTBOUND_IMAGE_LIMIT,
            _OUTBOUND_IMAGE_LIMIT + 1,
            _OUTBOUND_IMAGE_LIMIT + _IMAGE_EVICTION_BATCH,
            _OUTBOUND_IMAGE_LIMIT + _IMAGE_EVICTION_BATCH + 1,
            40,
            60,
        ):
            outbound = sanitize_api_messages(_history_with_screenshots(n))
            evict_stale_outbound_tool_images(outbound)
            kept = _image_bearing_tool_ids(outbound)
            assert len(kept) <= _OUTBOUND_IMAGE_LIMIT, (n, len(kept))

    def test_frontier_holds_between_batch_advances(self):
        """The rewritten set must not move on every new image.

        A frontier that advances one step per image edits an already-cached row each
        turn, so Anthropic re-writes the entire prompt-cache prefix instead of reading
        it — orders of magnitude more expensive than the image tokens reclaimed.
        """
        from agent.conversation_loop import _clone_message_for_send

        def first_surviving(n: int) -> str:
            msgs = [_clone_message_for_send(m) for m in _history_with_screenshots(n)]
            evict_stale_outbound_tool_images(msgs)
            return _image_bearing_tool_ids(msgs)[0]

        start = _OUTBOUND_IMAGE_LIMIT + 1
        end = _OUTBOUND_IMAGE_LIMIT + 3 * _IMAGE_EVICTION_BATCH
        frontier = [first_surviving(n) for n in range(start, end + 1)]
        held = sum(a == b for a, b in zip(frontier, frontier[1:]))
        assert held >= len(frontier) - 3, (
            f"eviction advanced on nearly every image (frontier={frontier}); "
            "each advance rewrites a cached row and restarts the prefix"
        )
        assert len(set(frontier)) >= 3, frontier

    def test_does_not_rewrite_persisted_history(self):
        from agent.conversation_loop import _clone_message_for_send

        n = _OUTBOUND_IMAGE_LIMIT + 1
        history = _history_with_screenshots(n)
        outbound = [_clone_message_for_send(m) for m in history]
        evict_stale_outbound_tool_images(outbound)
        assert _image_bearing_tool_ids(history) == [f"call_{i}" for i in range(n)]
        assert _image_bearing_tool_ids(outbound) == [
            f"call_{i}" for i in range(_IMAGE_EVICTION_BATCH, n)
        ]

    def test_user_uploads_are_not_evicted(self):
        history = [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "look"},
                    {
                        "type": "image_url",
                        "image_url": {"url": "data:image/png;base64,USERUPLOAD"},
                    },
                ],
            }
        ]
        for i in range(_OUTBOUND_IMAGE_LIMIT + 2):
            history.extend(_image_tool(i))
        outbound = sanitize_api_messages(history)
        evict_stale_outbound_tool_images(outbound)
        user = next(m for m in outbound if m.get("role") == "user")
        assert user["content"][1]["image_url"]["url"].endswith("USERUPLOAD")

    def test_user_uploads_count_toward_limit_without_being_rewritten(self):
        history: list[dict] = []
        for u in range(15):
            history.append(
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": f"look {u}"},
                        {
                            "type": "image_url",
                            "image_url": {"url": f"data:image/png;base64,USER{u}"},
                        },
                    ],
                }
            )
        for i in range(10):
            history.extend(_image_tool(i))
        outbound = sanitize_api_messages(history)
        evict_stale_outbound_tool_images(outbound)
        user_urls = [
            part["image_url"]["url"]
            for m in outbound
            if m.get("role") == "user" and isinstance(m.get("content"), list)
            for part in m["content"]
            if isinstance(part, dict) and part.get("type") == "image_url"
        ]
        assert len(user_urls) == 15
        kept_tools = _image_bearing_tool_ids(outbound)
        assert len(kept_tools) + 15 <= _OUTBOUND_IMAGE_LIMIT
        assert len(kept_tools) < 10
        assert len(kept_tools) >= _MAX_KEEP_TOOL_IMAGES

    def test_user_uploads_filling_ceiling_keep_newest_screenshots(self):
        history: list[dict] = []
        for u in range(_OUTBOUND_IMAGE_LIMIT + 1):
            history.append(
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": f"look {u}"},
                        {
                            "type": "image_url",
                            "image_url": {"url": f"data:image/png;base64,USER{u}"},
                        },
                    ],
                }
            )
        history.extend(_image_tool(0))
        outbound = sanitize_api_messages(history)
        evict_stale_outbound_tool_images(outbound)
        assert _image_bearing_tool_ids(outbound) == ["call_0"]
        user_images = [
            part
            for m in outbound
            if m.get("role") == "user" and isinstance(m.get("content"), list)
            for part in m["content"]
            if isinstance(part, dict) and part.get("type") == "image_url"
        ]
        assert len(user_images) == _OUTBOUND_IMAGE_LIMIT + 1
