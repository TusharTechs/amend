# Amend — agent context

## Purpose
Turn a regulatory change into a verified pull request, running inside IBM Bob.

## Folder layout
- `amend/` — engine package (parsing, diffing, obligation tracing, PR generation)
- `demo/lendwise/` — sample fintech app used as the change target in demos
- `regulations/` — vendored regulation XML snapshots (public domain, US Gov)
- `docs/` — static MCP console (HTML/JS, no build step)
- `benchmark/` — regression harness comparing engine outputs across versions
- `bob_sessions/` — screenshots from IBM Bob task runs (not committed to CI)

## Test command
`.venv/bin/pytest -q`

## Rules
- Python 3.11+ only; prefer the standard library; reach for a dependency only when it saves substantial work.
- Allowed dependencies: `pyyaml`, `sqlglot`, `networkx`, `mcp`, `fastapi`, `sqlalchemy`, `httpx`, `pytest`.
- Keep modules small and single-purpose.
- Never weaken, skip, or delete an existing test to make something pass.
- Use synthetic data only; never use real PII or real customer records.
- Never claim or imply that a system is "compliant" with any regulation.
- Commit messages must follow Conventional Commits; do not add Co-authored-by or AI-attribution trailers.
