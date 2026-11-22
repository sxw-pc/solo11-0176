"""Offline tender portal capture, eligibility screening and rule-based awarding.

A bid award creates an *executable contract projection* only. It never writes a
material_batch: usable nursery stock exists solely after real arrival evidence
passes material.import_material. Award rows are versioned and immutable; a
post-award withdrawal opens re-procurement gaps instead of rewriting history.
"""
import json
from contextlib import closing
from .store import connect, evidence, log

NATIVE_SPECIES = ('native-grass', 'native-shell')
RULES = ('lowest_total', 'provenance_diversity')


def _overlap(a_start, a_end, b_start, b_end):
    return a_start <= b_end and b_start <= a_end


def import_tender_bundle(path, file):
    """Ingest one portal snapshot bundle: page closure check plus bid screening.

    Pages may arrive in any order. Missing pages, a watermark before the
    submission deadline, or duplicate snapshots with different payloads leave
    the capture explicitly *not closed*; an absent page is never treated as an
    absence of bids. Later bundles may complete an earlier partial capture.
    """
    with closing(connect(path)) as db, db:
        doc, fresh = evidence(db, file, 'tender_bundle')
        if not fresh:
            return {'id': doc['id'], 'replayed': True}
        tender_id = doc['tender_id']
        demand = doc['demand']
        demand_plots = {x['plot_id'] for x in demand}
        if not demand_plots or any(x.get('quantity', 0) <= 0 for x in demand):
            raise ValueError('invalid_tender_demand')
        duplicate_conflict = False
        seen_pages = set()
        doc_keys = set()
        for page in doc['pages']:
            no = page['page_no']
            snap = page['snapshot_no']
            if not isinstance(no, int) or no < 1 or not isinstance(snap, int) or snap < 1:
                raise ValueError('invalid_page_numbering')
            if (no, snap) in doc_keys:
                raise ValueError('duplicate_page_snapshot')
            doc_keys.add((no, snap))
            if no in seen_pages:
                # Same logical page re-shoot inside one bundle download.
                duplicate_conflict = True
            seen_pages.add(no)
            stored = db.execute(
                'SELECT 1 FROM tender_page WHERE tender_id=? AND page_no=? AND snapshot_no=?',
                (tender_id, no, snap)).fetchone()
            if stored:
                continue
            earlier = db.execute(
                'SELECT 1 FROM tender_page WHERE tender_id=? AND page_no=?',
                (tender_id, no)).fetchone()
            if earlier:
                # Same logical page seen again under a different snapshot whose
                # payload differs: the portal reordered/re-shot the page.
                duplicate_conflict = True
            db.execute('INSERT INTO tender_page VALUES(?,?,?,?,?)',
                       (tender_id, no, snap, page['fetched_at'], doc['id']))
            for bid in page.get('bids', []):
                _ingest_bid(db, doc['id'], tender_id, bid, demand_plots)
        expected = doc['expected_pages']
        page_nos = {r['page_no'] for r in db.execute(
            'SELECT DISTINCT page_no FROM tender_page WHERE tender_id=?', (tender_id,))}
        missing = sorted(set(range(1, expected + 1)) - page_nos)
        # A logical page seen under more than one snapshot number (portal
        # re-shoot/reorder), or a snapshot chain that does not start at 1, is a
        # duplicate-snapshot conflict even when it happens inside one bundle.
        snapshots = {}
        for r in db.execute(
                'SELECT page_no,snapshot_no FROM tender_page WHERE tender_id=?', (tender_id,)):
            snapshots.setdefault(r['page_no'], set()).add(r['snapshot_no'])
        duplicate_conflict = any(len(snaps) > 1 or min(snaps) != 1
                                 for snaps in snapshots.values())
        before_deadline = doc['captured_at'] < doc['deadline']
        reasons = []
        if missing:
            reasons.append('missing_pages:' + ','.join(map(str, missing)))
        if before_deadline:
            reasons.append('capture_before_deadline')
        if duplicate_conflict:
            reasons.append('duplicate_snapshot_conflict')
        closed = not reasons
        db.execute(
            '''INSERT INTO tender_capture VALUES(?,?,?,?,?,?,?)
               ON CONFLICT(tender_id) DO UPDATE SET deadline=excluded.deadline,
               expected_pages=excluded.expected_pages,demand_json=excluded.demand_json,
               closed=excluded.closed,closure_reason=excluded.closure_reason,source_id=excluded.source_id''',
            (tender_id, doc['deadline'], expected, json.dumps(demand, sort_keys=True),
             1 if closed else 0, 'closed' if closed else '+'.join(reasons), doc['id']))
        log(db, tender_id, 'closed' if closed else 'capture_not_closed',
            'closed' if closed else '+'.join(reasons), doc['id'])
        return {'tender_id': tender_id,
                'status': 'closed' if closed else 'capture_not_closed',
                'reason': 'closed' if closed else '+'.join(reasons)}


def _ingest_bid(db, source_id, tender_id, bid, demand_plots):
    versions = bid['versions']
    if not versions or any(v['version'] < 1 for v in versions):
        raise ValueError('invalid_bid_versions')
    base_keys = ('bid_id', 'supplier_id', 'nursery_id', 'species', 'origin')
    for v in versions:
        ident = {k: bid[k] for k in base_keys}
        if any(not ident[k] for k in base_keys):
            raise ValueError('invalid_bid_identity')
        plots = v['plots']
        if (not plots or any(p['plot_id'] not in demand_plots or p['quantity'] <= 0 for p in plots)
                or len({p['plot_id'] for p in plots}) != len(plots)):
            raise ValueError('invalid_bid_plots')
        if v['price'] <= 0 or v['tide_window_start'] >= v['tide_window_end']:
            raise ValueError('invalid_bid_terms')
        existing = db.execute(
            'SELECT source_id FROM tender_bid WHERE bid_id=? AND version=?',
            (bid['bid_id'], v['version'])).fetchone()
        if existing:
            continue
        withdrawn = 1 if v.get('withdrawn', False) else 0
        eligible, reason = _screen(bid, v, plots, demand_plots, withdrawn)
        db.execute(
            '''INSERT INTO tender_bid VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
            (bid['bid_id'], v['version'], tender_id, bid['supplier_id'], bid['nursery_id'],
             bid['species'], bid['origin'], json.dumps(plots, sort_keys=True),
             v['package_id'], 1 if v.get('is_bundle', False) else 0, v['price'],
             v['tide_window_start'], v['tide_window_end'], v.get('truck_id'),
             v.get('cold_chain', 'none'), v.get('provenance_doc', ''), withdrawn,
             1 if eligible else 0, reason, source_id))
        log(db, bid['bid_id'] + ':v' + str(v['version']),
            'eligible' if eligible else 'screened_out', reason or 'eligible', source_id)


def _screen(bid, version, plots, demand_plots, withdrawn):
    """Initial screen: non-local or non-native bids keep the original + reason."""
    if withdrawn:
        return False, 'withdrawn_version'
    if bid['species'] not in NATIVE_SPECIES:
        return False, 'non_native_species'
    if bid['origin'] != 'local':
        return False, 'non_local_origin'
    if not version.get('provenance_doc'):
        return False, 'missing_provenance'
    return True, None


def _latest_bids(db, tender_id):
    rows = db.execute('SELECT * FROM tender_bid WHERE tender_id=?', (tender_id,)).fetchall()
    latest = {}
    for r in rows:
        if r['bid_id'] not in latest or r['version'] > latest[r['bid_id']]['version']:
            latest[r['bid_id']] = r
    return latest


def _packages(db, tender_id):
    """One executable package per still-eligible latest bid version.

    A bid whose awarded package was already breached by a post-award
    withdrawal is disqualified from later re-procurement: the old winner
    cannot be re-awarded the gap it created.
    """
    out = []
    for r in _latest_bids(db, tender_id).values():
        if not r['eligible']:
            continue
        breached = db.execute(
            "SELECT 1 FROM tender_award_item WHERE bid_id=? AND version=? AND status='breached'",
            (r['bid_id'], r['version'])).fetchone()
        if breached:
            continue
        plots = {x['plot_id']: x['quantity'] for x in json.loads(r['plots_json'])}
        out.append({'bid_id': r['bid_id'], 'version': r['version'],
                    'package_id': r['package_id'], 'is_bundle': bool(r['is_bundle']),
                    'supplier_id': r['supplier_id'], 'nursery_id': r['nursery_id'],
                    'species': r['species'], 'origin': r['origin'], 'plots': plots,
                    'price': r['price'], 'truck_id': r['truck_id'],
                    'window': (r['tide_window_start'], r['tide_window_end']),
                    'cold_chain': r['cold_chain'],
                    'delivery_commitment': r['tide_window_start'] + '/' + r['tide_window_end'],
                    'liability': {'withdrawal_breach': True,
                                  'terms': 'withdrawal_after_award_is_breach_and_reprocurement_gap',
                                  'provenance_doc': r['provenance_doc']}})
    return out


def _feasible(remaining, chosen, packages):
    if not remaining:
        # Every demanded plot was filled to its exact quantity.
        return [list(chosen)]
    target = next(plot_id for plot_id in sorted(remaining))
    results = []
    for idx, pkg in enumerate(packages):
        if target not in pkg['plots']:
            continue
        # A package is atomic: every plot it covers must still need exactly its
        # quantity, otherwise taking it would over- or under-supply a plot.
        if any(remaining.get(p, 0) != q for p, q in pkg['plots'].items()):
            continue
        # One cold-chain truck cannot carry two winning packages in the same
        # tide window; overlapping windows on the same truck are mutually exclusive.
        clash = any(pkg['truck_id'] and pkg['truck_id'] == other['truck_id']
                    and _overlap(pkg['window'][0], pkg['window'][1],
                                 other['window'][0], other['window'][1])
                    for other in chosen)
        if clash:
            continue
        next_remaining = dict(remaining)
        for p, q in pkg['plots'].items():
            next_remaining[p] -= q
            if next_remaining[p] == 0:
                del next_remaining[p]
        results.extend(_feasible(next_remaining, chosen + [pkg], packages[idx + 1:]))
    return results


def _choose(combos, rule, tender_id):
    if not combos:
        raise ValueError('no_feasible_award')
    if rule == 'lowest_total':
        combos.sort(key=lambda c: (sum(p['price'] for p in c),
                                   '|'.join(sorted(p['package_id'] for p in c))))
    elif rule == 'provenance_diversity':
        combos = [c for c in combos
                  if len({p['nursery_id'] for p in c}) >= 2]
        if not combos:
            raise ValueError('diversity_rule_unavailable')
        combos.sort(key=lambda c: (sum(p['price'] for p in c),
                                   '|'.join(sorted(p['package_id'] for p in c))))
    else:
        raise ValueError('unknown_award_rule')
    return combos[0]


def import_tender_award(path, file):
    """Compute and freeze an award under one pre-published rule.

    The caller supplies the rule and an award id, never the winner list: the
    winner set is recomputed from the closed portal snapshot so page order and
    per-plot greedy unit prices cannot influence the contract.
    """
    with closing(connect(path)) as db, db:
        doc, fresh = evidence(db, file, 'tender_award')
        if not fresh:
            return {'id': doc['id'], 'replayed': True}
        tender_id = doc['tender_id']
        rule = doc['rule']
        if rule not in RULES:
            raise ValueError('unknown_award_rule')
        capture = db.execute('SELECT * FROM tender_capture WHERE tender_id=?',
                             (tender_id,)).fetchone()
        if not capture:
            raise ValueError('unknown_tender')
        if not capture['closed']:
            raise ValueError('capture_not_closed:' + capture['closure_reason'])
        if db.execute("SELECT 1 FROM tender_award WHERE tender_id=? AND status='active'",
                      (tender_id,)).fetchone():
            raise ValueError('active_award_already_exists')
        demand = {x['plot_id']: x['quantity'] for x in json.loads(capture['demand_json'])}
        packages = _packages(db, tender_id)
        wanted = dict(demand)
        combos = _feasible(dict(wanted), [], packages)
        winner = _choose(combos, rule, tender_id)
        award_id = doc['award_id']
        if db.execute('SELECT 1 FROM tender_award WHERE award_id=?', (award_id,)).fetchone():
            raise ValueError('award_id_conflict')
        version = db.execute(
            'SELECT COALESCE(MAX(version),0)+1 FROM tender_award WHERE tender_id=?',
            (tender_id,)).fetchone()[0]
        total = sum(p['price'] for p in winner)
        db.execute('INSERT INTO tender_award VALUES(?,?,?,?,?,?,?,?)',
                   (award_id, tender_id, rule, 'active', total, version, 0, doc['id']))
        for pkg in winner:
            for plot_id, qty in sorted(pkg['plots'].items()):
                db.execute(
                    'INSERT INTO tender_award_item VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                    (award_id, pkg['package_id'], pkg['bid_id'], pkg['version'],
                     pkg['supplier_id'], pkg['nursery_id'], plot_id, qty, pkg['price'],
                     pkg['truck_id'], pkg['window'][0], pkg['window'][1],
                     pkg['delivery_commitment'], json.dumps(pkg['liability'], sort_keys=True),
                     'active', doc['id']))
        log(db, award_id, 'active', rule + ':total_' + str(total), doc['id'])
        return {'award_id': award_id, 'status': 'active', 'rule': rule,
                'total_price': total, 'version': version,
                'packages': sorted(p['package_id'] for p in winner)}


def import_tender_withdrawal(path, file):
    """Record a post-award supplier withdrawal as breach + re-procurement gaps.

    The old winning version is never rewritten. A bundle package is atomic:
    partial delivery cannot be split into partial performance, so every plot
    the package still owes becomes an open gap.
    """
    with closing(connect(path)) as db, db:
        doc, fresh = evidence(db, file, 'tender_withdrawal')
        if not fresh:
            return {'id': doc['id'], 'replayed': True}
        award_id = doc['award_id']
        award = db.execute('SELECT * FROM tender_award WHERE award_id=?',
                           (award_id,)).fetchone()
        if not award:
            raise ValueError('unknown_award')
        if award['status'] != 'active':
            raise ValueError('award_not_active')
        if doc.get('package_id'):
            items = db.execute(
                'SELECT * FROM tender_award_item WHERE award_id=? AND package_id=?',
                (award_id, doc['package_id'])).fetchall()
        else:
            items = db.execute(
                'SELECT * FROM tender_award_item WHERE award_id=?', (award_id,)).fetchall()
        items = [r for r in items if r['status'] == 'active']
        if not items:
            raise ValueError('withdrawal_without_active_item')
        delivered = set(doc.get('delivered_plots', []))
        package_ids = sorted({r['package_id'] for r in items})
        # A package spanning multiple plots is a non-severable bundle: a claim
        # of partial delivery does not count as partial performance, so every
        # covered plot becomes a re-procurement gap.
        bundle_packages = {pid for pid in package_ids
                           if sum(1 for r in items if r['package_id'] == pid) > 1}
        for pid in package_ids:
            db.execute(
                "UPDATE tender_award_item SET status='breached',source_id=? "
                "WHERE award_id=? AND package_id=? AND status='active'",
                (doc['id'], award_id, pid))
            log(db, award_id + ':' + pid, 'breached', 'supplier_withdrawal', doc['id'])
        gaps = []
        for r in items:
            if r['plot_id'] in delivered and r['package_id'] not in bundle_packages:
                continue
            gap_id = award_id + '-gap-' + r['plot_id']
            if db.execute('SELECT 1 FROM tender_gap WHERE gap_id=?', (gap_id,)).fetchone():
                continue
            if r['package_id'] in bundle_packages and delivered:
                reason = 'partial_bundle_delivery_not_severable'
            else:
                reason = 'supplier_withdrawal'
            db.execute('INSERT INTO tender_gap VALUES(?,?,?,?,?,?,?,?)',
                       (gap_id, award['tender_id'], award_id, r['plot_id'],
                        r['quantity'], reason, 'open', doc['id']))
            gaps.append({'plot_id': r['plot_id'], 'quantity': r['quantity'], 'reason': reason})
        still_active = db.execute(
            'SELECT 1 FROM tender_award_item WHERE award_id=? AND status=?',
            (award_id, 'active')).fetchone()
        if not still_active:
            db.execute("UPDATE tender_award SET status='breached' WHERE award_id=?",
                       (award_id,))
        log(db, award_id, 'breached' if not still_active else 'partial_breach',
            str(len(gaps)) + '_open_gaps', doc['id'])
        return {'award_id': award_id,
                'status': 'breached' if not still_active else 'partial_breach',
                'gaps': gaps}


def reopen_gap_award(path, file):
    """Re-award only the open gaps against bids still valid in the snapshot.

    Reopening is a fresh, verifiable award at current bid prices; the old
    winner's price is never reused. Uncoverable gaps stay open.
    """
    with closing(connect(path)) as db, db:
        doc, fresh = evidence(db, file, 'tender_award')
        if not fresh:
            return {'id': doc['id'], 'replayed': True}
        tender_id = doc['tender_id']
        rule = doc['rule']
        if rule not in RULES:
            raise ValueError('unknown_award_rule')
        capture = db.execute('SELECT * FROM tender_capture WHERE tender_id=?',
                             (tender_id,)).fetchone()
        if not capture or not capture['closed']:
            raise ValueError('capture_not_closed')
        open_gaps = db.execute(
            "SELECT * FROM tender_gap WHERE tender_id=? AND status='open'",
            (tender_id,)).fetchall()
        if not open_gaps:
            raise ValueError('no_open_gap')
        wanted = {}
        for g in open_gaps:
            wanted[g['plot_id']] = wanted.get(g['plot_id'], 0) + g['quantity']
        packages = _packages(db, tender_id)
        combos = _feasible(dict(wanted), [], packages)
        winner = _choose(combos, rule, tender_id)
        award_id = doc['award_id']
        if db.execute('SELECT 1 FROM tender_award WHERE award_id=?', (award_id,)).fetchone():
            raise ValueError('award_id_conflict')
        version = db.execute(
            'SELECT COALESCE(MAX(version),0)+1 FROM tender_award WHERE tender_id=?',
            (tender_id,)).fetchone()[0]
        total = sum(p['price'] for p in winner)
        db.execute('INSERT INTO tender_award VALUES(?,?,?,?,?,?,?,?)',
                   (award_id, tender_id, rule, 'active', total, version, 1, doc['id']))
        covered = set()
        for pkg in winner:
            for plot_id, qty in sorted(pkg['plots'].items()):
                db.execute(
                    'INSERT INTO tender_award_item VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                    (award_id, pkg['package_id'], pkg['bid_id'], pkg['version'],
                     pkg['supplier_id'], pkg['nursery_id'], plot_id, qty, pkg['price'],
                     pkg['truck_id'], pkg['window'][0], pkg['window'][1],
                     pkg['delivery_commitment'], json.dumps(pkg['liability'], sort_keys=True),
                     'active', doc['id']))
                covered.add(plot_id)
        for g in open_gaps:
            if g['plot_id'] in covered:
                db.execute(
                    "UPDATE tender_gap SET status='re-awarded' WHERE gap_id=?", (g['gap_id'],))
        log(db, award_id, 'active', 'reprocurement:total_' + str(total), doc['id'])
        return {'award_id': award_id, 'status': 'active', 'rule': rule,
                'total_price': total, 'version': version,
                'packages': sorted(p['package_id'] for p in winner),
                'remaining_open_gaps': [g['gap_id'] for g in open_gaps
                                        if g['plot_id'] not in covered]}
