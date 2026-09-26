---
name: canary-sweep
description: >-
  Use when scanning the repo for plaintext sensitive data leaks and guard rule
  violations. Records each new problem as a finding.
---

# Canary Sweep

1. Call `run_canary`; for each hit, call `record_finding` with
   source="canary" and the hit's path and snippet as the message.
2. Call `guard_check`; for each finding not already recorded, call
   `record_finding` with source="guard".
3. Report the total number of new findings written.
4. Do not modify any source file; your role is auditing only.
