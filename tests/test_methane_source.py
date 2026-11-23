import json,sqlite3,subprocess,sys
from pathlib import Path
import pytest
from tidalrestore.baseline import import_baseline
from tidalrestore.carbon import import_flux
from tidalrestore.methane import import_methane,import_methane_route
from tidalrestore.store import report
from tidalrestore.audit import audit

FIX=Path(__file__).resolve().parent.parent/'fixtures'

def write(tmp,name,doc):
    p=tmp/name;p.write_text(json.dumps(doc),encoding='utf8');return p
def campaign(**changes):
    d=json.loads((FIX/'methane-mix.json').read_text(encoding='utf8'));d.update(changes);return d
def route_a(**changes):
    d=dict(id='mx1-ra',kind='methane_route',campaign_id='MX1',route='A',
           samples=[dict(id='s-pw1',role='porewater',chamber_id='pw-1',phase='low',
                         started_at='2026-04-03T16:00:00Z',ended_at='2026-04-03T17:00:00Z',
                         calib_batch='CAL-7',ch4_mg=15,tol_mg=1,iso=[6,6,9])]);d.update(changes);return d
def route_b(**changes):
    d=dict(id='mx1-rb',kind='methane_route',campaign_id='MX1',route='B',
           samples=[dict(id='s-tr1',role='transect',chamber_id='up-2',phase='mid',
                         started_at='2026-04-03T10:00:00Z',ended_at='2026-04-03T11:00:00Z',
                         calib_batch='CAL-7',ch4_mg=15,tol_mg=1,lag_minutes=45,iso=[6,6,2])]);d.update(changes);return d
def base(tmp,name='x.sqlite'):
    db=tmp/name;import_baseline(db,FIX/'baseline-ok.json');return db
def assessment(db,campaign_id='MX1'):
    row=[r for r in report(db)['methane_assessments'] if r['campaign_id']==campaign_id][0]
    return json.loads(row['result'])

# Acceptance 1: fixed replica F1 capture/report/audit, and rejection without P1.
def test_flux_capture_report_audit_and_unknown_plot(tmp_path):
    db=base(tmp_path)
    assert import_flux(db,FIX/'flux-ok.json')=={'flux_id':'F1','status':'captured'}
    assert import_flux(db,FIX/'flux-ok.json')['replayed']
    flux=report(db)['fluxes'][0]
    assert (flux['id'],flux['tide'],flux['chamber_id'],flux['ch4_mg'],flux['co2_mg'])==('F1','mid','ch-1',18,120)
    assert audit(db)['status']=='matched'
    with pytest.raises(ValueError,match='unknown_plot'):
        import_flux(tmp_path/'empty.sqlite',FIX/'flux-ok.json')
    with pytest.raises(ValueError,match='unknown_plot'):
        import_methane(tmp_path/'empty2.sqlite',FIX/'methane-mix.json')

# Acceptance 2: four readings + one shared drift keep BOTH source allocations feasible.
def test_shared_drift_keeps_full_joint_feasible_set(tmp_path):
    db=base(tmp_path);import_flux(db,FIX/'flux-ok.json')
    r=import_methane(db,FIX/'methane-mix.json')
    assert r['status']=='undetermined'
    res=assessment(db)
    lo,hi=res['local_fraction']['plot']
    assert lo<=0.2 and hi>=0.8 and hi-lo>0.5  # no fake single value
    assert res['scenarios']['local_80']['feasible']
    assert res['scenarios']['external_80']['feasible']
    # Concentration nearest-neighbour (18/20 vs upstream 12) must NOT decide it:
    # the external-80 witness exists and uses the ONE shared drift at an extreme.
    wit=res['scenarios']['external_80']['witness']
    assert abs(wit['delta_mg'])==3.0 and wit['local_fraction']<=0.2
    assert set(wit['uses_readings'])=={'s-ch1','s-ch2','s-up1'}
    # The batch-shared drift is +/-3 mg jointly; averaging four points cannot claim +/-1.5.
    assert res['shared_drift_mg']==3
    assert '1.5' not in str(res['shared_drift_mg'])
    assert res['rank_deficient'] and res['unidentified_dofs']>=1
    assert res['resampling_plan'] and {x['route'] for x in res['resampling_plan']}=={'A','B'}
    assert audit(db)['status']=='matched'

# Acceptance 3: a third independent measurement breaks the rank deficiency and
# the two routes point at different governance locations from identical inputs.
def test_third_independent_channel_routes_decide_opposite_governance(tmp_path):
    db_a=base(tmp_path,'a.sqlite');import_methane(db_a,FIX/'methane-mix.json')
    r=import_methane_route(db_a,write(tmp_path,'ra.json',route_a()))
    assert r['status']=='local_dominant' and r['target']=='P1'
    res=assessment(db_a)
    lo,hi=res['local_fraction']['plot']
    assert lo>0.5 and not res['rank_deficient']
    assert not res['scenarios']['external_80']['feasible']
    assert res['decision']['target']=='P1' and res['decision']['suspend']=='upstream interception works'
    assert audit(db_a)['status']=='matched'
    db_b=base(tmp_path,'b.sqlite');import_methane(db_b,FIX/'methane-mix.json')
    r=import_methane_route(db_b,write(tmp_path,'rb.json',route_b()))
    assert r['status']=='external_dominant' and r['target']=='upstream'
    res=assessment(db_b)
    lo,hi=res['local_fraction']['plot']
    assert hi<0.5 and not res['rank_deficient']
    assert not res['scenarios']['local_80']['feasible']
    assert res['decision']['target']=='upstream' and res['decision']['suspend']=='P1 contractor attribution'
    assert audit(db_b)['status']=='matched'

# Acceptance 4: tidal lag gaps, missing measurements and close endmembers keep
# the unknown degrees of freedom and the resampling list.
def test_lag_gap_missing_upstream_and_close_endmembers_keep_unknowns(tmp_path):
    doc=campaign(id='mx2',campaign_id='MX2')
    doc['samples']=[s for s in doc['samples'] if s['role']!='upstream']
    db=base(tmp_path)
    import_methane(db,write(tmp_path,'m2.json',doc))
    res=assessment(db,'MX2')
    assert res['local_fraction']['plot']==[0.0,1.0]
    assert 'upstream' in res['missing']
    assert res['unidentified_dofs']>=1 and res['resampling_plan']
    doc=campaign(id='mx3',campaign_id='MX3',lag_window_minutes=[180,240])
    import_methane(db,write(tmp_path,'m3.json',doc))
    res=assessment(db,'MX3')
    assert res['lag']['aligned'] is False and 'lag_alignment' in res['missing']
    lo,hi=res['local_fraction']['plot']
    assert hi-lo>0.8 and res['status']=='undetermined'
    assert any(x['route']=='B' for x in res['resampling_plan'])
    # The shipped endmembers are identical in both channels: linearly dependent.
    doc=campaign(id='mx4',campaign_id='MX4')
    import_methane(db,write(tmp_path,'m4.json',doc))
    res=assessment(db,'MX4')
    assert res['rank_deficient'] and res['unidentified_dofs']==3
    assert audit(db)['status']=='matched'

# Acceptance 5: source intervals trace back to raw readings; changing an
# observation or the stored inference projection is detectable.
def test_source_intervals_trace_to_readings_and_tampering_is_detected(tmp_path):
    db=base(tmp_path);import_methane(db,FIX/'methane-mix.json')
    res=assessment(db)
    assert res['readings']['s-ch1']['ch4_mg']==18
    assert res['readings']['s-ch2']['ch4_mg']==20
    assert res['readings']['s-up1']['ch4_mg']==12
    row=[r for r in report(db)['methane_assessments'] if r['campaign_id']=='MX1'][0]
    assert (row['local_lo'],row['local_hi'])==('1/88','1')  # exact Fraction bounds stored
    # A changed observation yields a different projection.
    doc=campaign(id='mx5',campaign_id='MX5');doc['samples'][1]['ch4_mg']=25
    import_methane(db,write(tmp_path,'m5.json',doc))
    assert assessment(db,'MX5')['local_fraction']['plot']!=res['local_fraction']['plot']
    # Re-importing altered bytes under the same evidence id is rejected.
    bad=campaign();bad['samples'][0]['ch4_mg']=99
    with pytest.raises(ValueError,match='evidence_id_conflict'):
        import_methane(db,write(tmp_path,'bad.json',bad))
    # Tampering with the stored inference projection diverges from evidence replay.
    conn=sqlite3.connect(db);conn.execute("UPDATE methane_assessment SET status='local_dominant' WHERE campaign_id='MX1'")
    conn.commit();conn.close()
    assert audit(db)['status']=='divergent'

def test_shared_batch_and_route_validation(tmp_path):
    db=base(tmp_path)
    doc=campaign(id='mx6',campaign_id='MX6');doc['samples'][0]['calib_batch']='CAL-9'
    with pytest.raises(ValueError,match='calibration_batch_mismatch'):
        import_methane(db,write(tmp_path,'m6.json',doc))
    import_methane(db,FIX/'methane-mix.json')
    with pytest.raises(ValueError,match='porewater_phase_invalid'):
        import_methane_route(db,write(tmp_path,'ra-mid.json',route_a(id='mx1-ra2',samples=[dict(route_a()['samples'][0],phase='mid')])))
    with pytest.raises(ValueError,match='transect_lag_invalid'):
        import_methane_route(db,write(tmp_path,'rb-lag.json',route_b(id='mx1-rb2',samples=[dict(route_b()['samples'][0],lag_minutes=90)])))
    assert audit(db)['status']=='matched'

def test_methane_cli(tmp_path):
    db=tmp_path/'cli.sqlite'
    cli=lambda *a:subprocess.run([sys.executable,'-m','tidalrestore',*a,'--db',str(db)],capture_output=True,text=True)
    assert cli('baseline-import','--file',str(FIX/'baseline-ok.json')).returncode==0
    out=cli('methane-import','--file',str(FIX/'methane-mix.json'))
    assert out.returncode==0,out.stderr
    assert json.loads(out.stdout)['status']=='undetermined'
    p=write(tmp_path,'ra.json',route_a())
    out=cli('methane-route-import','--file',str(p))
    assert json.loads(out.stdout)['target']=='P1'
    assert json.loads(cli('audit').stdout)['status']=='matched'
