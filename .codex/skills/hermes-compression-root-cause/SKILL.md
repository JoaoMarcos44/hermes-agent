# Hermes Compression Root-Cause Skill

Use this skill when a Hermes compression failure is being misclassified, retried on the wrong ladder, or escalated into session loss.

## Core invariant

Classify at the owner that created the failure signal, then let downstream compression policy consume that class. Do not create a second taxonomy in the compressor when the auxiliary transport already owns one.

## Workflow

1. Pin the exact issue, PR head, and current main SHA.
2. Trace the failure from provider/stream guard -> exception type -> transport classifier -> summary classifier -> cooldown/fallback flag -> session/gateway outcome.
3. Separate overlapping taxonomies:
   - timeout: full/no-progress budget exhausted;
   - connection: transport closed/unreachable;
   - auth/quota: terminal access failure;
   - overload: transient capacity failure;
   - malformed/empty/truncated: content-quality failures.
4. When helpers intentionally overlap (for example a timeout is also connection-like for transport failover), establish explicit precedence at the downstream policy boundary instead of changing the shared helper.
5. Reuse the canonical helper. Avoid message-only special cases when the producer already raises a typed exception.
6. Prove the state transition, not only the boolean classifier:
   - timeout streak increments;
   - terminal network flag stays clear for a timeout;
   - default abort gate remains open for the existing deterministic fallback.
7. Keep regression setup isolated. Never add contributor-local venv/import warmups or private dependency APIs to collection-time test module state.
8. Compare competing PRs with a real differential invariant. Proceed only if the patch is canonical, objectively more complete/safer, or independently complementary.
9. Run the standard three blocker sweeps after the final commit.

## Scope discipline

A prompt/template bug that makes a provider more likely to stall is a trigger. A stream-guard exception classification bug is the failure owner. Fix each at its own boundary unless one change demonstrably owns both.
