# Frontend test fixtures

Synthetic API responses for deterministic frontend tests (Node unit tests and the browser tests in
`../../e2e/`). They contain no real prompts, output, paths, identifiers or credentials:

- `workspace-sample.json`: a `GET /workspace` page with three Agents (a waiting autonomous Lead, a
  finished Helper and a Helper with a question for the user) and the workspace totals.

The field names follow the backend's read model (`backend/app/presentation.py`, `_nodes` and
`workspace`). `tests/fixtures.test.mjs` reads that backend source and fails if a fixture's fields
drift from it, so a projection change must be reflected here deliberately. The fixtures are
hand-written, not recorded from a running backend; recorded fixtures for the task-centred
projection belong to a later milestone.
