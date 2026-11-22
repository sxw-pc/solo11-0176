"""Independent coordinate rechecks and reproducible baseline adjudication.

A later baseline file can only *open* a coordinate dispute.  Resolving one
requires corroborating tracks from two independent institutions; neither a
newest-import nor a more favourable survival score is ever sufficient.
"""
import hashlib,json
from collections import Counter
from contextlib import closing
from .store import (connect,evidence,log,snapshot_acceptance,update_acceptance)

AUTHORIZATIONS=('third_party_surveyor','ecological_monitor')

def _track_coordinate(doc):
    points=doc.get('track') or []
    if not points:return None,'invalid_track'
    for p in points:
        if not isinstance(p.get('lat'),(int,float)) or not isinstance(p.get('lon'),(int,float)):
            return None,'invalid_track'
    if any(p.get('outside_tide_window') for p in points):return None,'outside_tide_window'
    labels=[p.get('coordinate') for p in points if p.get('coordinate')]
    if not labels:return None,'invalid_track'
    counted=Counter(labels).most_common()
    if len(counted)>1 and counted[0][1]==counted[1][1]:return None,'ambiguous_track'
    return counted[0][0],'ok'

def check_recheck(doc):
    """Pure validation used by import, replay and tests."""
    if doc.get('tier') not in ('high','mid','low'):raise ValueError('recheck_tier')
    if doc.get('authorization') not in AUTHORIZATIONS:raise ValueError('recheck_authorization')
    if not doc.get('agency') or not doc.get('supported_coordinate'):raise ValueError('recheck_identity')
    if doc['agency']==doc.get('contractor'):raise ValueError('recheck_not_independent')
    computed,reason=_track_coordinate(doc)
    valid=False
    if not doc.get('clock_synced'):
        reason='clock_missing'
    elif reason=='ok' and computed!=doc['supported_coordinate']:
        reason='track_does_not_support_claim'
    elif reason=='ok':
        valid=True
    return computed,valid,reason

def _fingerprint(doc):
    body={k:v for k,v in doc.items() if k!='id'}
    return hashlib.sha256(json.dumps(body,sort_keys=True,separators=(',',':')).encode()).hexdigest()

def import_recheck(path,file):
    with closing(connect(path)) as db,db:
        doc,fresh=evidence(db,file,'recheck')
        if not fresh:return {'id':doc['id'],'replayed':True}
        fingerprint=_fingerprint(doc)
        duplicate=db.execute('SELECT id FROM recheck WHERE sha=?',(fingerprint,)).fetchone()
        if duplicate:raise ValueError('recheck_duplicate_evidence')
        if not db.execute('SELECT 1 FROM plot WHERE id=?',(doc['plot_id'],)).fetchone():
            raise ValueError('unknown_plot')
        plot=db.execute('SELECT * FROM plot WHERE id=?',(doc['plot_id'],)).fetchone()
        if doc.get('season') and doc['season']!=plot['season']:raise ValueError('season_not_comparable')
        contract=db.execute('SELECT * FROM work_contract WHERE plot_id=?',(doc['plot_id'],)).fetchone()
        if contract:doc=dict(doc,contractor=contract['contractor'])
        # A track may name a coordinate from a baseline revision that has not
        # arrived yet; it is recorded now and adjudicated when the dispute opens.
        computed,valid,reason=check_recheck(doc)
        db.execute('INSERT INTO recheck VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',
                   (doc['id'],doc['plot_id'],doc['tier'],doc['agency'],doc['authorization'],
                    doc['supported_coordinate'],computed,int(bool(doc.get('clock_synced'))),
                    int(valid),reason,fingerprint,doc['id']))
        log(db,doc['id'],'accepted' if valid else 'rejected',reason,doc['id'])
        result={'id':doc['id'],'tier':doc['tier'],'valid':valid,'reason':reason}
        decision=adjudicate(db,doc['plot_id'])
        if decision:result['decisions']=decision
        return result

def _original_and_disputed(db,plot_id,tier):
    original=db.execute('SELECT brt.* FROM baseline_revision_transect brt JOIN baseline_revision br ON br.id=brt.revision_id '
                        'WHERE br.plot_id=? AND br.revision=1 AND brt.tier=?',(plot_id,tier)).fetchone()
    disputed=db.execute('SELECT * FROM baseline_dispute WHERE plot_id=? AND id LIKE ? ORDER BY rowid DESC LIMIT 1',
                        (plot_id,'%-'+tier)).fetchone()
    return original,disputed

def _valid_supports(db,plot_id,tier):
    return [dict(r) for r in db.execute('SELECT * FROM recheck WHERE plot_id=? AND tier=? AND valid=1 ORDER BY rowid',(plot_id,tier))]

def _invalid_reasons(db,plot_id,tier):
    return [r['reason'] for r in db.execute('SELECT reason FROM recheck WHERE plot_id=? AND tier=? AND valid=0 ORDER BY rowid',(plot_id,tier))]

def _record(db,plot_id,tier,decision,coordinate,missing,reason,source_id,evidence_id=None):
    version=db.execute('SELECT COALESCE(MAX(version),0)+1 FROM dispute_decision WHERE plot_id=? AND tier=?',(plot_id,tier)).fetchone()[0]
    db.execute('INSERT INTO dispute_decision(plot_id,tier,version,status,decision,coordinate,evidence_id,missing,reason,source_id) '
               'VALUES(?,?,?,?,?,?,?,?,?,?)',
               (plot_id,tier,version,decision['status'],decision['decision'],coordinate,evidence_id,
                json.dumps(sorted(missing)),reason,source_id))
    db.execute('UPDATE dispute_state SET status=? WHERE plot_id=? AND tier=?',
               ('resolved' if decision['status']=='resolved' else 'disputed',plot_id,tier))
    return version

def _spatial_checks(db,plot_id,tier,coordinate,source_id):
    # Construction cells and surveys only cover the coordinate they recorded.
    for cell in db.execute('SELECT * FROM work_cell WHERE tier=? AND contract_id IN '
                           '(SELECT id FROM work_contract WHERE plot_id=?) ORDER BY id',(tier,plot_id)):
        db.execute('INSERT INTO spatial_check(plot_id,tier,object_type,object_id,expected_coordinate,actual_coordinate,covered,source_id) '
                   'VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(plot_id,tier,object_type,object_id) DO UPDATE SET '
                   'expected_coordinate=excluded.expected_coordinate,actual_coordinate=excluded.actual_coordinate,'
                   'covered=excluded.covered,source_id=excluded.source_id',
                   (plot_id,tier,'work_cell',cell['id'],coordinate,cell['coordinate'],
                    int(cell['coordinate']==coordinate),source_id))
    for s in db.execute('SELECT * FROM survey WHERE plot_id=? ORDER BY id',(plot_id,)):
        actual=s[tier+'_coord']
        db.execute('INSERT INTO spatial_check(plot_id,tier,object_type,object_id,expected_coordinate,actual_coordinate,covered,source_id) '
                   'VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(plot_id,tier,object_type,object_id) DO UPDATE SET '
                   'expected_coordinate=excluded.expected_coordinate,actual_coordinate=excluded.actual_coordinate,'
                   'covered=excluded.covered,source_id=excluded.source_id',
                   (plot_id,tier,'survey',s['id'],coordinate,actual,int(actual==coordinate),source_id))

def _disputed_revision(db,plot_id):
    return db.execute('SELECT * FROM baseline_revision WHERE plot_id=? AND status=? ORDER BY revision DESC LIMIT 1',
                      (plot_id,'disputed')).fetchone()

def _resolve_original(db,plot_id,tier,source_id,original):
    db.execute("UPDATE baseline_revision SET status='adopted' WHERE plot_id=? AND revision=1",(plot_id,))
    db.execute("UPDATE baseline_revision SET status='rejected' WHERE plot_id=? AND revision>1 AND status='disputed'",(plot_id,))
    # Restore this tier's original coordinate if an earlier ruling rewrote it.
    db.execute("UPDATE baseline_transect SET coordinate=?,source_id=? WHERE plot_id=? AND tier=?",
               (original['coordinate'],db.execute('SELECT evidence_id FROM baseline_revision WHERE plot_id=? AND revision=1',(plot_id,)).fetchone()['evidence_id'],plot_id,tier))
    # Each coordinate tier is independently disputed; the restoration is only
    # confirmed once no tier remains under dispute.
    unresolved=db.execute("SELECT 1 FROM dispute_state WHERE plot_id=? AND status='disputed'",(plot_id,)).fetchone()
    previous=db.execute('SELECT * FROM acceptance WHERE plot_id=?',(plot_id,)).fetchone()
    frozen=db.execute('SELECT * FROM acceptance_snapshot WHERE plot_id=?',(plot_id,)).fetchone()
    if unresolved:
        # Another tier is still disputed: keep the plot frozen and record that
        # this tier's original coordinate was upheld.
        update_acceptance(db,plot_id,'rework',previous['weighted_survival'] if previous else 0,
                          'baseline_partial_tier_upheld',source_id,create=False)
        acceptance_status='frozen'
    elif frozen:
        # Restore the decision already reached on the original samples; the
        # original survey is evidence for this restoration, not a new round.
        update_acceptance(db,plot_id,frozen['status'],frozen['score'],'baseline_original_upheld',source_id)
        acceptance_status=frozen['status']
    else:
        acceptance_status='rework'
    if not unresolved:
        db.execute("UPDATE plot SET baseline_status='confirmed' WHERE id=?",(plot_id,))
    _spatial_checks(db,plot_id,tier,original['coordinate'],source_id)
    return acceptance_status

def _resolve_disputed(db,plot_id,tier,source_id,disputed):
    revision=_disputed_revision(db,plot_id)
    if revision:
        db.execute("UPDATE baseline_revision SET status='superseded' WHERE plot_id=? AND revision< ? AND status IN ('original','adopted')",
                   (plot_id,revision['revision']))
        db.execute("UPDATE baseline_revision SET status='adopted' WHERE id=?",(revision['id'],))
    db.execute("UPDATE baseline_transect SET coordinate=?,source_id=? WHERE plot_id=? AND tier=?",
               (disputed['coordinate'],revision['evidence_id'] if revision else source_id,plot_id,tier))
    unresolved=db.execute("SELECT 1 FROM dispute_state WHERE plot_id=? AND status='disputed'",(plot_id,)).fetchone()
    # Do not carry the old score into an acceptance for uninspected coordinates.
    if unresolved:
        current=db.execute('SELECT weighted_survival FROM acceptance WHERE plot_id=?',(plot_id,)).fetchone()
        update_acceptance(db,plot_id,'rework',current['weighted_survival'] if current else 0,
                          'baseline_partial_tier_revised',source_id,create=False)
    else:
        update_acceptance(db,plot_id,'rework',0,'baseline_coordinate_revised_recheck_required',source_id)
        db.execute("UPDATE plot SET baseline_status='recheck_required' WHERE id=?",(plot_id,))
    _spatial_checks(db,plot_id,tier,disputed['coordinate'],source_id)
    return 'rework'

def _apply(db,plot_id,tier,decision,candidate,source_id,original,disputed):
    """Apply a recorded ruling.  Repeated application is idempotent."""
    if decision['decision']=='original_upheld':
        return _resolve_original(db,plot_id,tier,source_id,original)
    if decision['decision']=='revised_adopted':
        return _resolve_disputed(db,plot_id,tier,source_id,disputed)
    if decision['status']=='frozen':
        # Evidence that cannot settle the coordinate freezes the plot again,
        # even if an earlier ruling had restored it.  A previously adopted
        # revision returns to disputed; the ruling and history stay intact.
        revision=_disputed_revision(db,plot_id)
        if revision:
            db.execute("UPDATE baseline_revision SET status='disputed' WHERE id=?",(revision['id'],))
        unresolved=db.execute("SELECT * FROM dispute_state WHERE plot_id=? AND status='disputed'",(plot_id,)).fetchone()
        if unresolved and db.execute('SELECT 1 FROM acceptance_snapshot WHERE plot_id=?',(plot_id,)).fetchone() is None:
            snapshot_acceptance(db,plot_id,source_id)
        db.execute("UPDATE plot SET baseline_status='frozen' WHERE id=?",(plot_id,))
        current=db.execute('SELECT * FROM acceptance WHERE plot_id=?',(plot_id,)).fetchone()
        if current and current['status']=='accepted':
            update_acceptance(db,plot_id,'rework',current['weighted_survival'],
                              'baseline_coordinate_conflict',source_id,create=False)
        return 'frozen'
    return None

def adjudicate(db,plot_id):
    """Recompute every disputed tier from stored rechecks.

    Decisions are a pure function of stored evidence: replaying the same
    imports in any order yields the same versions.  A repeated call that does
    not change the decision writes nothing.
    """
    states=list(db.execute("SELECT * FROM dispute_state WHERE plot_id=? ORDER BY tier",(plot_id,)))
    decisions=[]
    for state in states:
        tier=state['tier']
        original,disputed=_original_and_disputed(db,plot_id,tier)
        if not original or not disputed:continue
        valid=_valid_supports(db,plot_id,tier)
        by_role={}
        for r in valid:by_role.setdefault(r['authorization'],set()).add(r['supported_coordinate'])
        source_id=(valid[-1]['id'] if valid else disputed['source_id'])
        evidence_id=(valid[-1]['id'] if valid else None)
        missing=[]
        candidate=None
        reviewed_tiers={r['tier'] for r in db.execute("SELECT DISTINCT tier FROM recheck WHERE plot_id=? AND valid=1",(plot_id,))}
        if not valid:
            reasons=_invalid_reasons(db,plot_id,tier)
            if 'clock_missing' in reasons:missing.append('clock_synchronized_original_track')
            if 'outside_tide_window' in reasons:missing.append('in_tide_window_track')
            if 'ambiguous_track' in reasons or 'invalid_track' in reasons:missing.append('unambiguous_gps_track')
            # Tracks that only cover mid/low do not speak to a high-coordinate dispute.
            if reviewed_tiers-{tier}:
                missing.append('recheck_covering_'+tier+'_tier')
            if not missing:missing.append('valid_independent_track')
            decision={'status':'frozen','decision':'insufficient_evidence'}
        else:
            # Unclocked evidence cannot be ignored just because a clocked file is
            # more convenient: an unresolved clock gap on this tier keeps it frozen.
            unclocked=list(db.execute('SELECT * FROM recheck WHERE plot_id=? AND tier=? AND valid=0 AND reason=? ORDER BY rowid',
                                      (plot_id,tier,'clock_missing')))
            if unclocked:
                missing.append('clock_synchronized_track_resolving_conflict')
                decision={'status':'frozen','decision':'conflicting_evidence'}
            elif reviewed_tiers-{tier}:
                missing.append('recheck_covering_'+tier+'_tier')
                decision={'status':'frozen','decision':'partial_coverage'}
            elif len(by_role)<2:
                missing.append('independent_second_institution')
                decision={'status':'frozen','decision':'single_source'}
            elif any(len(v)>1 for v in by_role.values()):
                missing.append('agencies_must_agree')
                decision={'status':'frozen','decision':'conflicting_agencies'}
            else:
                coords={next(iter(v)) for v in by_role.values()}
                if len(coords)>1:
                    missing.append('agencies_must_agree')
                    decision={'status':'frozen','decision':'conflicting_agencies'}
                else:
                    candidate=next(iter(coords))
                    if candidate==original['coordinate']:
                        decision={'status':'resolved','decision':'original_upheld'}
                    elif candidate==disputed['coordinate']:
                        decision={'status':'resolved','decision':'revised_adopted'}
                    else:
                        missing.append('coordinate_matching_a_recorded_revision')
                        decision={'status':'frozen','decision':'unsupported_coordinate'}
        prior=db.execute('SELECT * FROM dispute_decision WHERE plot_id=? AND tier=? ORDER BY version DESC LIMIT 1',
                         (plot_id,tier)).fetchone()
        signature=(decision['status'],decision['decision'],candidate,sorted(missing),evidence_id)
        if prior and signature==(prior['status'],prior['decision'],prior['coordinate'],json.loads(prior['missing']),prior['evidence_id']):
            continue
        version=_record(db,plot_id,tier,decision,candidate,missing,
                        decision['decision'],source_id,evidence_id)
        acceptance_status=_apply(db,plot_id,tier,decision,candidate,source_id,original,disputed)
        log(db,plot_id,decision['status'],decision['decision'],source_id)
        decisions.append({'tier':tier,'version':version,'decision':decision['decision'],
                          'status':decision['status'],'coordinate':candidate,
                          'missing':sorted(missing),'acceptance_status':acceptance_status})
    return decisions
