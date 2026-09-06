# Source: Issue #104059

## Reported behavior

Issue #104059 reports that an agent can run:

```text
hermes config set approvals.single_query_mode approve
```

The reported consequence is that a single-query session can change the policy that decides whether its own dangerous commands require approval.

## Scope

This artifact records only verified repository behavior and the requested security outcome. It contains no credentials, tokens, or private configuration values.

## Verified acceptance criteria

1. An agent-reachable generic configuration writer cannot set or unset `approvals.*`, `security.*`, `command_allowlist`, or the persistent `yolo` policy.
2. The exact `hermes config set approvals.single_query_mode approve` path is rejected.
3. The dangerous-command detector recognizes supported CLI entrypoints that target these policy keys.
4. A human approval for a policy mutation is one-shot and cannot become a session or permanent allowlist entry.
5. The dedicated operator-mediated approval-mode path continues to work.
6. A disabled gateway admin policy cannot authorize a persistent approval-mode change.
7. Existing non-security configuration writes retain their behavior.
