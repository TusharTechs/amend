---
name: trace-impact
description: >-
  Use when tracing which code files are affected by a compliance obligation.
  Writes compliance/impact/<id>.json using the code graph.
---

# Trace Impact

1. For each obligation ID, call `query_graph` with query=`trace` and the
   obligation's data class (e.g. `government_id`).
2. Also call `query_graph` with query=`sinks_of` for the same data class.
3. Write `compliance/impact/<id>.json` as a JSON list of:
   `{obligation_id, file, line, kind, source ("graph" or "inferred"), rationale}`.
4. For routes missing the MFA dependency, call `query_graph` with
   query=`routes_missing_dependency` and arg=`require_mfa`.
5. Spawn one explore subagent per obligation for parallel tracing.
