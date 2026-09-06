# Approval-Gate Self-Mutation: Fix Summary

## Overview

A generic configuration command could persist a security policy that controls the agent's own approval behavior. The fix makes policy writes operator-only at the shared configuration writer, detects the command before execution, and prevents one approval from becoming a reusable allowlist entry.

## Learning Objectives

The viewer will understand:

1. The reported path was real because `set_config_value` persisted arbitrary dotted keys and the dangerous-command detector did not classify the policy mutation.
2. The shared writer now rejects policy keys unless a scoped operator-mediated path authorizes the write.
3. The operator path remains available, while session and permanent approval caches cannot authorize future policy mutations.

---

## Section 1: Reported Path

**Key Concept**: The agent could target the policy that governs its own single-query approval behavior.

**Content**:
- `hermes config set approvals.single_query_mode approve`
- `approvals.single_query_mode`
- The setting controls dangerous-command behavior for single-query sessions.

**Visual Element**:
- Type: red flow arrow
- Subject: Agent command entering the generic config writer
- Treatment: Mark the path as rejected at the security boundary

**Text Labels**:
- Headline: "Reported self-mutation path"
- Labels: "Agent", "config set", "approvals.single_query_mode", "Self-approval risk"

---

## Section 2: Root Cause

**Key Concept**: The generic writer had no security-policy authorization check, and command detection had no matching rule.

**Content**:
- Generic dotted-key persistence accepted `approvals.*` without an operator capability.
- Alternate supported CLI entrypoints reached the same writer.
- Approval decisions could otherwise be stored for later reuse.

**Visual Element**:
- Type: structural breakdown
- Subject: Detector and writer shown as two missing gates around one persistence boundary
- Treatment: Use a highlighted gap between command input and config write

**Text Labels**:
- Headline: "Root cause"
- Labels: "Unclassified command", "Generic writer", "No policy authorization", "Persistent state"

---

## Section 3: Root Fix

**Key Concept**: One canonical policy-key predicate protects the shared write boundary; the operator path uses a scoped authorization context.

**Content**:
- Protected key families: `approvals.*`, `security.*`, `command_allowlist`, `yolo`.
- Detector coverage: `hermes config set/unset` and `python -m hermes_cli.main ...`.
- Dedicated operator-mediated approval-mode changes continue through the canonical writer.

**Visual Element**:
- Type: blue shield around a code module
- Subject: Detector → policy-key guard → atomic config write
- Treatment: Show ordinary configuration bypassing the policy guard and policy configuration stopping at the shield

**Text Labels**:
- Headline: "Operator-only policy boundary"
- Labels: "Detect", "Classify key", "Scoped operator write", "Atomic persistence"

---

## Section 4: Assurance and Cost

**Key Concept**: Policy approval is one-shot, and the guard adds constant-time overhead without a global lock.

**Content**:
- Security-policy approvals are never stored in session or permanent allowlists.
- Policy-key classification is `O(1)` with a fixed prefix/name set, aside from key length.
- The completed Graphify index contains `218,934 nodes` and `477,030 edges`.

**Visual Element**:
- Type: compact assurance panel
- Subject: one-shot stamp, complexity badge, and verification checklist
- Treatment: Green checks for regression coverage and existing behavior preservation

**Text Labels**:
- Headline: "Assurance"
- Labels: "One-shot only", "O(1) guard", "Regression tests", "Graphify traced"

---

## Data Points (Verbatim)

- `hermes config set approvals.single_query_mode approve`
- `approvals.single_query_mode`
- `approvals.*`
- `security.*`
- `command_allowlist`
- `O(1)`
- `218,934 nodes`
- `477,030 edges`

## Design Instructions

- English text only.
- Use a technical blueprint / security schematic look.
- Use red for the rejected agent path, blue for the trusted boundary, and green for verified safeguards.
- Keep all labels short enough to remain legible.
- Do not include credentials, tokens, or unverified incident statistics.
