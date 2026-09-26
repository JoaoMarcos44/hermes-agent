# Hermes Root-Cause Contribution Skill

Use this workflow for NousResearch/hermes-agent bug fixes, PR reviews, and blocker resolution.

## Non-negotiable rules

1. Validate every material hypothesis before accepting or discarding it.
2. Start from the exact issue/PR head and current upstream main.
3. Trace the full causal path to the controlling owner/boundary. Prefer fixing the owner over adding downstream exceptions.
4. Search related issues, PRs, commits, docs, tests, and sibling code paths before implementation.
5. Never invent a defect. Separate proven blockers from optional improvements.
6. Existing competing work is not an automatic blocker. A contribution may proceed only when it is:
   - canonical / non-duplicative,
   - demonstrably superior, or
   - genuinely complementary with an independent owner/boundary.
7. Prefer RED -> GREEN evidence for superiority and regression coverage.
8. Inspect lifecycle, concurrency, retries/idempotency, persistence, cleanup, restart/shutdown, migration/compatibility, trust boundaries, and resource handling when materially related.
9. Keep upstream diffs focused. Personal Codex methodology files belong on a separate branch, not in product PRs.
10. Do not claim CI is green unless jobs actually ran and passed.

## Investigation sequence

### 1. Establish reality
- Read the issue body and comments.
- Reproduce from code/data when possible.
- Identify which observations are proven and which explanations are only hypotheses.
- Compare against current main; stale line numbers or prior implementations are not authority.

### 2. Build the relation graph
- Find linked/duplicate issues.
- Find open/closed/merged PRs for the same causal path.
- Inspect the exact heads of competing PRs.
- Record what each candidate does and does not cover.

### 3. Locate root cause
Write the causal chain as:
symptom -> first wrong state -> owner that created it -> missing/broken invariant.

Do not stop at the first function that throws if an earlier owner created the invalid state.

### 4. Review the proposed patch adversarially
For every changed semantic rule, search sibling inputs that also satisfy the new predicate.
Ask:
- Is the predicate too broad?
- Is it too narrow?
- Does it change behavior outside the documented domain?
- Does it preserve fail-closed safety where identity/ownership is uncertain?
- Does it create partial-write, symlink, race, or restart hazards?

### 5. Implement
- Change the smallest controlling owner.
- Reuse an existing invariant/helper when possible.
- Add one positive regression for the reported bug.
- Add at least one negative/sibling invariant when the fix broadens a predicate.
- Prefer new additive commits; do not rewrite contributor history unless necessary.

### 6. Three blocker sweeps
Run three independent passes after implementation:

**Sweep 1 — diff/scope**
- exact files/commits
- merge base / behind-ahead
- accidental scope changes
- duplicate search

**Sweep 2 — behavior/CI/reviews**
- unresolved review threads
- workflow execution and real job conclusions
- negative invariants / sibling paths
- compatibility and lifecycle risks

**Sweep 3 — moving context**
- re-fetch main
- re-fetch competing heads
- re-fetch reviews/comments
- re-run duplicate search
- ensure no newly-landed upstream change invalidates the patch

### 7. Respond to reviewers
For every proven blocker:
- state the root cause,
- reference the new commit,
- describe the regression test,
- explain why the fix is at the controlling owner,
- resolve only after the requested behavior is actually present.

For a disproven review claim, reply with evidence rather than changing correct code.
