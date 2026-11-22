"""Joint lane-level eDNA attribution acceptance tests.

Covers the five acceptance criteria:
  1. fixed-copy import/report/audit replay and missing-baseline rejection;
  2. numeric joint lower bounds for two plots, two blanks, positive control,
     ambiguous barcodes and the cross-talk upper-bound matrix;
  3. the same evidence drives two authorised pathways with different patrol /
     re-run lists, neither making a false confirmation;
  4. shared source totals, ambiguous barcodes and lane controls conserve reads;
  5. tampering a raw read or an attribution projection is located on replay.
"""
import json,subprocess,sys
from pathlib import Path
import pytest
from tidalrestore.baseline import import_baseline
from tidalrestore.edna import (import_sample,import_lane,solve_lane,
                               conservative_plan,probabilistic_plan,
                               naive_blank_subtraction)
from tidalrestore.store import connect,report
from tidalrestore.audit import audit

FIX=Path(__file__).resolve().parent.parent/'fixtures'


def _two_plots(tmp):
    db=tmp/'lane.sqlite'
    import_baseline(db,FIX/'baseline-ok.json')
    b1=json.loads((FIX/'baseline-ok.json').read_text())
    b2=dict(b1,id='b2',plot_id='P2',
            transects=[dict(t,id=t['id']+'-p2') for t in b1['transects']])
    p=tmp/'baseline-p2.json';p.write_text(json.dumps(b2))
    import_baseline(db,p)
    return db


def _lane_doc():
    return json.loads((FIX/'edna-lane.json').read_text())


# --------------------------------------------------------------------------- #
# Criterion 1: fixed-copy import / report / audit and missing-baseline reject
# --------------------------------------------------------------------------- #
def test_fixed_copy_import_report_audit_and_unknown_plot(tmp_path):
    db=tmp_path/'seed.sqlite'
    import_baseline(db,FIX/'baseline-ok.json')
    first=import_sample(db,FIX/'edna-ok.json')
    assert first['status']=='captured'
    # identical evidence is replayed, not duplicated
    assert import_sample(db,FIX/'edna-ok.json')['replayed']
    rows=report(db)['edna_samples']
    assert len(rows)==1
    row=rows[0]
    # only the five fixed-copy fields are captured, no species occupancy claim
    assert row['read_count']==36 and row['blank_reads']==2
    assert row['barcode']=='BIRD-A' and row['batch_id']=='LIB1'
    assert {'site','batch_id','barcode','read_count','blank_reads'} <= set(row)
    assert audit(db)['status']=='matched'
    # a lane sample referencing a plot that was never baselined is rejected
    bad=tmp_path/'no-plot.json'
    doc=_lane_doc();doc['wells'][0]['plot_id']='P9';doc['id']='lane-bad-plot'
    bad.write_text(json.dumps(doc))
    with pytest.raises(ValueError,match='unknown_plot'):
        import_lane(db,bad)


def test_lane_import_without_baseline_is_rejected(tmp_path):
    db=tmp_path/'lonely.sqlite'
    with pytest.raises(ValueError,match='unknown_plot'):
        import_lane(db,FIX/'edna-lane.json')


# --------------------------------------------------------------------------- #
# Criterion 2: numeric joint lower bounds
# --------------------------------------------------------------------------- #
def test_joint_lower_and_possible_ranges_have_numeric_assertions(tmp_path):
    db=_two_plots(tmp_path)
    result=import_lane(db,FIX/'edna-lane.json')
    p1,p2=result['plots']['P1'],result['plots']['P2']
    # P1's 36 invasive reads are never provable: every one could be cross-talk
    # or shared background, so the provable lower bound is zero
    assert p1['invasive_lower']==0
    assert 0 < p1['invasive_upper'] <= 36
    # the 12 indistinguishable reads stay unattributed, never an invasive call
    assert p1['unattributed_reads']==12
    # high-abundance P2 keeps a positive provable lower bound despite leakage
    assert p2['invasive_lower']>0
    assert p2['invasive_lower'] < p2['invasive_upper'] <= 120
    assert p2['unattributed_reads']==0
    # raw numbers are preserved verbatim, not relabelled as occupancy
    assert result['conservation']['observed_reads']==273
    stored={r['plot_id']:r for r in report(db)['edna_attribution']}
    assert stored['P1']['invasive_lower']==0
    assert stored['P2']['invasive_lower']==p2['invasive_lower']


def test_per_barcode_ranges_keep_ambiguous_reads_in_candidate_set(tmp_path):
    doc=_lane_doc()
    res=solve_lane(doc)
    p1=res['plots']['P1']
    # invasive barcode range never borrows the ambiguous 12 reads
    assert p1['local_bounds']['BIRD-A'][1] <= 36
    # ambiguous barcode remains a candidate interval, is never collapsed to a call
    assert p1['local_bounds']['BIRD-AMB']==[0,12]
    # the control amplicon can never be local at a sample
    assert p1['local_bounds']['CTL-B'][1]==0


# --------------------------------------------------------------------------- #
# Criterion 3: two pathways, same evidence, different lists, no false confirm
# --------------------------------------------------------------------------- #
def test_two_pathways_issue_different_lists_without_false_confirmation(tmp_path):
    db=_two_plots(tmp_path)
    import_lane(db,FIX/'edna-lane.json')
    cons=conservative_plan(db);prob=probabilistic_plan(db)
    # conservative: P1 cannot be confirmed; P2 (positive lower bound) is confirmed
    cons_plots={x['plot_id']:x for x in cons['confirmed_sites']}
    cons_pending={x['plot_id']:x for x in cons['unconfirmed_sites']}
    assert 'P1' not in cons_plots
    assert cons_pending['P1']['verdict']=='unconfirmed_priority_check'
    assert 'P2' in cons_plots
    # the unconfirmable lane must be re-sequenced; only P2 is publishable
    assert cons['rerun_lanes']==['L1']
    assert cons['field_visit_count']==1
    # probabilistic: P1 is a hot-spot to verify, explicitly never confirmed
    prob_hot={x['plot_id']:x for x in prob['hotspots']}
    assert prob_hot['P1']['verdict']=='hotspot_to_verify'
    assert 0.0 < prob_hot['P1']['presence_probability'] < 1.0
    prob_confirmed={x['plot_id'] for x in prob['confirmed_sites']}
    assert 'P1' not in prob_confirmed
    # both plots go on patrol, and no lane is blocked for re-run in this pathway
    assert prob['field_visit_count']==2
    assert prob['rerun_lanes']==[]
    # the pathways genuinely differ in attendance and certainty
    assert cons['field_visit_count'] != prob['field_visit_count']
    assert cons['rerun_lanes'] != prob['rerun_lanes']


def test_naive_per_sample_blank_subtraction_fabricates_confirmation(tmp_path):
    db=_two_plots(tmp_path)
    import_lane(db,FIX/'edna-lane.json')
    naive=naive_blank_subtraction(db)
    # the deliberately wrong method confirms both plots, exposing why a fixed
    # per-sample subtraction violates single-lane cross-talk conservation
    assert {c['plot_id'] for c in naive['calls'] if c['naive_call']=='confirmed_positive'}=={'P1','P2'}
    # while the conservative joint model refuses to confirm P1
    cons=conservative_plan(db)
    assert all(x['plot_id']!='P1' for x in cons['confirmed_sites'])


# --------------------------------------------------------------------------- #
# Criterion 4: read conservation under shared source totals and lane controls
# --------------------------------------------------------------------------- #
def test_reads_are_conserved_and_never_double_counted(tmp_path):
    doc=_lane_doc()
    res=solve_lane(doc)
    w=res['witness']
    # every non-blank destination's observed reads equal its inflow exactly
    inflow={}
    for k,v in w.items():
        if not v:continue
        if k[0]=='local':inflow[(k[1],k[2])]=inflow.get((k[1],k[2]),0)+v
        elif k[0]=='xtalkout':inflow[(k[2],k[3])]=inflow.get((k[2],k[3]),0)+v
        elif k[0]=='bg':inflow[(k[1],k[2])]=inflow.get((k[1],k[2]),0)+v
    for well in doc['wells']:
        if 'blank' in well['role']:continue
        for bc,n in well['reads'].items():
            assert inflow.get((well['well_id'],bc),0)==n
    # cross-talk from P2 never exceeds the measured 0-40 matrix bound
    e2_to_e1=sum(v for k,v in w.items()
                 if k[0]=='xtalk' and k[1]=='E2' and k[2]=='E1')
    assert e2_to_e1 <= 40
    # background is bounded jointly by the two blank controls (2 + 3)
    assert res['conservation']['background_cap']==5
    assert sum(v for k,v in w.items() if k[0]=='bg') <= 5
    # positive-control calibration is derived from observed control leakage
    assert res['calibration']['control_well']=='CTL'
    assert res['calibration']['leaked_reads']==5
    assert res['calibration']['cross_talk_rate']==0.05


def test_shared_cross_talk_capacity_spans_all_barcodes(tmp_path):
    doc=_lane_doc()
    # tightening the P2->P1 matrix bound to zero cannot be satisfied while
    # pretending all 36 P1 reads are local if external supply is needed
    tightened=json.loads(json.dumps(doc))
    tightened['cross_talk_upper']['E2']['E1']=0
    tightened['id']='lane-tight';tightened['lane_id']='Ltight'
    res=solve_lane(tightened)
    # with no P2->P1 leak, P1's provable lower bound can only rise
    base=solve_lane(doc)
    assert (res['plots']['P1']['invasive_lower']
            >= base['plots']['P1']['invasive_lower'])


# --------------------------------------------------------------------------- #
# Criterion 5: tampering is located on replay
# --------------------------------------------------------------------------- #
def test_tampered_raw_read_is_detected_by_audit(tmp_path):
    db=_two_plots(tmp_path)
    import_lane(db,FIX/'edna-lane.json')
    assert audit(db)['status']=='matched'
    with closing_connect(db) as conn:
        # mutate a captured raw read inside the immutable evidence blob
        row=conn.execute("SELECT raw FROM evidence WHERE id=?",
                         ('edna-lane-L1-v1',)).fetchone()
        doc=json.loads(row[0])
        doc['wells'][0]['reads']['BIRD-A']=999
        conn.execute("UPDATE evidence SET raw=? WHERE id=?",
                     (json.dumps(doc).encode(),'edna-lane-L1-v1'))
        conn.commit()
    result=audit(db)
    assert result['status']=='divergent'
    assert 'edna_attribution' in result['differences'] or 'edna_lane_wells' in result['differences']


def test_tampered_attribution_projection_is_detected_by_audit(tmp_path):
    db=_two_plots(tmp_path)
    import_lane(db,FIX/'edna-lane.json')
    assert audit(db)['status']=='matched'
    with closing_connect(db) as conn:
        # a hand-edited projection cannot be reproduced from the raw evidence
        conn.execute("UPDATE edna_attribution SET invasive_lower=99 WHERE plot_id='P1'")
        conn.commit()
    result=audit(db)
    assert result['status']=='divergent'
    assert 'edna_attribution' in result['differences']


def test_reference_correction_re_solves_the_joint_set(tmp_path):
    db=_two_plots(tmp_path)
    import_lane(db,FIX/'edna-lane.json')
    revised=_lane_doc()
    revised['id']='edna-lane-L1-v2'
    revised['ref_version']='refdb-2026.06'
    # a reference correction that resolves the ambiguous barcode changes attribution
    for bc in revised['barcodes']:
        if bc['identity']=='ambiguous':
            bc['identity']='native'
    p=tmp_path/'lane-v2.json';p.write_text(json.dumps(revised))
    out=import_lane(db,p)
    assert out['version']==2
    history=report(db)['edna_lane_history']
    assert [h['version'] for h in history]==[1,2]
    # latest projection reflects the re-solved set; the old version stays on record
    latest=[r for r in report(db)['edna_attribution'] if r['version']==2]
    assert latest and all(r['unattributed_reads']==0 for r in latest)
    assert audit(db)['status']=='matched'


def test_cli_exposes_both_pathways(tmp_path):
    db=_two_plots(tmp_path)
    def cli(*args):
        return subprocess.run([sys.executable,'-m','tidalrestore',*args,'--db',str(db)],
                              capture_output=True,text=True,cwd=str(FIX.parent))
    r=cli('lane-import','--file',str(FIX/'edna-lane.json'))
    assert r.returncode==0,r.stderr
    cons=json.loads(cli('conservative-plan').stdout)
    prob=json.loads(cli('probabilistic-plan').stdout)
    assert cons['pathway']=='conservative_corroboration'
    assert prob['pathway']=='probabilistic_patrol'


def closing_connect(path):
    return connect(path)
