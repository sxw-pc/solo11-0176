"""Chunked salinity capture: staging, signed manifests, atomic publish, recovery.

Covers the two device-authorised delivery routes (A closes the intake, B
corrects it to open), out-of-order and retransmitted pieces, missing pieces,
sealed piece conflicts, signature failures, unsigned revisions, concurrent
collectors, the four crash seams and audit after recovery.
"""
import hashlib
import json
import sqlite3
import subprocess
import sys
import threading
from pathlib import Path

import pytest

from tidalrestore.salinity import (import_logger, summary, pending, recover, history,
                                   group_state, try_commit_group, InjectedCrash, FAULT_POINTS)
from tidalrestore.audit import audit
from tidalrestore.water_access import access_report, access_for

FIX = Path(__file__).resolve().parent.parent / 'fixtures'
A = FIX / 'salinity-chunked-a'
B_LEGACY = FIX / 'salinity-chunked-b-legacy'
B_CORRECTED = FIX / 'salinity-chunked-b-corrected'
THRESHOLD = 100


def db_at(tmp_path):
    return tmp_path / 'salinity.sqlite'


def import_doc(db, doc, name, **kwargs):
    p = Path(db).parent / name
    p.write_bytes(json.dumps(doc).encode('utf8'))
    return import_logger(db, p, **kwargs)


def doc(path):
    return json.loads(Path(path).read_bytes())


def statuses(db):
    with sqlite3.connect(db) as con:
        con.row_factory = sqlite3.Row
        return {'sources': [dict(r) for r in con.execute(
                    'SELECT source_key,source_id,kind,status,sha,group_key,manifest_key,revision '
                    'FROM salinity_source ORDER BY source_key')],
                'samples': [dict(r) for r in con.execute(
                    'SELECT source_key,station,at,conductivity,tide FROM salinity_sample '
                    'ORDER BY source_key,seq')],
                'manifests': [dict(r) for r in con.execute(
                    'SELECT manifest_key,status,reason FROM salinity_manifest ORDER BY manifest_key')],
                'conflicts': [dict(r) for r in con.execute(
                    'SELECT scope,object_key,piece_index,reason FROM salinity_conflict ORDER BY id')]}


def deliver_a(db, order=(1, 2, 3), collector='collector-a'):
    results = [import_logger(db, A / f'piece-{i}.json', collector=collector) for i in order]
    results.append(import_logger(db, A / 'manifest.json', collector=collector))
    return results


# ---------------------------------------------------------------------------
# Route A: incomplete material never looks open; full group closes the intake
# ---------------------------------------------------------------------------

def test_route_a_any_prefix_is_closed_and_full_group_publishes_peak_120(tmp_path):
    db = db_at(tmp_path)
    manifest = import_logger(db, A / 'manifest.json', collector='collector-a')
    assert manifest['status'] == 'staging'
    assert summary(db) == []
    assert access_for(db, 'S1')['access'] == 'closed'
    assert access_for(db, 'S1')['basis'] == 'pending'

    for index in (1, 3):
        result = import_logger(db, A / f'piece-{index}.json', collector='collector-a')
        assert result['status'] == 'staging'
        assert summary(db) == []                      # never a partial S1 peak
        assert access_for(db, 'S1')['access'] == 'closed'
        assert access_for(db, 'S1')['basis_peak'] is None

    final = import_logger(db, A / 'piece-2.json', collector='collector-b')
    assert final['status'] == 'committed' and final['samples'] == 2
    assert summary(db, THRESHOLD) == [{'station': 'S1', 'peak': 120, 'count': 2, 'status': 'alert'}]
    assert access_for(db, 'S1')['access'] == 'closed'
    assert access_for(db, 'S1')['basis'] == 'alert'
    assert audit(db)['status'] == 'matched'


def test_pieces_may_arrive_out_of_order_and_before_the_manifest(tmp_path):
    db = db_at(tmp_path)
    for index in (3, 1, 2):
        result = import_logger(db, A / f'piece-{index}.json')
        assert result['status'] == 'staging'
    # Orphans exist but no manifest: the gate must still be closed.
    assert access_for(db, 'S1')['access'] == 'closed'
    assert summary(db) == []
    result = import_logger(db, A / 'manifest.json')
    assert result['status'] == 'committed' and result['samples'] == 2
    assert summary(db)[0]['peak'] == 120
    assert audit(db)['status'] == 'matched'


def test_retransmission_is_idempotent_for_manifest_and_pieces(tmp_path):
    db = db_at(tmp_path)
    delivered = deliver_a(db, order=(1, 2, 3))
    assert delivered[-1]['status'] == 'committed'
    before = statuses(db)
    for index in (1, 2, 3):
        repeated = import_logger(db, A / f'piece-{index}.json')
        assert repeated['status'] == 'committed' and repeated['replayed'] is True
        assert repeated['samples'] == 2
    assert import_logger(db, A / 'manifest.json')['replayed'] is True
    assert statuses(db) == before
    assert len([s for s in statuses(db)['samples']]) == 2


def test_retransmission_of_a_staging_piece_does_not_duplicate_it(tmp_path):
    db = db_at(tmp_path)
    import_logger(db, A / 'manifest.json')
    first = import_logger(db, A / 'piece-1.json')
    again = import_logger(db, A / 'piece-1.json')
    assert first['pieces_received'] == 1 and again['pieces_received'] == 1
    assert again['replayed'] is True


# ---------------------------------------------------------------------------
# Missing pieces / missing totals / bad calibration stay group-pending
# ---------------------------------------------------------------------------

def test_missing_piece_keeps_staging_and_does_not_leak_local_peak(tmp_path):
    db = db_at(tmp_path)
    import_logger(db, A / 'manifest.json')
    import_logger(db, A / 'piece-1.json')
    import_logger(db, A / 'piece-3.json')            # the 120 is on disk...
    state = group_state(db, 'logger-A@1')
    assert state['status'] == 'staging'
    assert state['blocking'].startswith('missing_piece')
    assert summary(db) == []                          # ...but it is not public
    assert pending(db)['staging'][0]['blocking'].startswith('missing_piece')
    assert access_for(db, 'S1')['access'] == 'closed'


def test_manifest_without_total_digest_never_commits(tmp_path):
    db = db_at(tmp_path)
    d = FIX / 'salinity-bad-no-total'
    for name in ('piece-1.json', 'piece-2.json', 'piece-3.json', 'manifest.json'):
        result = import_logger(db, d / name)
        assert result['status'] == 'staging'
    assert summary(db) == []
    assert group_state(db, 'logger-NT@1')['blocking'] == 'missing_total_digest'
    assert audit(db)['status'] == 'matched'


def test_calibration_window_must_cover_samples(tmp_path):
    db = db_at(tmp_path)
    d = FIX / 'salinity-bad-calibration'
    for name in ('piece-1.json', 'piece-3.json', 'piece-2.json', 'manifest.json'):
        result = import_logger(db, d / name)
        assert result['status'] == 'staging'
    state = group_state(db, 'logger-CAL@1')
    assert state['blocking'].startswith('calibration_window_gap')
    assert summary(db) == []
    assert access_for(db, 'S1')['access'] == 'closed'


# ---------------------------------------------------------------------------
# Sealed conflicts and signature failures
# ---------------------------------------------------------------------------

def test_same_piece_index_different_bytes_is_sealed_not_committed(tmp_path):
    db = db_at(tmp_path)
    import_logger(db, A / 'manifest.json')
    import_logger(db, A / 'piece-1.json')
    conflicting = FIX / 'salinity-conflict-piece' / 'piece-1-different.json'
    with pytest.raises(ValueError, match='piece_index_conflict'):
        import_logger(db, conflicting)
    sealed = statuses(db)['conflicts']
    assert sealed and sealed[0]['reason'] == 'piece_index_conflict'
    assert sealed[0]['piece_index'] == 1
    # original bytes still stand, the conflict is not adopted
    with sqlite3.connect(db) as con:
        rows = con.execute("SELECT sha FROM salinity_piece WHERE manifest_key='logger-A@1' AND piece_index=1"
                           ).fetchall()
    assert len(rows) == 1
    assert summary(db) == []          # conflict keeps the group unpublishable
    assert access_for(db, 'S1')['access'] == 'closed'
    assert audit(db)['status'] == 'matched'


def test_unsigned_or_tampered_manifest_never_commits(tmp_path):
    db = db_at(tmp_path)
    signed_bad = FIX / 'salinity-bad-signature' / 'manifest.json'
    import_logger(db, signed_bad)
    for index in (1, 2, 3):
        import_logger(db, A / f'piece-{index}.json')  # pieces are valid bytes
    state = group_state(db, 'logger-SIG@1')
    assert state['status'] == 'staging'
    assert state['blocking'] == 'manifest_signature_invalid'
    assert summary(db) == []


def test_wrong_declared_piece_digest_is_rejected_at_receipt(tmp_path):
    db = db_at(tmp_path)
    d = FIX / 'salinity-bad-piece-digest'
    import_logger(db, d / 'manifest.json')
    with pytest.raises(ValueError, match='piece_digest_mismatch'):
        import_logger(db, d / 'piece-3.json')         # bytes do not match declaration
    assert statuses(db)['conflicts'][0]['reason'] == 'piece_digest_mismatch'
    assert summary(db) == []


def test_piece_with_invalid_device_signature_blocks_the_group(tmp_path):
    db = db_at(tmp_path)
    d = FIX / 'salinity-bad-piece-signature'
    for name in ('manifest.json', 'piece-1.json', 'piece-2.json', 'piece-3.json'):
        result = import_logger(db, d / name)
        assert result['status'] == 'staging'
    state = group_state(db, 'logger-PS@1')
    assert state['blocking'] == 'piece_signature_invalid:3'
    assert summary(db) == []
    assert access_for(db, 'S1')['access'] == 'closed'
    assert audit(db)['status'] == 'matched'


# ---------------------------------------------------------------------------
# Route B: cross-device piece cannot leak; signed revision re-opens at 80
# ---------------------------------------------------------------------------

def test_route_b_cross_device_120_is_sealed_and_signed_revision_opens_at_80(tmp_path):
    db = db_at(tmp_path)
    import_logger(db, B_LEGACY / 'manifest.json')
    import_logger(db, B_LEGACY / 'piece-1.json')
    import_logger(db, B_LEGACY / 'piece-2.json')
    with pytest.raises(ValueError, match='piece_device_mismatch'):
        import_logger(db, B_LEGACY / 'piece-3.json')  # belongs to device-7, logger-B is device-9
    assert statuses(db)['conflicts'][0]['reason'] == 'piece_device_mismatch'
    assert summary(db) == []

    corrected = import_logger(db, B_CORRECTED / 'manifest.json')
    assert corrected['status'] == 'staging'
    import_logger(db, B_CORRECTED / 'piece-1.json')
    final = import_logger(db, B_CORRECTED / 'piece-2.json')
    assert final['status'] == 'committed' and final['samples'] == 1
    assert summary(db, THRESHOLD) == [{'station': 'S1', 'peak': 80, 'count': 1, 'status': 'normal'}]
    assert access_for(db, 'S1')['access'] == 'open'
    assert access_for(db, 'S1')['basis_peak'] == 80
    # The other device's 120 must never appear in S1.
    with sqlite3.connect(db) as con:
        values = [r[0] for r in con.execute(
            "SELECT conductivity FROM salinity_sample WHERE station='S1'")]
    assert 120 not in values
    assert audit(db)['status'] == 'matched'


def test_revision_without_signed_revocation_cannot_replace_the_old_manifest(tmp_path):
    db = db_at(tmp_path)
    d = FIX / 'salinity-bad-unsigned-revision'
    assert import_logger(db, d / 'rev1-manifest.json')['status'] == 'staging'
    import_logger(db, d / 'rev1-piece-1.json')
    assert summary(db)[0]['peak'] == 80          # rev 1 is genuine and commits
    second = import_logger(db, d / 'rev2-manifest.json')
    assert second['status'] == 'staging'
    assert second['blocking'] == 'revocation_signature_invalid'
    import_logger(db, d / 'rev2-piece-1.json')   # full new group still blocked
    assert group_state(db, 'logger-U@2')['status'] == 'staging'
    # The old, legitimately signed publication is not withdrawn by an
    # unsigned revision.
    assert summary(db) == [{'station': 'S1', 'peak': 80, 'count': 1, 'status': 'normal'}]
    assert history(db)[-1]['status'] == 'active'
    assert audit(db)['status'] == 'matched'


def test_revision_keeps_old_publication_interval_and_history_readable(tmp_path):
    db = db_at(tmp_path)
    # The original route-B manifest exists (staging) and its third piece is
    # sealed as belonging to another device.
    import_logger(db, B_LEGACY / 'manifest.json')
    import_logger(db, B_LEGACY / 'piece-1.json')
    import_logger(db, B_LEGACY / 'piece-2.json')
    with pytest.raises(ValueError, match='piece_device_mismatch'):
        import_logger(db, B_LEGACY / 'piece-3.json')
    # Route A then commits: the old public answer was 120, closed.
    deliver_a(db)
    old = summary(db)[0]
    assert old['peak'] == 120 and access_for(db, 'S1')['access'] == 'closed'
    # The device-signed correction supersedes the old manifest and, because it
    # reports the same station, the earlier active publication.
    for name in ('piece-1.json', 'piece-2.json', 'manifest.json'):
        import_logger(db, B_CORRECTED / name)
    now = history(db)
    assert [h['status'] for h in now] == ['closed', 'active']
    old_interval, new_interval = now
    assert old_interval['effective_to'] is not None
    assert new_interval['effective_to'] is None
    # No raw bytes were changed, no samples were deleted.
    assert old_interval['stations'] == [{'station': 'S1', 'peak': 120, 'count': 2}]
    assert new_interval['stations'] == [{'station': 'S1', 'peak': 80, 'count': 1}]
    # Historical report: before the correction the answer was 120/closed.
    assert summary(db, as_of=old_interval['effective_from'])[0]['peak'] == 120
    assert access_for(db, 'S1', as_of=old_interval['effective_from'])['access'] == 'closed'
    assert summary(db)[0]['peak'] == 80
    assert access_for(db, 'S1')['access'] == 'open'
    with sqlite3.connect(db) as con:
        assert con.execute('SELECT COUNT(*) FROM salinity_sample').fetchone()[0] == 3
        assert con.execute("SELECT status FROM salinity_source WHERE manifest_key='logger-A@1'"
                           ).fetchone()[0] == 'superseded'
        assert con.execute("SELECT status FROM salinity_manifest WHERE manifest_key='logger-B@1'"
                           ).fetchone()[0] == 'revoked'
    assert audit(db)['status'] == 'matched'


# ---------------------------------------------------------------------------
# Concurrency: two collectors racing the same manifest
# ---------------------------------------------------------------------------

def _race(db, workers=6):
    barrier = threading.Barrier(workers)
    outcomes, errors = [], []

    def run(collector):
        try:
            barrier.wait()
            outcomes.append(try_commit_group(db, 'logger-A@1', collector=collector))
        except Exception as exc:  # pragma: no cover - surfaced via assertion
            errors.append(repr(exc))

    threads = [threading.Thread(target=run, args=(f'collector-{i}',)) for i in range(workers)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    return outcomes, errors


def test_concurrent_collectors_produce_exactly_one_commit(tmp_path):
    db = db_at(tmp_path)
    deliver_a(db)   # committed before the race, then race re-commit attempts
    outcomes, errors = _race(db)
    assert not errors
    assert len(outcomes) == 6
    assert all(o['status'] == 'committed' for o in outcomes)
    assert all(o['samples'] == 2 for o in outcomes)
    assert sum(1 for o in outcomes if not o['replayed']) == 0   # already committed
    with sqlite3.connect(db) as con:
        assert con.execute("SELECT COUNT(*) FROM salinity_source WHERE manifest_key='logger-A@1'"
                           ).fetchone()[0] == 1
        assert con.execute("SELECT COUNT(*) FROM salinity_publication WHERE manifest_key='logger-A@1'"
                           ).fetchone()[0] == 1
        assert con.execute("SELECT COUNT(*) FROM salinity_sample").fetchone()[0] == 2


def test_two_collectors_receiving_the_final_piece_concurrently_publish_once(tmp_path):
    db = db_at(tmp_path)
    import_logger(db, A / 'manifest.json')
    import_logger(db, A / 'piece-1.json')
    import_logger(db, A / 'piece-2.json')
    barrier = threading.Barrier(2)
    results, errors = [], []

    def run(collector):
        try:
            barrier.wait()
            results.append(import_logger(db, A / 'piece-3.json', collector=collector))
        except Exception as exc:  # pragma: no cover
            errors.append(repr(exc))

    threads = [threading.Thread(target=run, args=(f'c{i}',)) for i in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors
    assert [r['status'] for r in results] == ['committed', 'committed']
    # Exactly one of the two receipts actually published the group; the other
    # replayed it.  The replayed flags may both read True when the loser
    # finishes after the winner, so the durable ledger is the authority.
    with sqlite3.connect(db) as con:
        assert con.execute('SELECT COUNT(*) FROM salinity_sample').fetchone()[0] == 2
        assert con.execute('SELECT COUNT(*) FROM salinity_source').fetchone()[0] == 1
        assert con.execute('SELECT COUNT(*) FROM salinity_publication').fetchone()[0] == 1
        assert con.execute("SELECT COUNT(*) FROM salinity_event WHERE event='group_committed'"
                           ).fetchone()[0] == 1


# ---------------------------------------------------------------------------
# Crash seams: restart converges to the uninterrupted result
# ---------------------------------------------------------------------------

def snapshot(db):
    """Public and protocol state that must converge, excluding wall-clock stamps."""
    state = statuses(db)
    for source in state['sources']:
        source.pop('raw', None)
        source.pop('committed_at', None)
        source.pop('created_at', None)
    return state, summary(db), pending(db), access_report(db)


def _clean_route_a(tmp_path):
    db = db_at(tmp_path)
    deliver_a(db)
    return snapshot(db)


@pytest.mark.parametrize('fault', FAULT_POINTS)
def test_crash_then_recover_matches_one_shot_import(tmp_path, fault):
    reference = _clean_route_a(tmp_path / 'ref')
    db = db_at(tmp_path / 'crash')
    names = ('manifest.json', 'piece-1.json', 'piece-2.json', 'piece-3.json')
    crashed_at = None
    for index, name in enumerate(names):
        try:
            import_logger(db, A / name, fault={fault})
        except InjectedCrash:
            crashed_at = index
            break
    assert crashed_at is not None
    # The USB still holds the rest of the capture: deliver it after restart.
    for name in names[crashed_at:]:
        import_logger(db, A / name)
    restored = recover(db)
    assert restored['restored'][0]['manifest_key'] == 'logger-A@1'
    assert restored['restored'][0]['outcome'] in ('committed', 'already_committed')
    assert snapshot(db) == reference
    assert audit(db)['status'] == 'matched'


@pytest.mark.parametrize('fault', FAULT_POINTS)
def test_crash_recovery_is_idempotent_when_run_twice(tmp_path, fault):
    db = db_at(tmp_path)
    names = ('manifest.json', 'piece-1.json', 'piece-2.json', 'piece-3.json')
    crashed_at = None
    for index, name in enumerate(names):
        try:
            import_logger(db, A / name, fault={fault})
        except InjectedCrash:
            crashed_at = index
            break
    assert crashed_at is not None
    first = recover(db)          # recover before the rest of the USB is read
    for name in names[crashed_at:]:
        import_logger(db, A / name)
    second = recover(db)
    # The recovery ledger grew, but no public state was duplicated.
    assert summary(db)[0]['count'] == 2
    assert len(statuses(db)['samples']) == 2
    assert len([s for s in statuses(db)['sources'] if s['status'] == 'committed']) == 1
    assert all(o['outcome'] in ('committed', 'already_committed', 'staging:missing_piece:1',
                                'staging:missing_piece:2', 'staging:missing_piece:3')
               for o in first['restored'] + second['restored'])
    assert first and second


@pytest.mark.parametrize('fault', FAULT_POINTS)
def test_restart_converges_even_if_the_crashed_packet_is_resent(tmp_path, fault):
    """Power loss, then the operator's field tool re-sends the same packet."""
    reference = _clean_route_a(tmp_path / 'ref')
    db = db_at(tmp_path / 'crash')
    names = ('manifest.json', 'piece-1.json', 'piece-2.json', 'piece-3.json')
    pending_names = list(names)
    crashed = False
    for index, name in enumerate(names):
        try:
            import_logger(db, A / name, fault={fault})
        except InjectedCrash:
            crashed = True
            pending_names = list(names[index:])
            break
    assert crashed
    for name in pending_names:          # retransmission after restart
        import_logger(db, A / name)
    recover(db)
    assert snapshot(db) == reference
    assert audit(db)['status'] == 'matched'


def test_crash_after_manifest_leaves_only_staging_and_no_peak(tmp_path):
    db = db_at(tmp_path)
    with pytest.raises(InjectedCrash):
        import_logger(db, A / 'manifest.json', fault={'after_manifest'})
    assert group_state(db, 'logger-A@1')['status'] == 'staging'
    assert summary(db) == [] and access_for(db, 'S1')['access'] == 'closed'
    # resending the exact bytes completes the staging step
    assert import_logger(db, A / 'manifest.json')['status'] == 'staging'


def test_crash_before_commit_rolls_the_whole_publication_back(tmp_path):
    db = db_at(tmp_path)
    import_logger(db, A / 'manifest.json')
    import_logger(db, A / 'piece-1.json')
    import_logger(db, A / 'piece-2.json')
    with pytest.raises(InjectedCrash):
        import_logger(db, A / 'piece-3.json', fault={'before_commit'})
    # piece 3 itself is durable, but nothing public was published
    assert statuses(db)['sources'] == []
    assert summary(db) == []
    import_logger(db, A / 'manifest.json')   # or any later packet / recover
    assert summary(db)[0]['peak'] == 120


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def test_cli_chunked_route_a_and_pending(tmp_path):
    db = tmp_path / 'cli.sqlite'

    def cli(*args):
        return subprocess.run([sys.executable, '-m', 'tidalrestore', *args, '--db', str(db)],
                              capture_output=True, text=True)

    out = cli('salinity-import', '--file', str(A / 'manifest.json'), '--collector', 'field-1')
    assert out.returncode == 0, out.stderr
    assert json.loads(out.stdout)['status'] == 'staging'
    assert json.loads(cli('salinity-summary').stdout) == []
    access = json.loads(cli('salinity-access').stdout)
    assert access[0]['access'] == 'closed' and access[0]['basis'] == 'pending'
    for index in (1, 3, 2):
        out = cli('salinity-import', '--file', str(A / f'piece-{index}.json'), '--collector', 'field-2')
        assert out.returncode == 0, out.stderr
    assert json.loads(cli('salinity-summary').stdout)[0]['peak'] == 120
    assert json.loads(cli('salinity-access').stdout)[0]['access'] == 'closed'
    assert json.loads(cli('salinity-pending').stdout) == {'staging': [], 'conflicts': []}
    assert json.loads(cli('salinity-history').stdout)[0]['status'] == 'active'
    assert json.loads(cli('salinity-group', '--manifest-key', 'logger-A@1').stdout)['status'] == 'committed'
    assert json.loads(cli('salinity-recover').stdout)['restored'][0]['outcome'] == 'already_committed'
    assert json.loads(cli('audit').stdout)['status'] == 'matched'


def test_cli_legacy_complete_file_still_works(tmp_path):
    db = tmp_path / 'legacy.sqlite'
    out = subprocess.run([sys.executable, '-m', 'tidalrestore', 'salinity-import',
                          '--db', str(db), '--file', str(FIX / 'salinity-logger-ok.json')],
                         capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
    assert json.loads(out.stdout)['status'] == 'committed'
    summary_out = subprocess.run([sys.executable, '-m', 'tidalrestore', 'salinity-summary',
                                  '--db', str(db)], capture_output=True, text=True)
    assert json.loads(summary_out.stdout)[0]['peak'] == 120
    access_out = subprocess.run([sys.executable, '-m', 'tidalrestore', 'salinity-access',
                                 '--db', str(db)], capture_output=True, text=True)
    assert json.loads(access_out.stdout)[0]['access'] == 'closed'
    assert json.loads(subprocess.run([sys.executable, '-m', 'tidalrestore', 'audit',
                                      '--db', str(db)], capture_output=True, text=True).stdout)['status'] == 'matched'


def test_legacy_id_with_different_bytes_is_rejected_and_sealed(tmp_path):
    db = db_at(tmp_path)
    original = FIX / 'salinity-logger-ok.json'
    import_logger(db, original)
    changed = tmp_path / 'changed.json'
    changed.write_text(json.dumps({'id': 'logger-1', 'kind': 'salinity_logger',
                                   'samples': [{'station': 'S1', 'at': '2026-04-03T08:00:00Z',
                                                'conductivity': 90, 'tide': 'flood'}]}))
    with pytest.raises(ValueError, match='logger_id_conflict'):
        import_logger(db, changed)
    assert statuses(db)['conflicts'][0]['reason'] == 'logger_id_conflict'
    assert summary(db)[0]['peak'] == 120


def test_migrates_baseline_database_without_losing_samples(tmp_path):
    """A DB written by the baseline release (old tables) keeps its public data."""
    db = db_at(tmp_path)
    with sqlite3.connect(db) as con:
        con.execute('CREATE TABLE salinity_source(id TEXT PRIMARY KEY,sha TEXT,raw BLOB)')
        con.execute('CREATE TABLE salinity_sample(source_id TEXT,station TEXT,at TEXT,'
                    'conductivity INTEGER,tide TEXT)')
        raw = (FIX / 'salinity-logger-ok.json').read_bytes()
        con.execute('INSERT INTO salinity_source VALUES(?,?,?)',
                    ('logger-1', hashlib.sha256(raw).hexdigest(), raw))
        for sample in doc(FIX / 'salinity-logger-ok.json')['samples']:
            con.execute('INSERT INTO salinity_sample VALUES(?,?,?,?,?)',
                        ('logger-1', sample['station'], sample['at'], sample['conductivity'], sample['tide']))
    assert summary(db)[0]['peak'] == 120
    assert access_for(db, 'S1')['access'] == 'closed'
    # A replay of the exact legacy file is idempotent on the migrated DB.
    assert import_logger(db, FIX / 'salinity-logger-ok.json')['replayed'] is True
    assert summary(db)[0]['count'] == 2
