# Lendwise — Amend Compliance Workspace

This workspace is the Amend compliance target for 16 CFR Part 314 (Safeguards Rule).

- Regulation: `compliance/amend.yaml`
- Obligations: `compliance/obligations/*.yaml`
- Impact maps: `compliance/impact/*.json`
- Compliance tests: `compliance/tests/test_<id>_*.py`
- Findings: `compliance/findings.json`

Run `/amend` to start the full workflow. Use amend-analyst, amend-mapper,
amend-fixer, and amend-auditor modes for individual phases.
