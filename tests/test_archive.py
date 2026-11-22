"""Fault-injection and legal-preservation tests for offline cold archive.

Covers route A (destruction permit -> read-only content-addressed archive) and
route B (litigation hold keeps named originals hot), cross-plot shared
evidence, package and member tamper, permit withdrawal, concurrent archive
races, every power-loss point, reverse restore, repeated execution and the
audit matched / per-source failure contract.
"""
import os, json, glob, tarfile, hashlib, threading, subprocess, sys
from pathlib import Path

import pytest

from tidalrestore.baseline import import_baseline
from tidalrestore.material import import_material
from tidalrestore.works import import_contract, import_work
from tidalrestore.survey import import_survey
from tidalrestore.store import connect, report
from tidalrestore.audit import audit
from tidalrestore import archive as ar

FIXTURES = Path(__file__).resolve().parent.parent / 'fixtures'
LEGAL = FIXTURES / 'legal'


def write(tmp, name, doc):
    p = tmp / name
    p.write_text(json.dumps(doc), encoding='utf8')
    return p


def build_p1(db_path, tmp, material_quantity=100):
    """Import the five canonical P1 originals in the accepted ordering."""
    import_baseline(db_path, tmp / 'b.json' if False else write(tmp, 'baseline.json', json.loads((FIXTURES / 'baseline-ok.json').read_text())))
    mat = json.loads((FIXTURES / 'material-ok.json').read_text())
    mat['quantity'] = material_quantity
    import_material(db_path, write(tmp, 'material.json', mat))
    import_contract(db_path, write(tmp, 'contract.json', json.loads((FIXTURES / 'contract-ok.json').read_text())))
    import_work(db_path, write(tmp, 'work.json', json.loads((FIXTURES / 'work-ok.json').read_text())))
    import_survey(db_path, write(tmp, 'survey.json', json.loads((FIXTURES / 'survey-ok.json').read_text())))
    return db_path


def permit_doc(permit_id='PERMIT-A-P1', plot_id='P1', expires_at='2030-01-10T09:00:00Z'):
    return {'doc_type': 'destruction_permit', 'permit_id': permit_id, 'route': 'A',
            'plot_id': plot_id, 'scope': 'settled season originals',
            'authorized_by': ['owner-coast-authority', 'independent-archivist'],
            'custodian': 'offline-archive-vault', 'issued_at': '2027-01-10T09:00:00Z',
            'expires_at': expires_at}


def hold_doc(hold_id='HOLD-B-001', source_ids=('w1', 's1'), plot_id=None, order_ref='COURT-ORDER-2027-77'):
    d = {'doc_type': 'legal_hold', 'hold_id': hold_id, 'source_ids': list(source_ids),
         'order_ref': order_ref, 'issued_by': 'coastal-court', 'issued_at': '2027-02-01T09:00:00Z'}
    if plot_id:
        d['plot_id'] = plot_id
    return d


def ids(db):
    with connect(db) as c:
        return {r['id']: (r['raw'] is not None) for r in c.execute('SELECT id,raw FROM evidence ORDER BY seq')}


def hot_bytes(db):
    with connect(db) as c:
        return c.execute('SELECT COALESCE(SUM(LENGTH(raw)),0) FROM evidence').fetchone()[0]


# ---------------------------------------------------------------------------
# Route A
# ---------------------------------------------------------------------------
def test_route_a_moves_all_originals_and_audit_stays_matched(tmp_path):
    db = build_p1(tmp_path / 'p1.sqlite', tmp_path)
    vault = tmp_path / 'vault'
    ar.record_permit(db, write(tmp_path, 'permit.json', permit_doc()))
    before = hot_bytes(db)
    plan = ar.plan_archive(db, 'PERMIT-A-P1', vault)
    assert sorted(x['evidence_id'] for x in plan['eligible']) == ['b1', 'c1', 'm1', 's1', 'w1']
    assert plan['hot_bytes'] == before
    out = ar.export_archive(db, 'PERMIT-A-P1', vault)
    assert out['state'] == 'committed' and out['member_count'] == 5
    # Hot store shrinks by exactly the verifiable original byte total.
    assert hot_bytes(db) == 0
    status = ar.archive_status(db)
    assert status['cold_bytes'] == before and status['hot_bytes'] == 0
    # Cold audit replays all five originals and matches the eleven tables.
    res = audit(db, vault)
    assert res['status'] == 'matched' and res['cold_count'] == 5
    assert ar.consistency_check(db, vault)['status'] == 'consistent'
    # Package and index are read-only and content-addressed.
    pkg = glob.glob(str(vault / 'packages' / '*.tar'))[0]
    assert oct(os.stat(pkg).st_mode & 0o777) == '0o444'
    assert oct(os.stat(vault / 'index.json').st_mode & 0o777) == '0o444'
    index = json.loads((vault / 'index.json').read_text())
    assert index['packages'][0]['package_id'] == Path(pkg).stem


def test_route_a_report_and_acceptance_do_not_change_with_bytes(tmp_path):
    db = build_p1(tmp_path / 'p1.sqlite', tmp_path)
    vault = tmp_path / 'vault'
    ar.record_permit(db, write(tmp_path, 'permit.json', permit_doc()))
    before = report(db)
    ar.export_archive(db, 'PERMIT-A-P1', vault)
    assert report(db) == before
    ar.restore(db, vault, all_=True)
    assert report(db) == before


def test_restore_by_seq_returns_exact_original_bytes(tmp_path):
    db = build_p1(tmp_path / 'p1.sqlite', tmp_path)
    vault = tmp_path / 'vault'
    ar.record_permit(db, write(tmp_path, 'permit.json', permit_doc()))
    with connect(db) as c:
        seq = c.execute("SELECT seq FROM evidence WHERE id='c1'").fetchone()[0]
        original = c.execute("SELECT raw FROM evidence WHERE id='c1'").fetchone()[0]
    ar.export_archive(db, 'PERMIT-A-P1', vault)
    # Extract to files without touching the hot database.
    extracted = tmp_path / 'out'
    ar.restore(db, vault, seq=seq, out=extracted)
    got = list(extracted.glob('*.json'))[0].read_bytes()
    assert got == original
    # DB copy still cold until an in-place restore.
    assert ids(db)['c1'] is False
    ar.restore(db, vault, seq=seq)
    assert ids(db)['c1'] is True
    assert audit(db, vault)['status'] == 'matched'


def test_archive_without_root_arg_falls_back_to_stored_root(tmp_path):
    db = build_p1(tmp_path / 'p1.sqlite', tmp_path)
    vault = tmp_path / 'vault'
    ar.record_permit(db, write(tmp_path, 'permit.json', permit_doc()))
    ar.export_archive(db, 'PERMIT-A-P1', vault)
    assert audit(db)['status'] == 'matched'  # root resolved from batch row
    assert ar.consistency_check(db)['status'] == 'consistent'


# ---------------------------------------------------------------------------
# Route B
# ---------------------------------------------------------------------------
def test_route_b_hold_keeps_named_originals_hot_and_records_legal_ref(tmp_path):
    db = build_p1(tmp_path / 'p1.sqlite', tmp_path)
    vault = tmp_path / 'vault'
    ar.record_permit(db, write(tmp_path, 'permit.json', permit_doc()))
    ar.record_hold(db, write(tmp_path, 'hold.json', hold_doc()))
    out = ar.export_archive(db, 'PERMIT-A-P1', vault)
    assert sorted(out['archived']) == ['b1', 'c1', 'm1']
    state = ids(db)
    assert state['w1'] is True and state['s1'] is True  # instantly readable
    assert state['b1'] is False
    denials = ar.archive_status(db)['denials']
    refs = {d['evidence_id']: (d['reason'], d['legal_ref']) for d in denials}
    assert refs['w1'] == ('legal_hold_active', 'COURT-ORDER-2027-77/HOLD-B-001')
    assert refs['s1'][0] == 'legal_hold_active'
    # Mixed hot/cold replay still matches.
    assert audit(db, vault)['status'] == 'matched'


def test_lifting_hold_allows_remaining_originals_to_archive(tmp_path):
    db = build_p1(tmp_path / 'p1.sqlite', tmp_path)
    vault = tmp_path / 'vault'
    ar.record_permit(db, write(tmp_path, 'permit.json', permit_doc()))
    ar.record_hold(db, write(tmp_path, 'hold.json', hold_doc()))
    ar.export_archive(db, 'PERMIT-A-P1', vault)
    ar.lift_hold(db, 'HOLD-B-001')
    out = ar.export_archive(db, 'PERMIT-A-P1', vault)
    assert sorted(out['archived']) == ['s1', 'w1']
    assert hot_bytes(db) == 0
    assert audit(db, vault)['status'] == 'matched'


def test_plot_wide_hold_blocks_every_source_of_plot(tmp_path):
    db = build_p1(tmp_path / 'p1.sqlite', tmp_path)
    vault = tmp_path / 'vault'
    ar.record_permit(db, write(tmp_path, 'permit.json', permit_doc()))
    ar.record_hold(db, write(tmp_path, 'hold.json', hold_doc(source_ids=(), plot_id='P1')))
    out = ar.export_archive(db, 'PERMIT-A-P1', vault)
    assert out['state'] == 'nothing_eligible'
    assert all(hot is True for hot in ids(db).values())


# ---------------------------------------------------------------------------
# Cross-plot shared evidence and distinct legal identities
# ---------------------------------------------------------------------------
def _build_two_plots_shared_material(db, tmp):
    build_p1(db, tmp, material_quantity=300)
    b2 = dict(json.loads((FIXTURES / 'baseline-ok.json').read_text()), id='b2', plot_id='P2',
              transects=[{'id': 'u1', 'tier': 'high', 'coordinate': 'A', 'cover': 30},
                         {'id': 'u2', 'tier': 'mid', 'coordinate': 'B', 'cover': 20},
                         {'id': 'u3', 'tier': 'low', 'coordinate': 'C', 'cover': 10}])
    import_baseline(db, write(tmp, 'b2.json', b2))
    c2 = dict(json.loads((FIXTURES / 'contract-ok.json').read_text()), id='c2', contract_id='C2', plot_id='P2')
    import_contract(db, write(tmp, 'c2.json', c2))
    w2 = dict(json.loads((FIXTURES / 'work-ok.json').read_text()), id='w4', event_id='W4',
              contract_id='C2', cells=[{'id': 'D-H', 'tier': 'high', 'area': 20},
                                       {'id': 'D-M', 'tier': 'mid', 'area': 20},
                                       {'id': 'D-L', 'tier': 'low', 'area': 20}])
    import_work(db, write(tmp, 'w4.json', w2))
    s2 = dict(json.loads((FIXTURES / 'survey-ok.json').read_text()), id='s4', survey_id='S4', plot_id='P2')
    import_survey(db, write(tmp, 's4.json', s2))


def test_shared_source_needs_every_plot_permitted(tmp_path):
    db = tmp_path / 'shared.sqlite'
    _build_two_plots_shared_material(db, tmp_path)
    vault = tmp_path / 'vault'
    ar.record_permit(db, write(tmp_path, 'p1.json', permit_doc()))
    with connect(db) as c:
        ar.rebuild_usage(c)
        assert sorted(r[0] for r in c.execute(
            "SELECT DISTINCT plot_id FROM evidence_usage WHERE evidence_id='m1'")) == ['P1', 'P2']
    plan = ar.plan_archive(db, 'PERMIT-A-P1', vault)
    m1 = next(x for x in plan['review'] if x['evidence_id'] == 'm1')
    assert m1['reason'] == 'plot_without_active_permit'
    # P1-only export must not move the shared source.
    out = ar.export_archive(db, 'PERMIT-A-P1', vault)
    assert 'm1' not in out['archived'] and ids(db)['m1'] is True
    # A P2 permit covers the second usage; now both plots allow the move.
    ar.record_permit(db, write(tmp_path, 'p2.json', permit_doc('PERMIT-A-P2', 'P2')))
    out2 = ar.export_archive(db, 'PERMIT-A-P1', vault)
    assert 'm1' in out2['archived'] and ids(db)['m1'] is False
    assert audit(db, vault)['status'] == 'matched'


def test_distinct_evidence_ids_keep_own_seq_and_manifest_identity(tmp_path):
    db = build_p1(tmp_path / 'p1.sqlite', tmp_path)
    vault = tmp_path / 'vault'
    # A second material evidence id, actually consumed by a second contract,
    # so it carries a real business usage and is eligible to archive.
    twin = json.loads((FIXTURES / 'material-ok.json').read_text())
    twin['id'] = 'm1-twin'
    twin['batch_id'] = 'M1-TWIN'
    twin['quantity'] = 60
    import_material(db, write(tmp_path, 'twin.json', twin))
    c2 = dict(json.loads((FIXTURES / 'contract-ok.json').read_text()), id='c2', contract_id='C2')
    import_contract(db, write(tmp_path, 'c2.json', c2))
    w2 = dict(json.loads((FIXTURES / 'work-ok.json').read_text()), id='w4', event_id='W4',
              contract_id='C2', material_id='M1-TWIN',
              cells=[{'id': 'D-H', 'tier': 'high', 'area': 20},
                     {'id': 'D-M', 'tier': 'mid', 'area': 20},
                     {'id': 'D-L', 'tier': 'low', 'area': 20}])
    import_work(db, write(tmp_path, 'w4.json', w2))
    ar.record_permit(db, write(tmp_path, 'permit.json', permit_doc()))
    with connect(db) as c:
        rows = c.execute("SELECT id,seq,sha FROM evidence ORDER BY seq").fetchall()
        by_id = {r['id']: r for r in rows}
        assert {'m1', 'm1-twin'} <= set(by_id)
        assert by_id['m1']['seq'] != by_id['m1-twin']['seq']
        assert by_id['m1']['sha'] != by_id['m1-twin']['sha']
    ar.export_archive(db, 'PERMIT-A-P1', vault)
    pkg = glob.glob(str(vault / 'packages' / '*.tar'))[0]
    with tarfile.open(pkg) as t:
        manifest = json.loads(t.extractfile(t.getmember('manifest.json')).read())
    member_ids = {m['evidence_id'] for m in manifest['members']}
    assert {'m1', 'm1-twin'} <= member_ids
    twins = [m for m in manifest['members'] if m['evidence_id'] in ('m1', 'm1-twin')]
    assert {m['seq'] for m in twins} == {by_id['m1']['seq'], by_id['m1-twin']['seq']}
    ar.restore(db, vault, all_=True)
    assert audit(db, vault)['status'] == 'matched'


# ---------------------------------------------------------------------------
# Tamper: package digest and per-source SHA
# ---------------------------------------------------------------------------
def test_package_tamper_audit_names_each_source_not_just_divergent(tmp_path):
    db = build_p1(tmp_path / 'p1.sqlite', tmp_path)
    vault = tmp_path / 'vault'
    ar.record_permit(db, write(tmp_path, 'permit.json', permit_doc()))
    ar.export_archive(db, 'PERMIT-A-P1', vault)
    pkg = Path(glob.glob(str(vault / 'packages' / '*.tar'))[0])
    os.chmod(pkg, 0o644)
    with open(pkg, 'r+b') as f:
        f.seek(100); f.write(b'CORRUPT!!')
    os.chmod(pkg, 0o444)
    res = audit(db, vault)
    assert res['status'] == 'cold_evidence_failed'
    assert {e['source_id'] for e in res['errors']} == {'b1', 'c1', 'm1', 's1', 'w1'}
    assert all('package_tampered' in e['error'] for e in res['errors'])
    check = ar.consistency_check(db, vault)
    assert check['status'] == 'inconsistent'
    assert {e['source_id'] for e in check['errors'] if e['code'] == 'package_tampered'} == \
        {'b1', 'c1', 'm1', 's1', 'w1'}


def test_member_sha_tamper_names_exact_source(tmp_path):
    db = build_p1(tmp_path / 'p1.sqlite', tmp_path)
    vault = tmp_path / 'vault'
    ar.record_permit(db, write(tmp_path, 'permit.json', permit_doc()))
    ar.export_archive(db, 'PERMIT-A-P1', vault)
    pkg = Path(glob.glob(str(vault / 'packages' / '*.tar'))[0])
    with connect(db) as c:
        m1_sha = c.execute("SELECT sha FROM evidence WHERE id='m1'").fetchone()[0]
    with tarfile.open(pkg) as t:
        manifest = json.loads(t.extractfile(t.getmember('manifest.json')).read())
        blobs = {m['sha']: t.extractfile(t.getmember(m['member'])).read() for m in manifest['members']}
    blobs[m1_sha] = b'x' * len(blobs[m1_sha])  # wrong bytes, same length
    new_tar = ar._make_tar(manifest, blobs)
    digest = hashlib.sha256(new_tar).hexdigest()
    os.chmod(pkg, 0o644); pkg.write_bytes(new_tar); os.chmod(pkg, 0o444)
    with connect(db) as c, c:
        c.execute('UPDATE archive_batch SET package_digest=?', (digest,))
    res = audit(db, vault)
    assert res['status'] == 'cold_evidence_failed'
    assert len(res['errors']) == 1 and res['errors'][0]['source_id'] == 'm1'
    assert 'member_sha_mismatch' in res['errors'][0]['error']


def test_missing_package_is_reported_per_source(tmp_path):
    db = build_p1(tmp_path / 'p1.sqlite', tmp_path)
    vault = tmp_path / 'vault'
    ar.record_permit(db, write(tmp_path, 'permit.json', permit_doc()))
    ar.export_archive(db, 'PERMIT-A-P1', vault)
    for p in (vault / 'packages').glob('*.tar'):
        os.chmod(p, 0o644); p.unlink()
    res = audit(db, vault)
    assert res['status'] == 'cold_evidence_failed'
    assert {e['source_id'] for e in res['errors']} == {'b1', 'c1', 'm1', 's1', 'w1'}
    assert all('package_missing' in e['error'] for e in res['errors'])


# ---------------------------------------------------------------------------
# Permit withdrawal and concurrent archive
# ---------------------------------------------------------------------------
def test_revoked_permit_blocks_export_and_keeps_hot_bytes(tmp_path):
    db = build_p1(tmp_path / 'p1.sqlite', tmp_path)
    vault = tmp_path / 'vault'
    ar.record_permit(db, write(tmp_path, 'permit.json', permit_doc()))
    ar.revoke_permit(db, 'PERMIT-A-P1', 'owner withdrawal')
    out = ar.export_archive(db, 'PERMIT-A-P1', vault)
    assert out['state'] == 'nothing_eligible'
    assert all(x['reason'] == 'permit_revoked' for x in out['blocked'])
    assert hot_bytes(db) > 0


def test_inflight_hold_aborts_commit_atomically(tmp_path):
    db = build_p1(tmp_path / 'p1.sqlite', tmp_path)
    vault = tmp_path / 'vault'
    ar.record_permit(db, write(tmp_path, 'permit.json', permit_doc()))
    original = ar._plan_archive

    def flaky(c, permit_id, root, now):
        if not getattr(flaky, 'fired', False) and c.in_transaction:
            flaky.fired = True
            c.execute("INSERT INTO legal_hold(hold_id,plot_id,source_ids,order_ref,issued_by,issued_at,document_sha)"
                      " VALUES(?,?,?,?,?,?,?)",
                      ('HOLD-RACE', None, '["w1"]', 'RACE-1', 'court', now, 'x'))
        return original(c, permit_id, root, now)

    ar._plan_archive = flaky
    try:
        with pytest.raises(ar.ArchiveConflict, match='eligibility_changed'):
            ar.export_archive(db, 'PERMIT-A-P1', vault)
    finally:
        ar._plan_archive = original
    # Nothing left half-archived: no pointers, all bytes hot, no committed batch.
    with connect(db) as c:
        assert c.execute('SELECT COUNT(*) FROM archived_evidence').fetchone()[0] == 0
        assert c.execute('SELECT COUNT(*) FROM evidence WHERE raw IS NOT NULL').fetchone()[0] == 5
        assert c.execute("SELECT state FROM archive_batch").fetchone()[0] in ('failed', 'sealed')
    assert audit(db)['status'] == 'matched'


def test_concurrent_exports_are_serialized(tmp_path):
    db = build_p1(tmp_path / 'p1.sqlite', tmp_path)
    vault = tmp_path / 'vault'
    ar.record_permit(db, write(tmp_path, 'permit.json', permit_doc()))
    results, errors = [], []

    def worker():
        try:
            results.append(ar.export_archive(db, 'PERMIT-A-P1', vault))
        except Exception as e:  # one contender may lose the race, never corrupt
            errors.append(type(e).__name__)

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    committed = [r for r in results if r['state'] == 'committed']
    assert len(committed) == 1
    with connect(db) as c:
        assert c.execute('SELECT COUNT(*) FROM evidence WHERE raw IS NOT NULL').fetchone()[0] == 0
        # No duplicated cold pointers.
        assert c.execute('SELECT COUNT(*) FROM archived_evidence').fetchone()[0] == 5
    assert audit(db, vault)['status'] == 'matched'


# ---------------------------------------------------------------------------
# Power-loss points and recovery convergence
# ---------------------------------------------------------------------------
@pytest.mark.parametrize('fault,expect_hot', [
    ('after_staging_file', 5),
    ('after_index_write', 5),
    ('before_hot_delete', 5),
    ('after_hot_delete', 5),
])
def test_crash_points_converge_after_recovery(tmp_path, fault, expect_hot):
    db = build_p1(tmp_path / 'p1.sqlite', tmp_path)
    vault = tmp_path / 'vault'
    ar.record_permit(db, write(tmp_path, 'permit.json', permit_doc()))
    with pytest.raises(RuntimeError):
        ar.export_archive(db, 'PERMIT-A-P1', vault, fault=fault)
    with connect(db) as c:
        assert c.execute('SELECT COUNT(*) FROM evidence WHERE raw IS NOT NULL').fetchone()[0] == expect_hot
        assert c.execute('SELECT COUNT(*) FROM archived_evidence').fetchone()[0] == 0
    summary = ar.recover(db, vault)
    # Either the staging batch rolled back or the sealed batch recommitted.
    assert summary['rolled_back'] or summary['recommitted']
    assert audit(db, vault)['status'] == 'matched'
    assert ar.consistency_check(db, vault)['status'] == 'consistent'
    # No staging leftovers survive recovery.
    assert list((vault / 'packages').glob('*.tmp')) == []


def test_staging_corruption_never_archives_half_evidence(tmp_path):
    db = build_p1(tmp_path / 'p1.sqlite', tmp_path)
    vault = tmp_path / 'vault'
    ar.record_permit(db, write(tmp_path, 'permit.json', permit_doc()))
    with pytest.raises(ar.ArchiveVerifyError, match='staging_verify_failed'):
        ar.export_archive(db, 'PERMIT-A-P1', vault, fault='staging_corrupt')
    with connect(db) as c:
        assert c.execute('SELECT COUNT(*) FROM evidence WHERE raw IS NOT NULL').fetchone()[0] == 5
        assert c.execute("SELECT state FROM archive_batch").fetchone()[0] == 'failed'
    assert list((vault / 'packages').glob('*.tar')) == []
    assert audit(db)['status'] == 'matched'


def test_recovery_corrupt_sealed_package_quarantines_and_fails(tmp_path):
    db = build_p1(tmp_path / 'p1.sqlite', tmp_path)
    vault = tmp_path / 'vault'
    ar.record_permit(db, write(tmp_path, 'permit.json', permit_doc()))
    with pytest.raises(RuntimeError):
        ar.export_archive(db, 'PERMIT-A-P1', vault, fault='before_hot_delete')
    pkg = next((vault / 'packages').glob('*.tar'))
    os.chmod(pkg, 0o644)
    with open(pkg, 'r+b') as f:
        f.seek(50); f.write(b'BAD')
    summary = ar.recover(db, vault)
    assert summary['failed']
    assert list((vault / 'packages').glob('*.corrupt'))
    # Hot originals remain intact and replayable without the vault.
    assert hot_bytes(db) > 0
    assert audit(db)['status'] == 'matched'


# ---------------------------------------------------------------------------
# Reverse restore, repeated execution, validation
# ---------------------------------------------------------------------------
def test_full_restore_and_re_archive_is_idempotent(tmp_path):
    db = build_p1(tmp_path / 'p1.sqlite', tmp_path)
    vault = tmp_path / 'vault'
    ar.record_permit(db, write(tmp_path, 'permit.json', permit_doc()))
    ar.export_archive(db, 'PERMIT-A-P1', vault)
    first = ar.archive_status(db)
    ar.restore(db, vault, all_=True)
    assert hot_bytes(db) == first['cold_bytes']
    # Re-archive moves them again; third run reports nothing eligible.
    again = ar.export_archive(db, 'PERMIT-A-P1', vault)
    assert again['state'] == 'committed'
    third = ar.export_archive(db, 'PERMIT-A-P1', vault)
    assert third['state'] == 'nothing_eligible'
    assert audit(db, vault)['status'] == 'matched'


def test_reimporting_archived_source_rehydrates_hot_copy(tmp_path):
    db = build_p1(tmp_path / 'p1.sqlite', tmp_path)
    vault = tmp_path / 'vault'
    ar.record_permit(db, write(tmp_path, 'permit.json', permit_doc()))
    # Capture the exact bytes that were ingested (including any test tweaks).
    with connect(db) as c:
        original = c.execute("SELECT raw FROM evidence WHERE id='m1'").fetchone()[0]
    source = write(tmp_path, 'm1-original.json', json.loads(original))
    ar.export_archive(db, 'PERMIT-A-P1', vault)
    assert ids(db)['m1'] is False
    # Re-importing the identical original file is idempotent and restores bytes.
    result = import_material(db, source)
    assert result.get('replayed') is True
    assert ids(db)['m1'] is True
    with connect(db) as c:
        row = c.execute("SELECT sha,raw FROM evidence WHERE id='m1'").fetchone()
        assert hashlib.sha256(row['raw']).hexdigest() == row['sha']


def test_permit_and_hold_validation(tmp_path):
    db = build_p1(tmp_path / 'p1.sqlite', tmp_path)
    bad_permit = write(tmp_path, 'bp.json', {'doc_type': 'destruction_permit', 'permit_id': 'X',
                                             'route': 'Z', 'scope': 's', 'authorized_by': ['only-one'],
                                             'custodian': 'c', 'issued_at': '2027-01-01T00:00:00Z'})
    with pytest.raises(ar.ArchiveError):
        ar.record_permit(db, bad_permit)
    bad_hold = write(tmp_path, 'bh.json', {'doc_type': 'legal_hold', 'hold_id': 'H',
                                           'order_ref': 'o', 'issued_by': 'c', 'issued_at': 't'})
    with pytest.raises(ar.ArchiveError, match='hold_needs_plot_or_sources'):
        ar.record_hold(db, bad_hold)
    unknown = write(tmp_path, 'uh.json', hold_doc(source_ids=('ghost',)))
    with pytest.raises(ar.ArchiveError, match='hold_unknown_source'):
        ar.record_hold(db, unknown)


def test_unknown_permit_and_restore_targets(tmp_path):
    db = build_p1(tmp_path / 'p1.sqlite', tmp_path)
    with pytest.raises(ar.ArchiveError, match='unknown_permit'):
        ar.plan_archive(db, 'NOPE', tmp_path / 'v')
    with pytest.raises(ar.ArchiveError, match='restore_needs_target'):
        ar.restore(db)
    with pytest.raises(ar.ArchiveError, match='restore_target_not_archived'):
        ar.restore(db, evidence_id='b1')


# ---------------------------------------------------------------------------
# Schema migration from a pre-archive NOT NULL raw database
# ---------------------------------------------------------------------------
def test_legacy_not_null_raw_database_migrates(tmp_path):
    import sqlite3
    # Build a database using the historical schema where evidence.raw was
    # NOT NULL, then confirm connect() migrates it without losing bytes.
    db_path = tmp_path / 'legacy.sqlite'
    legacy = sqlite3.connect(db_path)
    legacy.execute('CREATE TABLE evidence(id TEXT PRIMARY KEY,kind TEXT NOT NULL,sha TEXT NOT NULL,'
                   'raw BLOB NOT NULL,seq INTEGER NOT NULL UNIQUE)')
    legacy.execute('CREATE TABLE plot(id TEXT PRIMARY KEY,area INTEGER NOT NULL,season TEXT NOT NULL,'
                   'baseline_status TEXT NOT NULL)')
    legacy.commit(); legacy.close()
    # Pre-migration the column really is NOT NULL.
    pre = sqlite3.connect(db_path)
    raw_col = [c for c in pre.execute('PRAGMA table_info(evidence)') if c[1] == 'raw'][0]
    assert raw_col[3] == 1  # notnull flag
    pre.close()
    build_p1(db_path, tmp_path)  # first connect() here runs the migration
    vault = tmp_path / 'vault'
    ar.record_permit(db_path, write(tmp_path, 'permit.json', permit_doc()))
    ar.export_archive(db_path, 'PERMIT-A-P1', vault)
    assert audit(db_path, vault)['status'] == 'matched'
    with connect(db_path) as c:
        assert c.execute('SELECT COUNT(*) FROM evidence WHERE raw IS NULL').fetchone()[0] == 5


# ---------------------------------------------------------------------------
# CLI surface
# ---------------------------------------------------------------------------
def test_shipped_legal_fixtures_drive_route_a_and_b(tmp_path):
    db = build_p1(tmp_path / 'p1.sqlite', tmp_path)
    vault = tmp_path / 'vault'
    # Route A fixture: owner + independent archivist destruction permit.
    ar.record_permit(db, LEGAL / 'permit-a.json')
    # Route B fixture: court order naming only the work and survey originals.
    ar.record_hold(db, LEGAL / 'hold-b.json')
    out = ar.export_archive(db, 'PERMIT-A-P1', vault)
    assert sorted(out['archived']) == ['b1', 'c1', 'm1']
    assert ids(db)['w1'] and ids(db)['s1']
    assert audit(db, vault)['status'] == 'matched'


def test_cli_archive_lifecycle(tmp_path):
    db = tmp_path / 'cli.sqlite'
    vault = tmp_path / 'vault'
    py = sys.executable

    def cli(*args):
        return subprocess.run([py, '-B', '-m', 'tidalrestore', *args, '--db', str(db)],
                              capture_output=True, text=True, cwd=Path(__file__).resolve().parent.parent)

    assert cli('demo').returncode == 0
    permit = write(tmp_path, 'permit.json', permit_doc())
    hold = write(tmp_path, 'hold.json', hold_doc())
    assert cli('permit-record', '--file', str(permit)).returncode == 0
    assert cli('hold-record', '--file', str(hold)).returncode == 0
    plan = json.loads(cli('archive-plan', '--permit-id', 'PERMIT-A-P1', '--archive-root', str(vault)).stdout)
    assert sorted(x['evidence_id'] for x in plan['eligible']) == ['b1', 'c1', 'm1']
    exported = json.loads(cli('archive-export', '--permit-id', 'PERMIT-A-P1', '--archive-root', str(vault)).stdout)
    assert sorted(exported['archived']) == ['b1', 'c1', 'm1']
    checked = json.loads(cli('archive-check', '--archive-root', str(vault)).stdout)
    assert checked['status'] == 'consistent'
    audited = json.loads(cli('audit', '--archive-root', str(vault)).stdout)
    assert audited['status'] == 'matched'
    restored = json.loads(cli('archive-restore', '--archive-root', str(vault), '--all').stdout)
    assert len(restored['restored']) == 3
