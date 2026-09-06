Create a professional English infographic about preventing approval-gate self-mutation in Hermes Agent.

Image specifications:
- Type: infographic
- Layout: bento-grid with a structural security flow
- Style: technical-schematic, dark navy blueprint background, clean vector lines, restrained red/blue/green accents
- Aspect ratio: landscape 16:9
- Language: English

Visual hierarchy:
1. Large title: "Prevent Approval-Gate Self-Mutation"
2. Four balanced modules:
   - "Reported path": Agent → `hermes config set approvals.single_query_mode approve` → red blocked marker
   - "Root cause": generic writer + unclassified command + persisted policy
   - "Operator-only boundary": Detect → Classify key → Scoped operator write → Atomic persistence
   - "Assurance": "One-shot only", "O(1) guard", "Regression tests", "Graphify traced"
3. Make these exact labels readable and spelled correctly:
   "approvals.single_query_mode", "approvals.*", "security.*", "command_allowlist", "Operator-only policy boundary", "One-shot only", "O(1) guard".

Content accuracy:
- The reported path is rejected; do not depict it as successful after the fix.
- The operator-mediated approval-mode path remains available.
- Security-policy approvals cannot be saved as session or permanent allowlist entries.
- Do not invent incident counts, credentials, tokens, or product logos.

Design:
- Use a red arrow for the agent path, a blue shield for the trusted write boundary, and green check marks for assurance.
- Use ample whitespace, high contrast, crisp typography, and no decorative text outside the specified labels.
- Keep the diagram understandable without a legend.
