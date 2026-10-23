"""Explicit, controlled recovery evidence for tests (Stage 2D.1b P2).

Production code has no bypass: an execution settles only from kernel or reboot
evidence (and, in P3, an explicit attestation). Tests state their evidence:

- `rebooted()` presents what a verified Windows restart produces (a higher
  boot counter and a boot time after every controller recorded so far).
- `attested_test_doubles()` records the end of a *test double* (an object that
  is not a real `subprocess.Popen`) as `attested_ended` with an attestation of
  kind `test_fixture` (never `user_responsibility`), an explicit test
  attestation. The production schema refuses that kind: the fixture widens the
  trigger of the isolated test database only. No kernel evidence is
  fabricated, and a real process, even one launched without supervision,
  always follows the production rules.
"""
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from app import agent_manager as manager, execution_attestation, executions, process_supervision as ps

REAL_POPEN = manager._RealPopen
REAL_RECORD_END = manager._record_end  # captured before any test patches it
ATTESTATION = 'test fixture: controlled test double (not a real process)'


def _later_boot(identity):
    # After every controller started so far (their start times have millisecond precision).
    now = (datetime.now(timezone.utc) + timedelta(seconds=1)).isoformat(timespec='milliseconds').replace('+00:00', 'Z')
    return {'boot_id': (identity.get('boot_id') or 0) + 1000, 'os_boot_time': now}


@contextmanager
def rebooted():
    """Controlled evidence: Windows restarted after every controller recorded so far."""
    real = ps.controller_identity
    later = _later_boot(real())

    def identity():
        return {**real(), **later}
    previous = manager._controller
    with patch.object(ps, 'controller_identity', identity):
        if previous is not None:
            manager._controller = {**previous, **later}  # What this controller would observe after a reboot.
        try:
            yield
        finally:
            if previous is not None and manager._controller is not None \
                    and manager._controller.get('instance_id') == previous.get('instance_id'):
                manager._controller = previous


def is_test_double(process):
    return process is not None and not isinstance(process, REAL_POPEN)


FIXTURE_KIND = 'test_fixture'


def allow_fixture_attestations(db):
    """Test databases only: let the attested_ended trigger also accept FIXTURE_KIND."""
    for name, _ in execution_attestation.TRIGGERS:
        row = db.execute("SELECT sql FROM sqlite_master WHERE type='trigger' AND name=?", (name,)).fetchone()
        if row is None or FIXTURE_KIND in row[0]:
            continue
        guard = "WHEN NEW.state = 'attested_ended' AND NOT ("
        assert row[0].count(guard) == 1, 'the production trigger changed; update this fixture'
        db.execute(f'DROP TRIGGER {name}')
        db.execute(row[0].replace(guard, "WHEN NEW.state = 'attested_ended' AND "
                                         f"json_extract(NEW.attestation, '$.kind') IS NOT '{FIXTURE_KIND}' AND NOT ("))


def attested_test_doubles():
    """A patcher: the observed end of a test double is attested by the test, never by the kernel."""
    real = manager._record_end

    def record_end(process, via):
        if not is_test_double(process):
            return real(process, via)  # Real processes: production rules, always.
        execution_id = getattr(process, '_control_center_execution_id', None)
        if isinstance(execution_id, str) and manager._store is not None:
            with manager._store._connection() as db:
                allow_fixture_attestations(db)
            manager._record_execution(execution_id, 'attested_ended', executions.LIVE,
                                      evidence={'end': {'via': via, 'attested_by': ATTESTATION}},
                                      attestation={'kind': FIXTURE_KIND, 'by': ATTESTATION,
                                                   'technically_verified': False})
    return patch.object(manager, '_record_end', record_end)


def use_attested_test_doubles(test):
    """Apply `attested_test_doubles()` for the rest of a test (call from setUp)."""
    test.enterContext(attested_test_doubles())  # Not a started patch: patch.stopall() leaves it alone.


def genuine_unsupervised():
    """Opt out inside a test: test doubles follow the production rules (genuine regressions)."""
    return patch.object(manager, '_record_end', REAL_RECORD_END)
