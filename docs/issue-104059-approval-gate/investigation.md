# Issue #104059 — Approval-Gate Self-Mutation Investigation

## Verdict

The reported defect is real on the pre-fix `origin/main` revision `089bb32886c8c18f7fa20182c7bf8826d6935ac5`.

The exact reported command was accepted by the generic configuration writer and persisted `approvals.single_query_mode`. That setting controls how a single-query session handles dangerous-command approval. The command therefore targeted the policy that governs the agent's own approval behavior.

This was a real authorization gap, not only a prompt-injection concern or a documentation gap.

## Reproduction and proof

The minimal pre-fix path was:

1. Start with an isolated `HERMES_HOME`.
2. Call the same `hermes_cli.config.set_config_value` path used by `hermes config set`.
3. Set `approvals.single_query_mode` to `approve`.
4. Read the resulting config file.

Before the fix, the writer reported success and the key was present in the isolated config. The pre-fix sabotage run restored the production files while retaining the new regression tests: the writer tests failed on every protected-key case and the command-approval tests failed because the command was not detected, the denial text advertised the bypass, and approvals could be cached. This proves the regression tests bite on the old behavior.

After the fix, the same isolated writer path exits with code `1`, reports that the key is operator-only, and leaves the config file unchanged. Non-security configuration writes still persist normally, and a non-security write preserves an already-existing approval policy.

## Root cause

The relevant data flow was:

```text
agent terminal command
  -> tools.approval.detect_dangerous_command
  -> hermes config subcommand / console engine
  -> hermes_cli.config.set_config_value
  -> config.yaml
  -> tools.approval_context._get_single_query_approval_mode
```

The command detector had no rule for policy mutations through `hermes config set`. The shared writer accepted arbitrary dotted keys and had no caller-capability check. `--force` was an unrelated scalar-overwrite/unknown-key option, but there was no security-policy boundary separating it from policy writes. A direct raw writer in the TUI config setter also persisted `approvals.mode` without using the canonical approval-mode path.

The unattended denial text also named the exact configuration change that would lift the gate, turning a safety failure into an actionable bypass recipe for the agent.

## Implemented solution

### 1. Shared config-write boundary

`hermes_cli.config` now classifies `approvals.*`, `security.*`, `command_allowlist` (including dotted children), and persistent `yolo` as security-policy keys. `set_config_value` and `unset_config_value` reject these keys unless the scoped operator-mediated approval-mode context is active.

The generic `force` argument cannot authorize a policy write. No new environment variable or credential is used.

### 2. Scoped operator path

`hermes_cli.approval_mode.run_approval_mode_command` remains the canonical writer for the persistent approval mode and enters the short-lived operator context only around that write. The context is reset in a `finally` block and is not stored in configuration or carried into a later turn.

The TUI's persistent approval-mode and global YOLO setters now use this same canonical path instead of a second raw `config.yaml` writer. This removes a sibling bypass without duplicating authorization logic.

### 3. Command detection

The dangerous-command detector recognizes policy mutations through both supported forms:

- `hermes config set/unset ...`
- `python -m hermes_cli.main ... config set/unset ...`

It handles profile/global flags, quoted policy keys, and `--force`. The detector's policy-mutation key is shared with the approval layer's one-shot classification.

### 4. One-shot approval semantics

A human may approve one policy mutation, but the policy-mutation pattern is never inserted into a session or permanent allowlist. Gateway approval payloads expose only the one-shot choice for this pattern. Existing cached entries for this pattern are ignored when checking approvals and are filtered during load.

### 5. Gateway authorization

The persistent `/approvals <mode>` mutation now requires both an enabled gateway admin policy and an admin identity. Querying `/approvals` remains available when no admin policy is configured.

### 6. Diagnostic message

Single-query and unattended denial messages no longer tell the agent to set `approvals.single_query_mode: approve`. They require an operator-controlled session instead.

## Related work and duplication check

Open related work was searched by issue number and mechanism. PR #81108 contains an earlier candidate for the same security-policy writer boundary, and its review history was read before implementing this branch. This branch is a current-main replacement rather than a blind duplicate: it uses the current refactored config/TUI layout, keeps authorization scoped instead of exposing a boolean writer override, routes the TUI raw writer through the canonical path, and adds the exact `single_query_mode` regression plus one-shot behavior checks.

The existing candidate should not be merged independently of this current-main implementation if its branch does not contain these current-tree protections. The PR body links the relationship explicitly so maintainers can supersede or consolidate it without losing the original contributor's credit.

## Algorithm and performance

The fix reduces key authorization to a constant-size name/prefix classification. For a key of length `L`, classification is `O(L)` because normalization must inspect the key; the number of protected families is constant, so it is `O(1)` with respect to the number of configuration entries. The context authorization read/write is `O(1)`.

Command detection already scans its fixed pattern table and command variants. The two policy-entrypoint patterns add a constant number of compiled regex checks; they do not scan the config file or acquire a new global lock. The existing atomic configuration writer and lock remain the only persistence synchronization mechanism.

A full configuration-tree scan, a second policy registry, or a global lock would add more work and more state without improving the security invariant. The selected algorithm is therefore the smallest complete constant-overhead guard at the shared write boundary.

As a local CPython measurement on the Windows development host, one million protected-key classifications took `0.615291` seconds (`615.3 ns/call`) and one million ordinary-key classifications took `0.650326` seconds (`650.3 ns/call`). One hundred thousand ordinary approval lookups took `0.064961` seconds (`0.65 µs/call`). These are directional microbenchmarks, not CI thresholds; they confirm that the added classification is negligible relative to config parsing and disk I/O.

## Verification map

| Acceptance criterion | Evidence |
|---|---|
| Exact policy write rejected | `tests/hermes_cli/test_security_config_write_guard.py` |
| `--force` cannot bypass | `test_config_set_command_rejects_force_for_single_query_policy` |
| `unset` cannot remove policy | `test_unset_refuses_security_policy_keys` |
| Ordinary config writes preserved | `test_non_security_config_writes_are_unchanged` |
| Existing policy preserved | `test_non_security_write_preserves_existing_approval_policy` |
| Policy commands detected | `tests/tools/test_security_config_approval.py` |
| Denial text does not advertise bypass | `test_single_query_denial_does_not_advertise_the_mutation_bypass` |
| Policy approval remains one-shot | `test_security_policy_approval_is_one_shot` |
| Gateway disabled-policy rejection | `tests/gateway/test_approvals_command.py` |
| TUI persistent mode remains functional | `tests/test_tui_gateway_server.py` approval-mode tests |
| Windows verification cleanup remains safe | `tests/tools/test_approval.py` |

## Graphify evidence

`graphify update 'C:/Users/Nitro/hermes-agent-104059' --no-cluster` completed with `218934 nodes` and `477030 edges`. Graphify was used to explain `set_config_value`, trace the approval/config relationship, and bound the affected caller set before the patch. The index reported four unrelated source files with syntax errors and partially extracted them; those files are outside this change.

## Security considerations

- No secret, token, or credential is stored in the patch or documentation.
- Policy state remains in `config.yaml`; no new `HERMES_*` behavioral setting was introduced.
- The generic writer fails closed for protected keys.
- The dedicated operator context is scoped and reset even on failure.
- Direct operator maintenance of policy remains possible through the operator-controlled configuration path; agent-reachable generic writers cannot authorize it.
- Hardline commands and existing approval floors remain unchanged.
