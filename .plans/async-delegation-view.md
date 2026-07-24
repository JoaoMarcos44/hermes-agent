# Async Delegation View (docked `agents` panel + live steering)

## Motivation

Today, background delegation (`delegate_task(background=true)`) and the
subagent tree live in two disconnected places:

- The **parked turn** surfaces only as a one-line status hint
  (`↩ resumes when N subagents finish`, `appChrome.tsx:566`).
- The rich subagent tree is a **full-screen modal** (`/agents`,
  `agentsOverlay.tsx`) that hides the conversation while open.

There is no way to *watch* background agents next to the conversation, and no
way to *steer* one mid-flight. If a background `fixer` picks the wrong
approach, the only lever is `x` (kill) — you can't say "prefer a sliding
window over bucket refill, switch approach" and let it keep going.

This plan adds an **inline, docked `agents` panel** (like `LiveTodoPanel`)
that rides above the composer, plus a **steering channel** so the user can
send a message into a running subagent by id (`@fixer …`).

## What It Enables

```
 ● delegate_task(fixer · patch token-bucket refill race)
     spawned in background · id b7c2 · depth 1

 ┌ agents · 2 running · 1 done ──────── esc back · ⏎ send → fixer · x kill · ^a tree ┐
 │ 1 ● researcher  map auth handshake edge cases            2m14s   read_file        │
 │ 2 ● fixer       patch token-bucket refill race           1m02s   bash             │
 │     bash(pytest tests/gateway/test_rate_limit.py -x) · running 12s                 │
 │     note: refill race reproduced at burst=64 · drafting fix in limiter.py          │
 │ 3 ✓ tests       regression sweep on gateway/             44s     result ready ⏎    │
 └───────────────────────────────────────────────────────────────────────────────────┘
 @fixer prefer a sliding window over bucket refill — switch approach
```

- Watch running background agents **without leaving the conversation**.
- Inject a resolved background result into the chat (`⏎` on `result ready`).
- **Steer a running subagent** by id (`@fixer …`) — new capability.
- `^a` expands to the existing full `/agents` tree; `x` kills the selection.

## Current-State Inventory (what already exists — reuse, do not rebuild)

| Capability | Where | Reuse as |
|---|---|---|
| Async dispatch, parked-turn resume | `tools/async_delegation.py:438` `dispatch_async_delegation`; watcher `gateway/run.py:8072` | Data source for "N running / done" |
| Async records snapshot | `async_delegation.py:835` `list_async_delegations`, `:410` `active_count` | Panel header + `result ready` rows |
| Live subagent event stream | `subagent.start/tool/progress/complete` → `turnStore.subagents` (`turnStore.ts:79`, `SubagentProgress` in `types.ts:23`) | Per-agent live rows (tool, elapsed) |
| Full tree overlay | `components/agentsOverlay.tsx`, opened via `openAgentsOverlay()` | `^a tree` action |
| Kill / pause / status RPCs | `tui_gateway/server.py:9308` (`delegation.status`, `delegation.pause`, `subagent.interrupt`) | `x kill`, backend for panel |
| Live agent registry (id → `AIAgent`) | `delegate_tool.py:150` `_active_subagents`, `:183` `interrupt_subagent` | **Steering hook** (Phase 2) |
| Docked panel pattern | `LiveTodoPanel` (`streamingAssistant.tsx:102`), mounted in `appLayout.tsx:238` | Panel shell + collapse toggle |
| Status-line resume hint | `appChrome.tsx:556-566` (`usage.active_subagents`) | Already-shipped Mockup-1 footer |

**Key insight:** ~80% is reprojection of existing data. The one genuinely new
backend capability is inbound steering (Phase 2), and even that reuses the
existing `_active_subagents` registry and the child's iteration-boundary
interrupt check.

---

## Phase 1 — Docked read-only panel + existing actions (low risk)

Delivers the review value of the mockup using only infrastructure that
already exists.

### 1.1 Backend: expose async-delegation snapshot to the TUI
New thin RPC in `tui_gateway/server.py` next to the existing delegation
handlers (`:9308`):

```python
@method("delegation.async_list")
def _(rid, params: dict) -> dict:
    from tools.async_delegation import list_async_delegations, active_count
    return _ok(rid, {"delegations": list_async_delegations(), "running": active_count()})
```

- `list_async_delegations()` already strips the non-serialisable
  `interrupt_fn` and returns `goal/status/depth/role/model/delegation_id/
  dispatched_at/completed_at`.
- No new state; purely a read projection.

### 1.2 Frontend store
- Extend `ui-tui/src/app/delegationStore.ts` with an `$asyncDelegations` atom
  and an `applyAsyncList()` merger (mirror the existing
  `applyDelegationStatus`).
- Add the RPC response type to `gatewayTypes.ts` beside
  `DelegationStatusResponse` (`:508`).

### 1.3 Panel component `components/agentsPanel.tsx`
- Model it on `TodoPanel`/`LiveTodoPanel` (collapsed + `onToggle`, themed).
- Rows come from **two merged sources**:
  - live in-turn children: `useTurnSelector(s => s.subagents)` (running,
    with live `tools.at(-1)` + elapsed — the `fixer · bash · 12s` line).
  - async background/done: `$asyncDelegations` (the `result ready` rows).
- Reuse row helpers from `lib/subagentTree.ts` (`fmtDuration`, status glyphs
  map in `agentsOverlay.tsx:91`) — extract `STATUS_GLYPH` to a shared module
  so panel and overlay stay visually identical.
- Header: `agents · {running} running · {done} done`.

### 1.4 Mount + open/collapse
- Mount in `appLayout.tsx` alongside `LiveTodoPanel` (`:238`), gated on
  "has any live or async agent this turn".
- Collapse state in `overlayStore.ts` (follow `todoCollapsed`).

### 1.5 Actions wired to existing RPCs
- `x` on selected row → `gw.request('subagent.interrupt', {subagent_id})`
  (already exists, `server.py:9336`).
- `^a` → `openAgentsOverlay()` (existing) for the full tree.
- `⏎` on a `result ready` async row → trigger injection of its completion.
  *If* the completion has already been drained onto `completion_queue`, this
  is a no-op/focus; otherwise expose a `delegation.async_inject` RPC that
  forces the pending completion event for that `delegation_id` (small).

### 1.6 Tests
- `agentsPanel` render test mirroring `appChromeStatusRule.test.tsx` (running
  vs done counts, empty state).
- Backend: unit test `delegation.async_list` shape.

**Phase 1 exit:** panel visible next to chat, live rows update, kill + tree +
result-ready all functional. No changes to the agent loop.

---

## Phase 2 — Live steering (`@fixer …`) (higher risk, core loop)

The new capability. The registry and iteration-boundary hook already exist;
the work is an **inbound message channel** on the child agent.

### 2.1 Inbound queue on the child agent
- In `delegate_tool.py`, the `_active_subagents[sid]` record already holds the
  live `agent` (`AIAgent`) — the same object `interrupt_subagent` calls
  `agent.interrupt()` on (`:195-199`).
- Add a thread-safe inbound queue to `AIAgent` (or the record) and a
  `send_to_subagent(subagent_id, text) -> bool` that looks the child up the
  same way `interrupt_subagent` does.

### 2.2 Drain at the iteration boundary
- The child loop already stops "at its next iteration boundary" for
  interrupts (`delegate_tool.py:184`). At that **same** boundary, drain the
  inbound queue and append the text **as a clean user turn** in the child's
  conversation.
- This respects the invariant the async design was built around
  (`async_delegation.py:11-13`): never splice between a tool-result and an
  assistant message; the steer enters as a fresh, role-legal user turn so the
  prompt cache and role alternation stay intact.
- Guard: if the child is mid-tool, buffer until the boundary (never mutate
  in-flight context).

### 2.3 Gateway RPC
```python
@method("subagent.send")
def _(rid, params: dict) -> dict:
    from tools.delegate_tool import send_to_subagent
    sid = str(params.get("subagent_id") or "").strip()
    text = str(params.get("text") or "")
    if not sid or not text:
        return _err(rid, 4000, "subagent_id and text required")
    return _ok(rid, {"delivered": send_to_subagent(sid, text)})
```

### 2.4 Composer routing (`@fixer …`)
- In the panel's input mode, `⏎` with a selected agent, or a `@<name|id>`
  prefix in the composer, routes to `subagent.send` instead of the main turn.
- Resolve `@name` → `subagent_id` via the panel's current row set (the
  header already shows `⏎ send → fixer`).
- Feedback: flash "delivered → fixer" or "fixer already finished".

### 2.5 Edge cases + tests
- Target finished/failed between keystroke and send → report gracefully.
- Steer a *sync* (foreground) child vs *async* background child — both live
  in `_active_subagents`; confirm both paths.
- Depth/permission: only allow steering agents owned by the current session
  (reuse the session-ownership selectors in
  `async_delegation.interrupt_for_session`).
- Tests: queue drain appends exactly one user turn; role alternation stays
  legal; delivery to a dead id returns `delivered=false`.

**Phase 2 exit:** `@fixer prefer a sliding window …` reaches the running
child and changes its next iteration.

---

## Files touched (summary)

**Phase 1**
- `tui_gateway/server.py` — `delegation.async_list` (+ optional
  `delegation.async_inject`)
- `ui-tui/src/app/delegationStore.ts` — `$asyncDelegations`, `applyAsyncList`
- `ui-tui/src/gatewayTypes.ts` — response type
- `ui-tui/src/components/agentsPanel.tsx` — **new**
- `ui-tui/src/components/agentsOverlay.tsx` — extract shared `STATUS_GLYPH`
- `ui-tui/src/components/appLayout.tsx` — mount panel
- `ui-tui/src/app/overlayStore.ts` — collapse state
- tests

**Phase 2**
- `run_agent.py` (`AIAgent`) — inbound queue + boundary drain
- `tools/delegate_tool.py` — `send_to_subagent`
- `tui_gateway/server.py` — `subagent.send`
- `ui-tui/src/components/agentsPanel.tsx` + composer routing
- tests

## Risks & mitigations

- **Context-invariant violation (Phase 2)** — inject only as a fresh user
  turn at an iteration boundary; never mid-tool. This is the whole reason the
  async design avoided reaching into live loops; honour it explicitly.
- **Panel noise** — collapse by default when >N agents; reuse `TodoPanel`
  collapse ergonomics.
- **Visual drift from `/agents`** — share the glyph/format helpers so panel
  and overlay never diverge.
- **Steering a dead/foreign agent** — id lookup + session-ownership guard,
  graceful "already finished" feedback.

## Recommended sequencing

Ship **Phase 1** first (self-contained, no core-loop risk — most of the
review value). Land it, then take **Phase 2** as a separate change so the
agent-loop modification can be reviewed and tested in isolation.
