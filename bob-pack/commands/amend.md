# /amend

Run the full Amend compliance workflow through amend-lead.

**Usage:** `/amend [obligation-id ...]`

Passing one or more obligation IDs limits the run to those obligations only.

## Workflow

1. **Redline** — call `get_redline` to fetch all substantive changes.
2. **Obligations** — switch to amend-analyst; extract and write obligation YAMLs;
   call `validate_obligations` until zero problems; stop for human review.
3. **Human approval** — for each obligation, a human calls `approve_obligation`
   (approver must be in the approvers list); do not proceed until approved.
4. **Impact** — switch to amend-mapper; spawn one explore subagent per obligation
   in parallel; write `compliance/impact/<id>.json` for each.
5. **Fixes** — switch to amend-fixer; for each obligation write a fail-first test,
   confirm discriminating with `check_fails_on_base`, then implement the fix.
6. **Audit** — switch to amend-auditor; run `guard_check` and `run_canary`;
   call `record_finding` for each new problem.
7. **Evidence** — call `build_evidence`; summarize status counts.
8. **/review** — request a final review of all changes.
9. **/create-pr** — create the pull request.
