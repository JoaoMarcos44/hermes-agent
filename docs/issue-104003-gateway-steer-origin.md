# Issue #104003 — Gateway `/steer` origin metadata

Status: validated and fixed locally
Issue: https://github.com/NousResearch/hermes-agent/issues/104003
Baseline checked: `origin/main` at `245e48008f`

## Executive result

The report is real. A gateway event contains the requester’s routing identity in
`MessageEvent.source` and `MessageEvent.message_id`, but the busy-session paths
reduced the event to a bare string before calling the running agent. A CLI-origin
session has no persisted `origin_json` or `chat_id`, so the model had no reliable
reply destination and could guess a different group.

The fix keeps the existing `steer(text: str)` ABI and adds one canonical
runtime-boundary formatter. The formatter carries an allowlisted origin block
inside the existing steer payload before it enters the pending-steer drain. It
is used by the explicit `/steer` handler, normal busy-steer path, priority
busy-steer path, and the sibling redirect paths. Queue fallback still keeps the
original `MessageEvent`, so queued messages continue through the normal inbound
pipeline.

## Investigation receipt

### Remote issue and related work

- #104003 is open and includes a concrete incident involving a long-running CLI
  session steered from a messaging group.
- #97569 reports the adjacent sender-attribution loss on busy `interrupt` and
  `steer` paths. #101866 reports the corresponding reply-quote loss. Open PR
  #97617 extracts and applies sender/reply prefixes, but it does not provide a
  concrete `platform:chat_id` destination for a CLI-origin session. This fix is
  complementary and does not duplicate that work.
- #81828 and #65339 track the separate security problem that the plaintext
  out-of-band steer marker can be fabricated by a model. Open PR #82834 proposes
  structural runtime-owned user messages. This change does not claim to solve
  marker authenticity; it preserves the current marker delivery contract and
  adds only per-event routing data.
- Merged PR #101188 covers busy-steer preservation through compaction. The new
  origin block remains part of the existing steer text and therefore follows the
  same pending/drain path.
- Open PR #51312 covers a different no-tool-tail delivery gap. Open PRs #70406
  and #51126 cover broader exact-session/local IPC injection and CLI-session
  messaging. Neither is the narrow origin-loss fix described here.

### Graphify-assisted code tracing

The graph was queried before and after the code inspection with:

```text
graphify query "Where does the gateway /steer path pass MessageEvent origin metadata into AIAgent.steer?" --dfs
graphify path gateway.run_busy._busy_steer_command agent.interrupt_control.AIAgent.steer --undirected
graphify path gateway.run_inbound._hm_busy_steer agent.interrupt_control.AIAgent.steer --undirected
graphify affected agent_interrupt_control_interruptcontrolmixin_steer --relation calls --depth 2
graphify update . --no-cluster
```

The raw graph extraction was useful for locating `GatewayBusySessionMixin`,
`SessionSource`, and the gateway/agent neighborhoods, but its current raw graph
had no call edges for these dynamically dispatched methods. The final call-site
set was therefore verified by symbol search and direct source reads rather than
by assuming an incomplete graph edge.

### Line-level failure mechanism

On the checked baseline:

1. `gateway/run_busy.py` `_busy_steer_command` extracted
   `event.get_command_args().strip()` and called `running_agent.steer(text)`.
2. `gateway/run_busy.py` `_resolve_busy_steer_or_redirect` passed the prepared
   text to `_try_agent_verb`, which forwarded only that string.
3. `gateway/run_inbound.py` `_hm_busy_steer` independently passed
   `(event.text or "").strip()` to `running_agent.steer()`.
4. `agent/interrupt_control.py:216` accepted only `steer(self, text: str)`, and
   the pending-steer drain retained only text.
5. The event’s `source.platform`, `source.chat_id`, `source.user_id`, thread
   fields, and triggering `message_id` were consequently absent from the
   injected tool-result content.

A deterministic regression was added before the production fix. It failed with
the exact symptom: the mock received `"also check auth.log"` and no routing
metadata. The sibling priority-path regression failed for the same reason.

## Implemented solution

### Single canonical origin boundary

`gateway/run_busy.py` now owns two helpers:

- `_steer_origin_for_event(event)` copies only routing fields from the event:
  `platform`, `chat_id`, `thread_id`, `chat_type`, `user_id`, `message_id`,
  `scope_id`, and `profile`.
- `_steer_text_with_origin(text, event)` calls the one formatter and prepends the
  block exactly once before the agent receives the text.

`agent/prompt_builder.py:528` provides `format_steer_origin()`. The stable block
contains:

```text
[STEER ORIGIN — verified routing metadata]
delivery_target: <platform>:<chat_id>[:<thread_id>]
platform: <platform>
chat_id: <chat_id>
user_id: <user_id>
message_id: <message_id>
...
[/STEER ORIGIN]
```

The actual implementation emits only fields that exist. Missing or invalid
routing data produces an unavailable target instead of a guessed destination.

### Covered call paths

- `gateway/run_busy.py` `_busy_steer_command` — explicit `/steer`.
- `gateway/run_busy.py` `_resolve_busy_steer_or_redirect` — configured busy
  `steer` and active-turn `redirect` paths.
- `gateway/run_inbound.py` `_hm_busy_steer` — priority/fast-path steer.
- `gateway/run_inbound.py` `_hm_busy_interrupt` — priority/fast-path redirect.

Queue fallback is intentionally not preformatted: the original event remains
available to the normal inbound preparation path, avoiding duplicated sender or
reply-context logic.

### Safety and invariants

- No new environment variable, registry, or parallel routing store.
- No system-prompt rebuild; the origin block rides the per-message steer text,
  preserving the prompt-cache prefix.
- User-controlled metadata is flattened to single lines and delimiter lookalikes
  are defanged.
- Routing identifiers are never silently truncated. An oversized `chat_id` or
  `platform` yields an unavailable target; an oversized `thread_id` never falls
  back to the parent chat target.
- Empty text is returned unchanged, so origin metadata cannot turn an empty
  redirect into an accepted action.
- The existing plaintext steer-marker authenticity issue (#81828/#65339) remains
  a separate follow-up; this patch does not broaden that trust boundary.

## Verification

The focused regression was red before the production change:

```text
scripts/run_tests.sh tests/gateway/test_steer_command.py -k test_steer_injection_carries_the_requesting_chat_origin -q
→ exit 1

scripts/run_tests.sh tests/gateway/test_busy_session_ack.py -k test_steer_injection_carries_the_requesting_chat_origin -q
→ exit 1
```

The isolated clean worktree at `C:/Users/Nitro/hermes-agent-104003` then passed:

```text
scripts/run_tests.sh \
  tests/gateway/test_steer_command.py \
  tests/gateway/test_busy_session_ack.py \
  tests/gateway/test_multiplex_busy_input_mode.py \
  tests/gateway/test_subagent_protection_30170.py \
  tests/gateway/test_api_server_runs.py \
  tests/run_agent/test_steer.py \
  tests/agent/test_lock_fallback_base_semantics.py \
  tests/cli/test_cli_steer_busy_path.py -q
→ 8 files, 167 tests passed, 0 failed
```

A narrower rerun after the fail-closed empty/oversized-route checks passed:

```text
→ 4 files, 42 tests passed, 0 failed
```

The clean worktree was used because the primary checkout already contained
unrelated modified and untracked files. No unrelated files were reset, stashed,
or overwritten.

## Remaining scope

- The issue’s optional “home channel” fallback was not added. The event origin is
  already available for the reported gateway case; when it is absent or invalid,
  the implementation fails closed instead of selecting a different channel.
- #81828/#65339 still need the separate structural provenance fix proposed by
  #82834.
- The full repository suite was also attempted in the clean worktree with
  `scripts/run_tests.sh -q`, but exceeded the available 420-second execution
  window. It produced no usable completion status, so no claim is made that
  the full suite is green.
