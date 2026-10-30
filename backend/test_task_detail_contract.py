"""Atlas M7: recorded task-detail responses for the frontend contract (test support only).

The synthetic contract scenario (task_projection_scenarios.contract) is read through the real
read-only task endpoints and the control-impact preview, so the Atlas Task page is built against
the backend's actual shapes. Nothing is executed and no state changes: only GET routes and the
read-only POST /tasks/{id}/control-impact preview are called.
"""
from contextlib import closing
import json
import os
import re
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app import main
from app.persistence import AgentStore
from execution_test_support import use_attested_test_doubles
from test_product_workspace import TestClient
import task_projection_scenarios as scenarios

FIXTURE = Path(__file__).resolve().parents[1] / 'frontend' / 'tests' / 'fixtures' / 'task-detail.contract.json'
TASKS = ['release', 'docs', 'tests', 'flaky', 'solo', 'orphaned-work', 'paused', 'blocked', 'limited']
READS = ['', '/assignments', '/orchestration', '/integrations', '/delegations', '/dependencies', '/dependents', '/control-operations']
# Defaults written by SQLite at insert time; replaced so the recording is stable between runs.
GENERATED = re.compile(r'^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(\.\d+)?Z$')
PLAN = {'protocol': 'control-center.planning.v1', 'decision_key': 'k2', 'base_revision': 1, 'decision': 'propose',
        'rationale': 'Docs and tests can proceed in parallel; the plan item waits for the docs.',
        'items': [{'proposal_id': 'prop-1', 'deliverable': 'Release notes', 'responsibility': 'Summarize the changes',
                   'depends_on': [], 'parallel': True}], 'references': []}


def stable(value, recorded=None):
    """Timestamps the seeder did not write (SQLite `now` defaults) become a fixed marker."""
    if isinstance(value, dict):
        return {k: stable(v, recorded) for k, v in value.items()}
    if isinstance(value, list):
        return [stable(v, recorded) for v in value]
    if isinstance(value, str) and GENERATED.match(value) and not value.startswith('2026-01-01T00:'):
        return 'generated-timestamp'
    if isinstance(value, str):
        # The scenario's leak marker becomes neutral text: these endpoints show descriptions,
        # instructions and summaries by design, and the fixture must not look like a secret.
        return value.replace(scenarios.SECRET, 'Synthetic text')
    return value


class TaskDetailContractTests(unittest.TestCase):
    def setUp(self):
        use_attested_test_doubles(self)
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.path = Path(folder.name) / 'fixture.sqlite3'
        self.store = AgentStore(self.path)
        patcher = patch.object(main.manager, '_task_store', return_value=self.store)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.client = TestClient(main.app)

    def record(self):
        scenarios.contract(self.path)
        # The scenario stores placeholder plan content; give the lead's revisions a valid plan.
        with closing(sqlite3.connect(self.path)) as db:
            for revision in (1, 2):
                plan = {**PLAN, 'decision_key': f'k{revision}', 'base_revision': revision - 1}
                db.execute('UPDATE planning_revisions SET content=? WHERE task_id=? AND revision=?',
                           (json.dumps(plan, sort_keys=True), 'release', revision))
            promotion = {'protocol': 'control-center.promotion.v1', 'task_id': 'release', 'revision': 2, 'decision_key': 'k2',
                         'items': [{'proposal_id': 'prop-1'}], 'continuation': None}
            db.execute('UPDATE promotion_decisions SET content=? WHERE task_id=?', (json.dumps(promotion, sort_keys=True), 'release'))
            db.execute('UPDATE promotion_items SET proposal=? WHERE task_id=?', (json.dumps(PLAN['items'][0], sort_keys=True), 'release'))
            db.commit()
        result = {}
        for task_id in TASKS:
            entry = {}
            for suffix in READS:
                response = self.client.get(f'/tasks/{task_id}{suffix}')
                entry[suffix or 'task'] = {'status': response.status_code, 'body': response.json()}
            for action in ('pause', 'cancel'):
                response = self.client.post(f'/tasks/{task_id}/control-impact', json={'action': action})
                entry[f'impact:{action}'] = {'status': response.status_code, 'body': response.json()}
            result[task_id] = entry
        return stable(result)

    def test_frontend_task_detail_fixture_matches_the_real_endpoints(self):
        recorded = self.record()
        # The read-only preview changed nothing: a second recording is identical.
        self.assertEqual(stable(self.record_again()), recorded)
        if os.environ.get('ATLAS_WRITE_FIXTURES') == '1':
            FIXTURE.write_text(json.dumps(recorded, indent=1, ensure_ascii=False) + '\n', encoding='utf-8')
        self.assertEqual(json.loads(FIXTURE.read_text(encoding='utf-8')), recorded,
                         'Regenerate with ATLAS_WRITE_FIXTURES=1 after an intentional change to the task endpoints')

    def record_again(self):
        result = {}
        for task_id in TASKS:
            entry = {}
            for suffix in READS:
                response = self.client.get(f'/tasks/{task_id}{suffix}')
                entry[suffix or 'task'] = {'status': response.status_code, 'body': response.json()}
            for action in ('pause', 'cancel'):
                response = self.client.post(f'/tasks/{task_id}/control-impact', json={'action': action})
                entry[f'impact:{action}'] = {'status': response.status_code, 'body': response.json()}
            result[task_id] = entry
        return result


if __name__ == '__main__':
    unittest.main()
