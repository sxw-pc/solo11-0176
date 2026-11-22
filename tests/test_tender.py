import json
import pytest
from tidalrestore.baseline import import_baseline
from tidalrestore.works import import_contract, import_work
from tidalrestore.survey import import_survey
from tidalrestore.store import report, connect
from tidalrestore.audit import audit
from tidalrestore.material import import_material
from tidalrestore.tender import (import_tender_bundle, import_tender_award,
                                 import_tender_withdrawal, reopen_gap_award)

FIX = __import__('pathlib').Path(__file__).parent.parent / 'fixtures'


def write(tmp, name, doc):
    p = tmp / name
    p.write_text(json.dumps(doc, ensure_ascii=False), encoding='utf8')
    return p


def load_bundle():
    return json.loads((FIX / 'tender-bundles.json').read_text(encoding='utf8'))


def award_doc(aid, rule):
    return {'id': 'aw-' + aid, 'kind': 'tender_award',
            'award_id': aid, 'tender_id': 'T1', 'rule': rule}


def baseline_doc(plot='P1', area=100):
    return dict(id='b-' + plot, kind='baseline', plot_id=plot, area=area, season='spring',
                transects=[dict(id='t1-' + plot, tier='high', coordinate='A', cover=30),
                           dict(id='t2-' + plot, tier='mid', coordinate='B', cover=20),
                           dict(id='t3-' + plot, tier='low', coordinate='C', cover=10)])


def material_doc(mid, **changes):
    d = dict(id='m-' + mid, kind='material', batch_id=mid, species='native-grass',
             origin='local', quantity=100, valid_until='2026-12-31T00:00:00Z')
    d.update(changes)
    return d


def contract_doc(cid, plot='P1', **changes):
    d = dict(id='c-' + cid, kind='contract', contract_id=cid, plot_id=plot,
             species='native-grass', planned_area=60,
             window_start='2026-04-01T08:00:00Z', window_end='2026-04-01T12:00:00Z',
             repair_start='2026-04-04T08:00:00Z', repair_end='2026-04-05T12:00:00Z',
             contractor='build-a')
    d.update(changes)
    return d


def work_doc(wid, cid='C1', mid='M1'):
    return dict(id='w-' + wid, kind='work', event_id=wid, contract_id=cid,
                event_type='install', area=60, at='2026-04-01T10:00:00Z',
                material_id=mid,
                cells=[dict(id='C-H', tier='high', area=20),
                       dict(id='C-M', tier='mid', area=20),
                       dict(id='C-L', tier='low', area=20)])


def capture(tmp, db, doc, name='bundle.json'):
    return import_tender_bundle(db, write(tmp, name, doc))


# --- Acceptance 1: original real-material control still holds -----------------

def test_original_m1_supports_w1_but_m2_foreign_does_not(tmp_path):
    db = tmp_path / 'ctrl.sqlite'
    import_baseline(db, write(tmp_path, 'base.json', baseline_doc()))
    import_material(db, write(tmp_path, 'm1.json', material_doc('M1')))
    import_contract(db, write(tmp_path, 'c1.json', contract_doc('C1')))
    import_work(db, write(tmp_path, 'w1.json', work_doc('W1')))
    assert report(db)['work_cells'] and report(db)['materials'][0]['status'] == 'approved'
    import_material(db, write(tmp_path, 'm2.json',
                              material_doc('M2', id='m-M2', species='foreign-grass', origin='overseas')))
    with pytest.raises(ValueError, match='material_not_eligible'):
        import_work(db, write(tmp_path, 'w2.json', work_doc('W2', mid='M2')))
    assert audit(db)['status'] == 'matched'


# --- Acceptance 2: capture closure watermarks ---------------------------------

def test_out_of_order_pages_still_close_and_page_order_cannot_change_award(tmp_path):
    db = tmp_path / 't.sqlite'
    result = capture(tmp_path, db, load_bundle())
    assert result['status'] == 'closed'
    # Shuffling page arrival order produces the identical award.
    shuffled = load_bundle()
    shuffled['id'] = 'tb1-shuffle'
    shuffled['pages'] = list(reversed(shuffled['pages']))
    db2 = tmp_path / 't2.sqlite'
    assert capture(tmp_path, db2, shuffled, 'shuffle.json')['status'] == 'closed'
    r1 = import_tender_award(db, write(tmp_path, 'a1.json', award_doc('AW1', 'lowest_total')))
    r2 = import_tender_award(db2, write(tmp_path, 'a2.json', award_doc('AW1', 'lowest_total')))
    assert r1['packages'] == r2['packages'] == ['PKG-A-BUNDLE']


def test_missing_page_before_deadline_and_duplicate_snapshot_block_award(tmp_path):
    db = tmp_path / 't.sqlite'
    # Missing page 3: not closed, and its bids must not be treated as absent by
    # silently awarding from the partial snapshot.
    partial = load_bundle()
    partial['id'] = 'tb-partial'
    partial['expected_pages'] = 4
    r = capture(tmp_path, db, partial, 'partial.json')
    assert r['status'] == 'capture_not_closed' and 'missing_pages' in r['reason']
    with pytest.raises(ValueError, match='capture_not_closed'):
        import_tender_award(db, write(tmp_path, 'aw.json', award_doc('AWX', 'lowest_total')))
    # Watermark still before the submission deadline.
    early = load_bundle()
    early['id'] = 'tb-early'
    early['captured_at'] = '2026-02-20T16:00:00Z'
    db_e = tmp_path / 'e.sqlite'
    r = capture(tmp_path, db_e, early, 'early.json')
    assert 'capture_before_deadline' in r['reason']
    with pytest.raises(ValueError, match='capture_not_closed'):
        import_tender_award(db_e, write(tmp_path, 'aw.json', award_doc('AWX', 'lowest_total')))
    # The missing page later arrives: the same capture becomes closed.
    done = load_bundle()
    done['id'] = 'tb-done'
    done['expected_pages'] = 4
    done['pages'] = done['pages'] + [{
        'page_no': 4, 'snapshot_no': 1, 'fetched_at': '2026-02-20T17:50:00Z', 'bids': []}]
    r2 = capture(tmp_path, db, done, 'done.json')
    assert r2['status'] == 'closed'
    # Duplicate snapshot of an already seen page with a new snapshot number is
    # an explicit portal re-shoot conflict.
    dup = load_bundle()
    dup['id'] = 'tb-dup'
    dup['pages'][0]['snapshot_no'] = 2
    db_d = tmp_path / 'd.sqlite'
    r = capture(tmp_path, db_d, dup, 'dup.json')
    assert 'duplicate_snapshot_conflict' in r['reason']


def test_screened_and_withdrawn_bids_keep_original_and_reason(tmp_path):
    db = tmp_path / 't.sqlite'
    capture(tmp_path, db, load_bundle())
    bids = {r['bid_id']: r for r in report(db)['tender_bids']}
    assert bids['BID-F']['eligible'] == 0 and bids['BID-F']['reject_reason'] == 'non_native_species'
    d_versions = [r for r in report(db)['tender_bids'] if r['bid_id'] == 'BID-D']
    assert {r['version'] for r in d_versions} == {1, 2}
    assert all(r['withdrawn'] == 1 and r['reject_reason'] == 'withdrawn_version'
               for r in d_versions)
    assert all(r['eligible'] == 0 for r in d_versions)


# --- Acceptance 3: one snapshot, two authorized rules, different contracts ----

def test_lowest_total_awards_bundle_a_at_700(tmp_path):
    db = tmp_path / 't.sqlite'
    capture(tmp_path, db, load_bundle())
    r = import_tender_award(db, write(tmp_path, 'aw.json', award_doc('AW-LOW', 'lowest_total')))
    assert r['total_price'] == 700 and r['packages'] == ['PKG-A-BUNDLE']
    items = report(db)['tender_award_items']
    assert len(items) == 2 and {i['nursery_id'] for i in items} == {'nursery-a'}
    assert all(i['status'] == 'active' and json.loads(i['liability_json'])['withdrawal_breach']
               for i in items)
    # A second active award on the same tender is refused.
    with pytest.raises(ValueError, match='active_award_already_exists'):
        import_tender_award(db, write(tmp_path, 'aw2.json', award_doc('AW-LOW2', 'lowest_total')))


def test_provenance_diversity_awards_b_plus_c_at_800(tmp_path):
    db = tmp_path / 't.sqlite'
    capture(tmp_path, db, load_bundle())
    r = import_tender_award(db, write(tmp_path, 'aw.json', award_doc('AW-DIV', 'provenance_diversity')))
    assert r['total_price'] == 800 and sorted(r['packages']) == ['PKG-B-P1', 'PKG-C-P2']
    items = report(db)['tender_award_items']
    assert {i['nursery_id'] for i in items} == {'nursery-b', 'nursery-c'}


def test_bundle_is_atomic_and_partial_delivery_opens_both_gaps(tmp_path):
    db = tmp_path / 't.sqlite'
    capture(tmp_path, db, load_bundle())
    import_tender_award(db, write(tmp_path, 'aw.json', award_doc('AW-LOW', 'lowest_total')))
    # A says it can only deliver P1's 60: the bundle cannot be split into
    # partial performance; both plots remain owed via open gaps.
    wd = {'id': 'wd1', 'kind': 'tender_withdrawal', 'award_id': 'AW-LOW',
          'package_id': 'PKG-A-BUNDLE', 'delivered_plots': ['P1']}
    r = import_tender_withdrawal(db, write(tmp_path, 'wd.json', wd))
    assert r['status'] == 'breached'
    gaps = report(db)['tender_gaps']
    assert sorted((g['plot_id'], g['quantity'], g['status']) for g in gaps) == [
        ('P1', 60, 'open'), ('P2', 40, 'open')]
    assert all(g['reason'] == 'partial_bundle_delivery_not_severable' for g in gaps)
    # The old winner row stays on file, never rewritten to B/C at old prices.
    items = report(db)['tender_award_items']
    assert all(i['status'] == 'breached' and i['bid_id'] == 'BID-A' for i in items)
    # Reopening is a fresh, verifiable award at current snapshot prices.
    rr = reopen_gap_award(db, write(tmp_path, 're.json', award_doc('AW-RE', 'lowest_total')))
    assert sorted(rr['packages']) == ['PKG-B-P1', 'PKG-C-P2'] and rr['total_price'] == 800
    assert all(g['status'] == 're-awarded' for g in report(db)['tender_gaps'])
    assert audit(db)['status'] == 'matched'


def test_diversity_branch_withdrawal_leaves_single_plot_gap(tmp_path):
    db = tmp_path / 't.sqlite'
    capture(tmp_path, db, load_bundle())
    import_tender_award(db, write(tmp_path, 'aw.json', award_doc('AW-DIV', 'provenance_diversity')))
    wd = {'id': 'wd1', 'kind': 'tender_withdrawal', 'award_id': 'AW-DIV',
          'package_id': 'PKG-B-P1', 'delivered_plots': []}
    r = import_tender_withdrawal(db, write(tmp_path, 'wd.json', wd))
    assert r['status'] == 'partial_breach'
    gaps = report(db)['tender_gaps']
    assert [(g['plot_id'], g['quantity']) for g in gaps] == [('P1', 60)]
    # C's P2 item is still executable: different breach footprint than the
    # bundle branch, and no silent substitution at B's old price.
    active = [i for i in report(db)['tender_award_items'] if i['status'] == 'active']
    assert [(i['package_id'], i['plot_id']) for i in active] == [('PKG-C-P2', 'P2')]
    assert audit(db)['status'] == 'matched'


def test_cold_chain_truck_cannot_be_double_booked_in_one_tide_window(tmp_path):
    db = tmp_path / 't.sqlite'
    doc = load_bundle()
    # Force B and C onto the same truck and overlapping window: B+C becomes
    # infeasible while A's single package still delivers everything.
    for page in doc['pages']:
        for bid in page['bids']:
            for v in bid['versions']:
                if bid['bid_id'] in ('BID-B', 'BID-C'):
                    v['truck_id'] = 'truck-shared'
    capture(tmp_path, db, doc)
    with pytest.raises(ValueError, match='diversity_rule_unavailable'):
        import_tender_award(db, write(tmp_path, 'aw.json', award_doc('AW-DIV', 'provenance_diversity')))
    r = import_tender_award(db, write(tmp_path, 'aw2.json', award_doc('AW-LOW', 'lowest_total')))
    assert r['packages'] == ['PKG-A-BUNDLE']


def test_per_plot_greedy_unit_price_cannot_split_the_bundle(tmp_path):
    db = tmp_path / 't.sqlite'
    capture(tmp_path, db, load_bundle())
    import_tender_award(db, write(tmp_path, 'aw.json', award_doc('AW-LOW', 'lowest_total')))
    # Awarded items always carry the whole package; there is no P1-only A item
    # and the package price is stored once per package, never split per plot.
    items = report(db)['tender_award_items']
    a = [i for i in items if i['bid_id'] == 'BID-A']
    assert {i['plot_id'] for i in a} == {'P1', 'P2'}
    assert {i['package_price'] for i in a} == {700}


# --- Acceptance 4: no fake material balances ----------------------------------

def test_awards_and_withdrawals_create_no_material_balance(tmp_path):
    db = tmp_path / 't.sqlite'
    capture(tmp_path, db, load_bundle())
    import_tender_award(db, write(tmp_path, 'aw.json', award_doc('AW-LOW', 'lowest_total')))
    import_tender_withdrawal(db, write(tmp_path, 'wd.json', {
        'id': 'wd1', 'kind': 'tender_withdrawal', 'award_id': 'AW-LOW',
        'package_id': 'PKG-A-BUNDLE', 'delivered_plots': ['P1']}))
    reopen_gap_award(db, write(tmp_path, 're.json', award_doc('AW-RE', 'lowest_total')))
    assert report(db)['materials'] == []
    # A real arrival against the old breached package is independently screened.
    res = import_material(db, write(tmp_path, 'arr.json', material_doc(
        'M9', id='m9', quantity=60, award_id='AW-LOW', package_id='PKG-A-BUNDLE')))
    assert res['status'] == 'approved'
    arrival = report(db)['material_arrivals'][0]
    assert arrival['batch_id'] == 'M9' and arrival['matched'] == 1
    # Foreign arrival referencing a valid native award cannot launder through it.
    bad = import_material(db, write(tmp_path, 'arr-bad.json', material_doc(
        'M10', id='m10', species='foreign-grass', origin='overseas',
        award_id='AW-RE', package_id='PKG-B-P1')))
    assert bad['status'] == 'quarantined' and bad['award_matched'] is False
    row = [x for x in report(db)['material_arrivals'] if x['batch_id'] == 'M10'][0]
    assert row['matched'] == 0 and 'species_mismatch' in row['mismatch_reason']
    import_baseline(db, write(tmp_path, 'base.json', baseline_doc()))
    import_contract(db, write(tmp_path, 'c1.json', contract_doc(
        'C1', award_id='AW-RE', package_id='PKG-B-P1')))
    with pytest.raises(ValueError, match='material_not_eligible'):
        import_work(db, write(tmp_path, 'wbad.json', work_doc('WBAD', mid='M10')))
    with pytest.raises(ValueError, match='awarded_package_not_active'):
        import_contract(db, write(tmp_path, 'c2.json', contract_doc(
            'C2', contract_id='C2', award_id='AW-LOW', package_id='PKG-A-BUNDLE')))
    assert audit(db)['status'] == 'matched'


# --- Acceptance 5: replay and projection tamper detection ----------------------

def test_full_tender_lifecycle_replays_and_tamper_is_detected(tmp_path):
    db = tmp_path / 't.sqlite'
    capture(tmp_path, db, load_bundle())
    import_tender_award(db, write(tmp_path, 'aw.json', award_doc('AW-DIV', 'provenance_diversity')))
    # C withdraws from P2; B's P1 stays active and the P2 gap is honestly left
    # open because no second independent nursery can fill P2 alone under the
    # diversity rule (A's atomic bundle cannot serve a P2-only residual).
    import_tender_withdrawal(db, write(tmp_path, 'wd.json', {
        'id': 'wd1', 'kind': 'tender_withdrawal', 'award_id': 'AW-DIV',
        'package_id': 'PKG-C-P2', 'delivered_plots': []}))
    with pytest.raises(ValueError, match='no_feasible_award'):
        reopen_gap_award(db, write(tmp_path, 're.json', award_doc('AW-RE', 'provenance_diversity')))
    gaps = report(db)['tender_gaps']
    assert [(g['plot_id'], g['status']) for g in gaps] == [('P2', 'open')]
    assert audit(db)['status'] == 'matched'
    # Rewrite a contract projection row without evidence: replay diverges.
    with connect(db) as w:
        w.execute("UPDATE tender_award SET total_price=1 WHERE award_id='AW-DIV'")
    result = audit(db)
    assert result['status'] == 'divergent' and 'tender_awards' in result['differences']


def test_replay_is_byte_for_byte_deterministic_across_two_ledgers(tmp_path):
    docs = [load_bundle(), award_doc('AW-LOW', 'lowest_total'),
            {'id': 'wd1', 'kind': 'tender_withdrawal', 'award_id': 'AW-LOW',
             'package_id': 'PKG-A-BUNDLE', 'delivered_plots': ['P1']},
            award_doc('AW-RE', 'lowest_total')]
    db1, db2 = tmp_path / 'a.sqlite', tmp_path / 'b.sqlite'
    for i, doc in enumerate(docs):
        f1, f2 = write(tmp_path, f'a{i}.json', doc), write(tmp_path, f'b{i}.json', doc)
        if doc['kind'] == 'tender_bundle':
            import_tender_bundle(db1, f1)
            import_tender_bundle(db2, f2)
        elif doc['kind'] == 'tender_withdrawal':
            import_tender_withdrawal(db1, f1)
            import_tender_withdrawal(db2, f2)
        elif i == 3:
            reopen_gap_award(db1, f1)
            reopen_gap_award(db2, f2)
        else:
            import_tender_award(db1, f1)
            import_tender_award(db2, f2)
    assert report(db1) == report(db2)


def test_duplicate_page_inside_one_bundle_and_award_id_conflict(tmp_path):
    db = tmp_path / 't.sqlite'
    dup = load_bundle()
    dup['id'] = 'tb-inner-dup'
    dup['pages'].append(dict(dup['pages'][0]))
    with pytest.raises(ValueError, match='duplicate_page_snapshot'):
        capture(tmp_path, db, dup, 'dup.json')
    db2 = tmp_path / 't2.sqlite'
    capture(tmp_path, db2, load_bundle())
    import_tender_award(db2, write(tmp_path, 'aw1.json', award_doc('AW1', 'lowest_total')))
    # Same award_id while the first award is still active is refused upstream.
    with pytest.raises(ValueError, match='active_award_already_exists'):
        import_tender_award(db2, write(tmp_path, 'aw2.json',
                                       dict(award_doc('AW1', 'lowest_total'), id='aw-other')))
    # After full breach the id still may not be recycled.
    import_tender_withdrawal(db2, write(tmp_path, 'wd.json', {
        'id': 'wd1', 'kind': 'tender_withdrawal', 'award_id': 'AW1',
        'package_id': 'PKG-A-BUNDLE', 'delivered_plots': ['P1']}))
    with pytest.raises(ValueError, match='award_id_conflict'):
        import_tender_award(db2, write(tmp_path, 'aw3.json',
                                       dict(award_doc('AW1', 'lowest_total'), id='aw-third')))


def test_same_truck_in_non_overlapping_windows_is_allowed(tmp_path):
    db = tmp_path / 't.sqlite'
    doc = load_bundle()
    for page in doc['pages']:
        for bid in page['bids']:
            for v in bid['versions']:
                if bid['bid_id'] in ('BID-B', 'BID-C'):
                    v['truck_id'] = 'truck-shared'
                    if bid['bid_id'] == 'BID-C':
                        v['tide_window_start'] = '2026-04-02T08:00:00Z'
                        v['tide_window_end'] = '2026-04-02T12:00:00Z'
    capture(tmp_path, db, doc)
    r = import_tender_award(db, write(tmp_path, 'aw.json', award_doc('AW-DIV', 'provenance_diversity')))
    assert sorted(r['packages']) == ['PKG-B-P1', 'PKG-C-P2']
    assert audit(db)['status'] == 'matched'


def test_arrival_against_unknown_award_package_is_rejected(tmp_path):
    db = tmp_path / 't.sqlite'
    capture(tmp_path, db, load_bundle())
    with pytest.raises(ValueError, match='unknown_awarded_package'):
        import_material(db, write(tmp_path, 'arr.json', material_doc(
            'MX', award_id='NOPE', package_id='PKG-A-BUNDLE')))
    assert report(db)['materials'] == []

