---
name: fail-first-test
description: >-
  Use when writing a compliance test that fails on base and passes after a fix.
  Tests exercise behavior through the app's HTTP API or job entry points.
---

# Fail-First Test

1. Read the obligation YAML and its impact JSON to identify the behavior to test.
2. Write `compliance/tests/test_<id_lowercased_nodash>_<name>.py` that:
   - Imports only names that already exist at base_ref.
   - Uses TestClient or job entry points; no mocking of the behavior under test.
   - Contains at least one assert that would fail on unfixed base code.
3. Call `check_fails_on_base` with the test file name.
4. If the test is non-discriminating (passes on base), change only the test
   logic; do not modify app code until the test is discriminating.
5. Only after `check_fails_on_base` confirms discriminating=true, implement the fix.
