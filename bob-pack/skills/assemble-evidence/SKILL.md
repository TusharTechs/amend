---
name: assemble-evidence
description: >-
  Use when building the final evidence pack after fixes and tests are complete.
  Summarizes obligation statuses from the certificate.
---

# Assemble Evidence

1. Call `build_evidence` (optionally pass a run_id for reproducibility).
2. Read the returned status_counts and guard_findings count.
3. Summarize in at most 8 lines:
   - Run ID and generation time.
   - Status counts: Verified / Partially verified / Unverified / Human review.
   - Guard finding count and canary hit count.
   - Path to the generated certificate.html.
4. Do not claim the system is compliant; report only what was verified.
