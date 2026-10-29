"""Deterministic synthetic Task scenarios for the task projection (test support only).

Rows are written directly so that states which are hard to reach through a live run (removed
owners, malformed hierarchies, recorded results) can be checked exactly. Nothing is executed.
"""
import sqlite3

SECRET = 'SECRET-DO-NOT-EXPOSE'


def ts(n):
    return f'2026-01-01T00:{n // 60:02d}:{n % 60:02d}.000Z'


class Seeder:
    def __init__(self, path):
        # A raw connection leaves foreign keys off so malformed hierarchies can be stored on purpose.
        self.db = sqlite3.connect(path)
        self.clock = 0

    def close(self):
        self.db.commit()
        self.db.close()

    def tick(self):
        self.clock += 1
        return ts(self.clock)

    def project(self, project_id='p1'):
        self.db.execute('INSERT INTO projects(project_id,name,root_path,root_path_key) VALUES (?,?,?,?)',
                        (project_id, 'Project ' + project_id, f'C:/{SECRET}/{project_id}', f'c:/{SECRET.lower()}/{project_id}'))

    def task(self, task_id, parent=None, status='pending', title=None, project='p1', control='active', created=None):
        when = created or self.tick()
        self.db.execute('''INSERT INTO tasks(task_id,title,description,status,created_at,updated_at,parent_task_id,project_id,control_intent)
            VALUES (?,?,?,?,?,?,?,?,?)''', (task_id, title or 'Task ' + task_id, SECRET + ' description', status,
                                            when, when, parent, project, control))

    def agent(self, agent_id, status='running', *, agent_type='claude', question=None, condition=None,
              removed=False, name=None, autonomous=False):
        flag = 1 if autonomous else 0
        self.db.execute('''INSERT INTO agents(agent_id,parent_id,task,agent_type,sandbox,status,session_id,waiting_question,
            similar_decisions_enabled,similar_examples,always_decide_enabled,always_decide_configured,display_name,display_color,
            model,effort,provider_condition,removed_at,planning_enabled,delegation_protocol_enabled,child_materialization_enabled,
            hybrid_delegation_enabled,recursive_delegation_enabled)
            VALUES (?,NULL,?,?,?,?,?,?,0,'[]',0,0,?,?,?,?,?,?,?,?,?,?,?)''',
                        (agent_id, SECRET + ' prompt', agent_type, 'workspace-write', status, 'session-' + SECRET, question,
                         name or 'Agent ' + agent_id, 'blue', 'model-x', 'high', condition,
                         self.tick() if removed else None, flag, flag, flag, flag, flag))

    def assign(self, assignment_id, task_id, agent_id, ended=None):
        self.db.execute('INSERT INTO task_assignments(assignment_id,task_id,agent_id,assigned_at,ended_at,ended_reason) VALUES (?,?,?,?,?,?)',
                        (assignment_id, task_id, agent_id, self.tick(), self.tick() if ended else None, ended))

    def blocker(self, task_id, code, blocker_type='coordination'):
        self.db.execute('INSERT INTO task_blockers(id,task_id,blocker_type,reason_code,active,created_at) VALUES (?,?,?,?,1,?)',
                        (f'b-{task_id}-{code}', task_id, blocker_type, code, self.tick()))

    def delegation(self, delegation_id, parent, by_agent, by_assignment, status, child=None):
        closed = self.tick() if status in ('acknowledged', 'canceled') else None
        self.db.execute('''INSERT INTO delegations(delegation_id,project_id,parent_task_id,requested_by_agent_id,requested_by_assignment_id,
            request_key,instruction,status,child_task_id,created_at,updated_at,materialized_at,closed_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)''', (delegation_id, 'p1', parent, by_agent, by_assignment, delegation_id,
                                                    SECRET + ' instruction', status, child, self.tick(), self.tick(),
                                                    self.tick() if child else None, closed))

    def integration(self, integration_id, source, kind, destination, status):
        self.db.execute('''INSERT INTO integrations(integration_id,source_task_id,destination_kind,destination_id,destination_key,
            base_snapshot,source_snapshot,destination_before_snapshot,status,changed_paths,conflict_paths,failure_reason,created_at,applied_at)
            VALUES (?,?,?,?,?,'b','s','d',?,?,?,?,?,?)''', (integration_id, source, kind, destination, kind + ':' + destination, status,
                                                            f'["{SECRET}/changed.py"]', f'["{SECRET}/conflict.py"]', SECRET,
                                                            self.tick(), self.tick() if status == 'applied' else None))

    def result(self, result_id, delegation_id, child, assignment_id, agent_id, outcome, phase, integration_id=None):
        self.db.execute('''INSERT INTO delegation_results(result_id,delegation_id,child_task_id,assignment_id,agent_id,outcome,
            output_after,output_through,phase,integration_id,summary,last_error,created_at,updated_at)
            VALUES (?,?,?,?,?,?,0,10,?,?,?,?,?,?)''', (result_id, delegation_id, child, assignment_id, agent_id, outcome, phase,
                                                       integration_id, SECRET + ' summary', SECRET, self.tick(), self.tick()))

    def revision(self, task_id, revision, agent_id, assignment_id):
        self.db.execute('''INSERT INTO planning_revisions(task_id,revision,project_id,agent_id,assignment_id,generation,session_id,
            message_id,decision_key,content) VALUES (?,?,?,?,?,?,?,?,?,?)''',
                        (task_id, revision, 'p1', agent_id, assignment_id, 'g', 's', f'm{revision}', f'k{revision}', SECRET))

    def proposal(self, task_id, proposal_id, revision, delegation_id, disposition, agent_id, assignment_id):
        key = f'k{revision}'
        self.db.execute('''INSERT OR IGNORE INTO promotion_decisions(task_id,decision_key,revision,agent_id,assignment_id,generation,
            session_id,message_id,content,receipt) VALUES (?,?,?,?,?,?,?,?,?,?)''',
                        (task_id, key, revision, agent_id, assignment_id, 'g', 's', 'pm' + key, SECRET, '{}'))
        self.db.execute('''INSERT INTO promotion_items(task_id,proposal_id,decision_key,revision,proposal,coordination,delegation_id,
            disposition,reason) VALUES (?,?,?,?,?,?,?,?,?)''',
                        (task_id, proposal_id, key, revision, SECRET, '{}', delegation_id, disposition, SECRET))

    def round(self, round_id, task_id, assignment_id, agent_id, accepted=True):
        self.db.execute('''INSERT INTO delegation_rounds(round_id,task_id,assignment_id,agent_id,generation,message_id,receipt,
            accepted_at,not_before) VALUES (?,?,?,?,?,?,?,?,0)''',
                        (round_id, task_id, assignment_id, agent_id, 'g', round_id, '{}', 1.0 if accepted else None))

    def continuation(self, task_id, assignment_id, phase, mode=None):
        self.db.execute('''INSERT INTO delegation_continuations(task_id,batch_id,assignment_id,origin_generation,current_generation,
            phase,mode,last_error) VALUES (?,?,?,?,?,?,?,?)''', (task_id, 'batch-' + task_id, assignment_id, 'g', 'g', phase, mode, SECRET))


def contract(path):
    """One family of every state Atlas must tell apart, plus standalone and malformed Tasks."""
    s = Seeder(path)
    s.project()
    # A lead Task in its second orchestration round, reviewing child results.
    s.task('release', status='in_progress', title='Ship the release')
    s.agent('lead', 'running', name='Lead', autonomous=True)
    s.assign('as-lead', 'release', 'lead')
    s.revision('release', 1, 'lead', 'as-lead')
    s.revision('release', 2, 'lead', 'as-lead')
    s.round('r1', 'release', 'as-lead', 'lead')
    s.round('r2', 'release', 'as-lead', 'lead')
    s.continuation('release', 'as-lead', 'reviewing', 'reviewing')
    # Child 1: first owner removed, replacement finished; result delivered and integrated into the lead.
    s.task('docs', 'release', status='completed', title='Write the docs')
    s.agent('docs-old', 'stopped', removed=True, name='First writer')
    s.agent('docs-new', 'finished', name='Second writer')
    s.assign('as-docs-1', 'docs', 'docs-old', ended='removed')
    s.assign('as-docs-2', 'docs', 'docs-new', ended='completed')
    s.delegation('d-docs', 'release', 'lead', 'as-lead', 'result_ready', child='docs')
    s.integration('i-docs', 'docs', 'task', 'release', 'applied')
    s.result('res-docs', 'd-docs', 'docs', 'as-docs-2', 'docs-new', 'completed', 'delivered', 'i-docs')
    # Child 2: its Agent asks the user a question.
    s.task('tests', 'release', status='waiting', title='Fix the tests')
    s.agent('tester', 'waiting', question=SECRET + ' question?', name='Tester')
    s.assign('as-tests', 'tests', 'tester')
    s.delegation('d-tests', 'release', 'lead', 'as-lead', 'materialized', child='tests')
    # Grandchild without any Agent yet.
    s.task('flaky', 'tests', status='pending', title='Investigate the flaky test')
    # Planned work that is not a Task yet: one held by planning, one direct request.
    s.delegation('d-plan', 'release', 'lead', 'as-lead', 'requested')
    s.proposal('release', 'prop-1', 2, 'd-plan', 'blocked', 'lead', 'as-lead')
    s.delegation('d-direct', 'release', 'lead', 'as-lead', 'requested')
    # Standalone Task applied to the project; its Agent finished.
    s.task('solo', status='completed', title='Rename the settings page')
    s.agent('solo-agent', 'finished', agent_type='codex', name='Solo')
    s.assign('as-solo', 'solo', 'solo-agent', ended='completed')
    s.integration('i-solo', 'solo', 'project', 'p1', 'applied')
    # Owner removed and nobody owns the Task.
    s.task('orphaned-work', status='in_progress', title='Migrate the database')
    s.agent('gone', 'stopped', removed=True, name='Gone')
    s.assign('as-gone', 'orphaned-work', 'gone', ended='removed')
    # Provider limit wait.
    s.task('limited', status='waiting', title='Summarize the logs')
    s.agent('limited-agent', 'waiting', agent_type='claude', name='Limited',
            condition='{"kind": "usage_limited", "blocking": [{"label": "5-hour limit", "reset_at": "2026-01-01T05:00:00Z"}]}')
    s.assign('as-limited', 'limited', 'limited-agent')
    # Paused by the user.
    s.task('paused', status='paused', control='paused', title='Refactor the parser')
    s.agent('paused-agent', 'stopped', name='Paused')
    s.assign('as-paused', 'paused', 'paused-agent', ended='paused')
    # Coordination-blocked Task.
    s.task('blocked', status='blocked', title='Deploy the preview')
    s.agent('blocked-agent', 'waiting', name='Blocked')
    s.assign('as-blocked', 'blocked', 'blocked-agent')
    s.blocker('blocked', 'resource_conflict')
    # Malformed: missing parent and a two-Task cycle.
    s.task('stray', 'missing-parent', status='pending', title='Stray Task')
    s.task('loop-a', 'loop-b', status='pending', title='Loop A')
    s.task('loop-b', 'loop-a', status='pending', title='Loop B')
    s.close()
