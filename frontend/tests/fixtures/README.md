# Frontend test fixtures

Synthetic API responses for deterministic frontend tests (Node unit tests and the browser tests in
`../../e2e/`). They contain no real prompts, output, paths, identifiers or credentials:

- `workspace-sample.json`: a `GET /workspace` page with three Agents (a waiting autonomous Lead, a
  finished Helper and a Helper with a question for the user) and the workspace totals.
- `workspace-tasks.contract.json`: real `GET /workspace/tasks` responses (all Tasks in one page,
  three-Task pages and the `release` family in two-Task pages), recorded by
  `backend/test_task_projection.py` from a synthetic isolated database
  (`backend/task_projection_scenarios.py`). That backend test fails when the endpoint's answer
  drifts from this file; regenerate it deliberately with `ATLAS_WRITE_FIXTURES=1`.

The field names follow the backend's read model (`backend/app/presentation.py`, `_nodes` and
`workspace`). `tests/fixtures.test.mjs` reads that backend source and fails if a fixture's fields
drift from it, so a projection change must be reflected here deliberately. `workspace-sample.json`
is hand-written; the task contract is recorded from the real endpoint.
