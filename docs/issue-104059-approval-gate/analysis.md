---
title: "Preventing approval-gate self-mutation"
topic: "security / configuration authorization"
data_type: "system architecture and process"
complexity: "complex"
point_count: 8
source_language: "English"
user_language: "Portuguese"
---

## Main Topic

The approval policy is security-sensitive state. The defect was an authorization gap at the generic configuration writer: the agent could reach the same persisted configuration used by the approval guard, while the command detector did not identify the mutation as security-sensitive.

## Learning Objectives

After viewing this infographic, the viewer should understand:

1. Why the reported self-mutation path was real.
2. Where the trusted boundary now rejects the write.
3. Why the operator path remains available without creating a reusable bypass.

## Target Audience

- **Knowledge Level**: Intermediate software engineers and security reviewers
- **Context**: Reviewing the issue, patch, and regression evidence
- **Expectations**: A concise, accurate explanation of the root cause, fix, and cost

## Content Type Analysis

- **Data Structure**: A before/after security flow with a shared writer and multiple entrypoints
- **Key Relationships**: Agent command → detector → config writer → approval policy; operator command → scoped authorization → same writer
- **Visual Opportunities**: Split the unsafe path from the protected path; show the constant-time policy-key decision and one-shot approval rule

## Key Data Points (Verbatim)

- `hermes config set approvals.single_query_mode approve`
- `approvals.single_query_mode`
- `approvals.*`
- `security.*`
- `command_allowlist`
- `O(1)` policy-key classification
- `218,934 nodes` and `477,030 edges` in the completed Graphify index

## Layout × Style Signals

- Content type: system/structure → `structural-breakdown` or `bento-grid`
- Tone: security review → `technical-schematic`
- Audience: engineers and reviewers → readable labels, restrained visual hierarchy
- Complexity: complex → four clearly separated modules, not a dense paragraph

## Recommended Combination

1. **Bento grid + technical schematic** (recommended): separates finding, root cause, fix, and assurance while preserving the security-review tone.
2. **Binary comparison + blueprint**: emphasizes the rejected agent path versus the operator path.
3. **Linear progression + IKEA manual**: emphasizes the guarded execution sequence.

## Design Instructions

- All visible text must be in English.
- Use verified terms only; do not invent incident counts, exploit telemetry, or performance measurements.
- Make `approvals.single_query_mode` legible and do not display credentials.
