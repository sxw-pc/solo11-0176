import json,sys,subprocess
import pytest
from tidalrestore.baseline import import_baseline
from tidalrestore.works import import_contract,import_work
from tidalrestore.survey import import_survey
from tidalrestore.store import report
from tidalrestore.audit import audit
from tidalrestore.material import import_material

def file(tmp,name,doc):
    p=tmp/name;p.write_text(json.dumps(doc),encoding='utf8');return p
def baseline(**changes):
    d=dict(id='b1',kind='baseline',plot_id='P1',area=100,season='spring',transects=[dict(id='t1',tier='high',coordinate='A',cover=30),dict(id='t2',tier='mid',coordinate='B',cover=20),dict(id='t3',tier='low',coordinate='C',cover=10)]);d.update(changes);return d
def contract(**changes):
    d=dict(id='c1',kind='contract',contract_id='C1',plot_id='P1',species='native-grass',planned_area=60,window_start='2026-04-01T08:00:00Z',window_end='2026-04-01T12:00:00Z',repair_start='2026-04-04T08:00:00Z',repair_end='2026-04-05T12:00:00Z',contractor='build-a');d.update(changes);return d
def work(**changes):
    d=dict(id='w1',kind='work',event_id='W1',contract_id='C1',event_type='install',area=60,at='2026-04-01T10:00:00Z',material_id='M1',cells=[dict(id='C-H',tier='high',area=20),dict(id='C-M',tier='mid',area=20),dict(id='C-L',tier='low',area=20)]);d.update(changes);return d
def survey(**changes):
    d=dict(id='s1',kind='survey',survey_id='S1',plot_id='P1',season='spring',observer='eco-b',high_alive=80,mid_alive=80,low_alive=80,at='2026-04-03T10:00:00Z',sample_coords=dict(high='A',mid='B',low='C'));d.update(changes);return d

def material(**changes):
    d=dict(id='m1',kind='material',batch_id='M1',species='native-grass',origin='local',quantity=100,valid_until='2026-12-31T00:00:00Z');d.update(changes);return d

def setup(tmp):
    db=tmp/'x.sqlite';import_baseline(db,file(tmp,'base.json',baseline()));import_material(db,file(tmp,'material.json',material()));import_contract(db,file(tmp,'contract.json',contract()));return db
def test_install_and_independent_survey_accept(tmp_path):
    print(__import__('tidalrestore').__file__)
    db=setup(tmp_path);import_work(db,file(tmp_path,'work.json',work()))
    assert import_survey(db,file(tmp_path,'survey.json',survey()))['status']=='accepted'
    assert len(report(db)['baseline'])==3
    assert audit(db)['status']=='matched'

def test_three_tiers_are_required(tmp_path):
    with pytest.raises(ValueError,match='baseline_coverage'):
        import_baseline(tmp_path/'x.sqlite',file(tmp_path,'bad.json',baseline(transects=baseline()['transects'][:2])))

def test_non_native_material_and_tide_window_rejected(tmp_path):
    db=tmp_path/'x.sqlite';import_baseline(db,file(tmp_path,'base.json',baseline()))
    with pytest.raises(ValueError,match='non_native_material'):
        import_contract(db,file(tmp_path,'badcontract.json',contract(species='foreign-grass')))
    import_material(db,file(tmp_path,'material.json',material()));import_contract(db,file(tmp_path,'contract.json',contract()))
    with pytest.raises(ValueError,match='outside_tide_window'):
        import_work(db,file(tmp_path,'badwork.json',work(at='2026-04-01T13:00:00Z')))

def test_survey_independence_and_season(tmp_path):
    db=setup(tmp_path);import_work(db,file(tmp_path,'work.json',work()))
    with pytest.raises(ValueError,match='survey_not_independent'):
        import_survey(db,file(tmp_path,'self.json',survey(observer='build-a')))
    with pytest.raises(ValueError,match='season_not_comparable'):
        import_survey(db,file(tmp_path,'season.json',survey(season='autumn')))

def test_storm_damage_reopens_acceptance(tmp_path):
    db=setup(tmp_path);import_work(db,file(tmp_path,'work.json',work()))
    import_survey(db,file(tmp_path,'survey.json',survey()))
    import_work(db,file(tmp_path,'storm.json',work(id='w2',event_id='W2',event_type='storm_damage',area=20,at='2026-04-03T10:00:00Z',cells=['C-H'])))
    assert report(db)['acceptance'][0]['status']=='rework'

def test_public_cli(tmp_path):
    p=file(tmp_path,'base.json',baseline());db=tmp_path/'cli.sqlite'
    out=subprocess.run([sys.executable,'-m','tidalrestore','baseline-import','--db',str(db),'--file',str(p)],capture_output=True,text=True)
    assert out.returncode==0,out.stderr
    assert json.loads(out.stdout)['status']=='confirmed'

def test_material_origin_and_batch_quantity_change_eligible_work(tmp_path):
    db=setup(tmp_path)
    import_material(db,file(tmp_path,'foreign.json',material(id='m2',batch_id='M2',origin='overseas')))
    with pytest.raises(ValueError,match='material_not_eligible'):
        import_work(db,file(tmp_path,'foreign-work.json',work(material_id='M2')))
    # A low quantity native batch is independently approved but cannot cover the installation.
    import_material(db,file(tmp_path,'low.json',material(id='m3',batch_id='M3',quantity=30)))
    with pytest.raises(ValueError,match='material_quantity_exceeded'):
        import_work(db,file(tmp_path,'low-work.json',work(material_id='M3')))
    assert report(db)['works']==[]

def test_same_tier_coordinate_conflict_freezes_future_work_and_old_acceptance(tmp_path):
    db=setup(tmp_path);import_work(db,file(tmp_path,'work.json',work()))
    import_survey(db,file(tmp_path,'survey.json',survey()))
    new=baseline(id='b2',transects=[dict(x) for x in baseline()['transects']])
    new['transects'][0]['coordinate']='OTHER'
    result=import_baseline(db,file(tmp_path,'changed.json',new))
    assert result['status']=='frozen'
    data=report(db)
    assert data['acceptance'][0]['status']=='rework'
    assert data['acceptance_history'][-1]['reason']=='baseline_coordinate_conflict'
    with pytest.raises(ValueError,match='baseline_frozen'):
        import_contract(db,file(tmp_path,'new-contract.json',contract(id='c2',contract_id='C2')))
    assert audit(db)['status']=='matched'

def test_cell_overlap_and_storm_damage_weighted_rework_then_replant(tmp_path):
    db=setup(tmp_path);import_work(db,file(tmp_path,'work.json',work()))
    assert import_survey(db,file(tmp_path,'survey.json',survey()))['status']=='accepted'
    storm=work(id='w2',event_id='W2',event_type='storm_damage',area=20,
               at='2026-04-03T12:00:00Z',cells=['C-H'])
    import_work(db,file(tmp_path,'storm.json',storm))
    assert report(db)['acceptance'][0]['status']=='rework'
    low=survey(id='s2',survey_id='S2',at='2026-04-03T13:00:00Z',high_alive=80,mid_alive=80,low_alive=80)
    decision=import_survey(db,file(tmp_path,'storm-survey.json',low))
    assert decision['weighted_survival']==53
    assert decision['status']=='rework'
    repair=work(id='w3',event_id='W3',event_type='replant',area=20,
                at='2026-04-04T10:00:00Z',cells=[dict(id='C-H',tier='high',area=20)])
    import_work(db,file(tmp_path,'repair.json',repair))
    recovered=survey(id='s3',survey_id='S3',at='2026-04-06T10:00:00Z')
    assert import_survey(db,file(tmp_path,'recovered.json',recovered))['status']=='accepted'
    assert [x['status'] for x in report(db)['acceptance_history']]==['accepted','rework','rework','accepted']
    assert audit(db)['status']=='matched'

def test_same_cell_cannot_be_counted_twice(tmp_path):
    db=setup(tmp_path)
    first=work(area=20,cells=[dict(id='C-H',tier='high',area=20)])
    import_work(db,file(tmp_path,'first.json',first))
    duplicate=work(id='w2',event_id='W2',area=20,
                   cells=[dict(id='C-H',tier='high',area=20)])
    with pytest.raises(ValueError,match='work_cell_overlap'):
        import_work(db,file(tmp_path,'duplicate.json',duplicate))
    assert len(report(db)['work_cells'])==1
    assert audit(db)['status']=='matched'

def test_late_independent_survey_retracts_earlier_acceptance(tmp_path):
    db=setup(tmp_path);import_work(db,file(tmp_path,'work.json',work()))
    import_survey(db,file(tmp_path,'first.json',survey()))
    late=survey(id='s2',survey_id='S2',at='2026-04-05T10:00:00Z',
                high_alive=20,mid_alive=30,low_alive=40)
    assert import_survey(db,file(tmp_path,'late.json',late))['status']=='rework'
    hist=report(db)['acceptance_history']
    assert [x['status'] for x in hist]==['accepted','rework']
    assert audit(db)['status']=='matched'

def test_public_demo_and_failure_fixtures(tmp_path):
    db=tmp_path/'demo.sqlite'
    cli=lambda *args:subprocess.run([sys.executable,'-m','tidalrestore',*args,'--db',str(db)],capture_output=True,text=True)
    result=cli('demo');assert result.returncode==0,result.stderr
    assert json.loads(result.stdout)['acceptance'][0]['status']=='accepted'
    assert json.loads(cli('audit').stdout)['status']=='matched'
    for command,fixture in [('material-import','material-bad.json'),('survey-import','survey-bad.json')]:
        p=cli(command,'--file',str(__import__('pathlib').Path(__file__).parent.parent/'fixtures'/fixture))
        if command=='material-import':assert json.loads(p.stdout)['status']=='quarantined'
        else:assert p.returncode!=0

# ---------------------------------------------------------------------------
# Baseline coordinate disputes: independent rechecks and reproducible rulings.
# ---------------------------------------------------------------------------
from pathlib import Path
from tidalrestore.review import import_recheck, adjudicate

FIX=Path(__file__).resolve().parent.parent/'fixtures'

def accepted_setup(tmp):
    db=setup(tmp);import_work(db,file(tmp,'work.json',work()))
    assert import_survey(db,file(tmp,'survey.json',survey()))['status']=='accepted'
    return db

def disputed_baseline(**changes):
    d=baseline(id='b2',transects=[dict(x) for x in baseline()['transects']])
    d['transects'][0]['coordinate']='D'
    d.update(changes);return d

def recheck(**changes):
    d=dict(id='r1',kind='recheck',plot_id='P1',tier='high',coordinate='A',
           agency='geo-third-party',authorization='third_party_surveyor',clock_synced=True,
           tide_window=dict(**{'from':'2026-04-01T08:00:00Z'},**{'to':'2026-04-01T12:00:00Z'}),
           track=[dict(lat=1.0,lon=103.0,coordinate='A')],supported_coordinate='A')
    d.update(changes);return d

def test_disputed_baseline_freezes_and_requires_spatial_recheck(tmp_path):
    db=accepted_setup(tmp_path)
    out=import_baseline(db,file(tmp_path,'b2.json',disputed_baseline()))
    assert out['status']=='frozen' and out['disputed_tiers']==['high']
    data=report(db)
    assert data['plots'][0]['baseline_status']=='frozen'
    assert data['acceptance'][0]['status']=='rework'
    assert data['baseline_disputes'][0]['coordinate']=='D'
    assert data['dispute_decisions'][0]['decision']=='insufficient_evidence'
    assert 'valid_independent_track' in json.loads(data['dispute_decisions'][0]['missing'])
    assert audit(db)['status']=='matched'

def test_route_a_two_agencies_restore_original_without_double_counting(tmp_path):
    db=accepted_setup(tmp_path)
    import_baseline(db,file(tmp_path,'b2.json',disputed_baseline()))
    first=import_recheck(db,file(tmp_path,'r1.json',recheck()))
    assert first['valid'] is True and first['decisions'][0]['decision']=='single_source'
    assert report(db)['acceptance'][0]['status']=='rework'
    second=import_recheck(db,file(tmp_path,'r2.json',recheck(id='r2',agency='eco-monitor',authorization='ecological_monitor')))
    assert second['decisions'][0]['decision']=='original_upheld'
    data=report(db)
    assert data['plots'][0]['baseline_status']=='confirmed'
    assert data['acceptance'][0]['status']=='accepted'
    assert data['acceptance'][0]['weighted_survival']==80
    # S1 is not a new survey round; the restored acceptance is a new version.
    assert len(data['surveys'])==1
    assert [h['reason'] for h in data['acceptance_history']]==[
        'survival_and_area','baseline_coordinate_conflict','baseline_original_upheld']
    spatial={(x['object_type'],x['object_id']):x['covered'] for x in data['spatial_checks']}
    assert spatial=={('work_cell','C-H'):1,('survey','S1'):1}
    assert data['acceptance_snapshots'][0]['status']=='accepted'
    assert data['acceptance_snapshots'][0]['score']==80
    assert audit(db)['status']=='matched' and audit(db)['differences']==[]

def test_route_b_two_agencies_adopt_revised_but_never_restore_acceptance(tmp_path):
    db=accepted_setup(tmp_path)
    import_baseline(db,file(tmp_path,'b2.json',disputed_baseline()))
    import_recheck(db,file(tmp_path,'r1.json',recheck(id='r1',coordinate='D',supported_coordinate='D',
                                                       track=[dict(lat=1.1,lon=103.1,coordinate='D')])))
    out=import_recheck(db,file(tmp_path,'r2.json',recheck(id='r2',coordinate='D',supported_coordinate='D',
                                                           agency='eco-monitor',authorization='ecological_monitor',
                                                           track=[dict(lat=1.1,lon=103.1,coordinate='D')])))
    assert out['decisions'][0]['decision']=='revised_adopted'
    data=report(db)
    assert data['plots'][0]['baseline_status']=='recheck_required'
    assert {x['tier']:x['coordinate'] for x in data['baseline']}['high']=='D'
    assert data['acceptance'][0]['status']=='rework'
    assert data['acceptance'][0]['weighted_survival']==0
    assert data['acceptance'][0]['reason']=='baseline_coordinate_revised_recheck_required'
    # Existing work and the old survey did not cover D, so they cannot be reused.
    spatial={(x['object_type'],x['object_id']):x['covered'] for x in data['spatial_checks']}
    assert spatial=={('work_cell','C-H'):0,('survey','S1'):0}
    assert audit(db)['status']=='matched'

def test_route_b_frozen_plot_rejects_new_contract_and_survey(tmp_path):
    db=accepted_setup(tmp_path)
    import_baseline(db,file(tmp_path,'b2.json',disputed_baseline()))
    with pytest.raises(ValueError,match='baseline_frozen'):
        import_contract(db,file(tmp_path,'c2.json',contract(id='c2',contract_id='C2')))
    late=survey(id='s9',survey_id='S9',at='2026-04-05T10:00:00Z')
    with pytest.raises(ValueError,match='baseline_frozen'):
        import_survey(db,file(tmp_path,'late.json',late))

def test_single_source_opposing_and_clock_gap_keep_frozen(tmp_path):
    db=accepted_setup(tmp_path)
    import_baseline(db,file(tmp_path,'b2.json',disputed_baseline()))
    import_recheck(db,file(tmp_path,'r1.json',recheck()))
    import_recheck(db,file(tmp_path,'r2.json',recheck(id='r2',agency='eco-monitor',authorization='ecological_monitor',
                                                       coordinate='D',supported_coordinate='D',
                                                       track=[dict(lat=1.1,lon=103.1,coordinate='D')])))
    data=report(db)
    assert data['plots'][0]['baseline_status']=='frozen'
    decision=data['dispute_decisions'][-1]
    assert decision['decision']=='conflicting_agencies'
    assert json.loads(decision['missing'])==['agencies_must_agree']
    # An unclocked track cannot break the tie.
    import_recheck(db,file(tmp_path,'r3.json',recheck(id='r3',agency='eco-monitor',authorization='ecological_monitor',
                                                      clock_synced=False)))
    data=report(db)
    assert data['plots'][0]['baseline_status']=='frozen'
    assert data['dispute_decisions'][-1]['decision']=='conflicting_evidence'
    assert 'clock_synchronized_track_resolving_conflict' in json.loads(data['dispute_decisions'][-1]['missing'])
    assert audit(db)['status']=='matched'

def test_track_outside_tide_window_is_not_evidence(tmp_path):
    db=accepted_setup(tmp_path)
    import_baseline(db,file(tmp_path,'b2.json',disputed_baseline()))
    out=import_recheck(db,file(tmp_path,'r1.json',recheck(
        track=[dict(lat=1.5,lon=103.5,coordinate='D',outside_tide_window=True)])))
    assert out['valid'] is False and out['reason']=='outside_tide_window'
    assert report(db)['plots'][0]['baseline_status']=='frozen'
    assert 'in_tide_window_track' in json.loads(report(db)['dispute_decisions'][-1]['missing'])

def test_partial_tier_coverage_keeps_other_tiers_frozen(tmp_path):
    db=accepted_setup(tmp_path)
    b=disputed_baseline();b['transects'][2]['coordinate']='E'
    import_baseline(db,file(tmp_path,'b2.json',b))
    import_recheck(db,file(tmp_path,'r1.json',recheck()))
    out=import_recheck(db,file(tmp_path,'r2.json',recheck(id='r2',agency='eco-monitor',authorization='ecological_monitor')))
    assert out['decisions'][0]['acceptance_status']=='frozen'
    data=report(db)
    assert data['plots'][0]['baseline_status']=='frozen'
    assert {x['tier']:x['status'] for x in data['dispute_states']}=={'high':'resolved','low':'disputed'}
    low=[x for x in data['dispute_decisions'] if x['tier']=='low'][-1]
    assert 'recheck_covering_low_tier' in json.loads(low['missing'])
    assert audit(db)['status']=='matched'

def test_mid_low_only_track_cannot_resolve_high_dispute(tmp_path):
    db=accepted_setup(tmp_path)
    import_baseline(db,file(tmp_path,'b2.json',disputed_baseline()))
    for i,agency in enumerate(('geo-third-party','eco-monitor')):
        import_recheck(db,file(tmp_path,f'r{i}.json',recheck(id=f'r{i}',tier='mid',coordinate='B',
                                                             supported_coordinate='B',agency=agency,
                                                             authorization=('third_party_surveyor' if i==0 else 'ecological_monitor'),
                                                             track=[dict(lat=2.0,lon=104.0,coordinate='B')])))
    data=report(db)
    assert data['plots'][0]['baseline_status']=='frozen'
    high=[x for x in data['dispute_decisions'] if x['tier']=='high'][-1]
    assert 'recheck_covering_high_tier' in json.loads(high['missing'])

def test_out_of_order_and_reordered_evidence_reproduces_same_ruling(tmp_path):
    forward=tmp_path/'forward';backward=tmp_path/'backward'
    forward.mkdir();backward.mkdir()
    a=accepted_setup(forward)
    import_baseline(a,file(forward,'b2.json',disputed_baseline()))
    for n,d in [('r1.json',recheck()),('r2.json',recheck(id='r2',agency='eco-monitor',authorization='ecological_monitor'))]:
        import_recheck(a,file(forward,n,d))
    b=accepted_setup(backward)
    # Rechecks first, the disputed baseline last: adjudication is recomputed.
    import_recheck(b,file(backward,'r1.json',recheck()))
    import_recheck(b,file(backward,'r2.json',recheck(id='r2',agency='eco-monitor',authorization='ecological_monitor')))
    import_baseline(b,file(backward,'b2.json',disputed_baseline()))
    fa,fb=report(a),report(b)
    assert fa['plots']==fb['plots'] and fa['acceptance']==fb['acceptance']
    # Arrival order changes how many intermediate rulings were recorded, but the
    # final ruling, its evidence, and the acceptance are identical.
    assert fa['dispute_decisions'][-1]['decision']==fb['dispute_decisions'][-1]['decision']=='original_upheld'
    assert fa['baseline_disputes']==fb['baseline_disputes']
    assert fa['spatial_checks']==fb['spatial_checks']
    assert audit(a)['status']=='matched' and audit(b)['status']=='matched'

def test_duplicate_recheck_bytes_cannot_form_a_second_agency(tmp_path):
    db=accepted_setup(tmp_path)
    import_baseline(db,file(tmp_path,'b2.json',disputed_baseline()))
    import_recheck(db,file(tmp_path,'r1.json',recheck()))
    # The same observations re-filed under another id are not a second agency.
    with pytest.raises(ValueError,match='recheck_duplicate_evidence'):
        import_recheck(db,file(tmp_path,'r1-copy.json',recheck(id='r1-copy')))
    assert report(db)['plots'][0]['baseline_status']=='frozen'
    assert audit(db)['status']=='matched'

def test_recheck_does_not_choose_by_survival_or_latest_file(tmp_path):
    db=accepted_setup(tmp_path)
    # A later, higher-survival survey cannot resolve a coordinate dispute.
    import_baseline(db,file(tmp_path,'b2.json',disputed_baseline()))
    higher=survey(id='s2',survey_id='S2',at='2026-04-05T10:00:00Z',high_alive=99,mid_alive=99,low_alive=99)
    with pytest.raises(ValueError,match='baseline_frozen'):
        import_survey(db,file(tmp_path,'higher.json',higher))
    assert report(db)['plots'][0]['baseline_status']=='frozen'

def test_season_and_plot_identity_cannot_be_revised_by_late_baseline(tmp_path):
    db=accepted_setup(tmp_path)
    with pytest.raises(ValueError,match='baseline_plot_mismatch'):
        import_baseline(db,file(tmp_path,'autumn.json',disputed_baseline(id='b3',season='autumn')))
    with pytest.raises(ValueError,match='baseline_plot_mismatch'):
        import_baseline(db,file(tmp_path,'area.json',disputed_baseline(id='b4',area=120)))
    assert report(db)['plots'][0]['baseline_status']=='confirmed'

def test_old_acceptance_report_snapshot_survives_declarations(tmp_path):
    db=accepted_setup(tmp_path)
    import_baseline(db,file(tmp_path,'b2.json',disputed_baseline()))
    data=report(db)
    snap=data['acceptance_snapshots'][0]
    # The original accepted report remains addressable after the dispute.
    assert (snap['status'],snap['score'],snap['reason'])==('accepted',80,'survival_and_area')
    assert snap['version']==1
    # The current report explicitly names the disputed baseline revision.
    current=data['current_reports'][0]
    assert current['version']==2 and 'disputed' in current['evidence_version']
    assert json.loads(current['payload'])['disputes']==[{'tier':'high','status':'disputed'}]

def test_audit_replays_late_recheck_and_keeps_versions(tmp_path):
    db=accepted_setup(tmp_path)
    import_recheck(db,file(tmp_path,'early.json',recheck()))
    import_baseline(db,file(tmp_path,'b2.json',disputed_baseline()))
    import_recheck(db,file(tmp_path,'late.json',recheck(id='r2',agency='eco-monitor',authorization='ecological_monitor')))
    before=report(db)
    assert before['plots'][0]['baseline_status']=='confirmed'
    result=audit(db)
    assert result['status']=='matched',result['differences']
    assert result['event_count']==8

def test_public_cli_recheck_route(tmp_path):
    db=tmp_path/'cli.sqlite'
    cli=lambda *args:subprocess.run([sys.executable,'-m','tidalrestore',*args,'--db',str(db)],capture_output=True,text=True)
    for cmd,name in [('baseline-import','baseline-ok.json'),('material-import','material-ok.json'),
                     ('contract-import','contract-ok.json'),('work-import','work-ok.json'),
                     ('survey-import','survey-ok.json'),('baseline-import','baseline-disputed.json'),
                     ('recheck-import','recheck-original.json'),('recheck-import','recheck-original-eco.json')]:
        out=cli(cmd,'--file',str(FIX/name));assert out.returncode==0,out.stderr
    assert json.loads(cli('audit').stdout)['status']=='matched'
    data=json.loads(cli('report').stdout)
    assert data['acceptance'][0]['status']=='accepted'
    assert data['current_reports'][0]['evidence_version'].startswith('baseline:b1-r1#r1')

def test_duplicate_baseline_under_new_id_is_not_a_second_dispute(tmp_path):
    db=accepted_setup(tmp_path)
    import_baseline(db,file(tmp_path,'b2.json',disputed_baseline()))
    with pytest.raises(ValueError,match='duplicate_baseline_evidence'):
        import_baseline(db,file(tmp_path,'b2-copy.json',disputed_baseline(id='b2-copy')))
    data=report(db)
    assert len(data['baseline_disputes'])==1
    assert data['plots'][0]['baseline_status']=='frozen'

def test_recheck_before_dispute_is_adjudicated_when_dispute_arrives(tmp_path):
    db=accepted_setup(tmp_path)
    import_recheck(db,file(tmp_path,'r1.json',recheck()))
    import_recheck(db,file(tmp_path,'r2.json',recheck(id='r2',agency='eco-monitor',authorization='ecological_monitor')))
    assert report(db)['acceptance'][0]['status']=='accepted'
    out=import_baseline(db,file(tmp_path,'b2.json',disputed_baseline()))
    assert out['decisions'][-1]['decision']=='original_upheld'
    data=report(db)
    assert data['plots'][0]['baseline_status']=='confirmed'
    assert data['acceptance'][0]['status']=='accepted'

def test_contradicting_track_after_restore_reopens_and_keeps_history(tmp_path):
    db=accepted_setup(tmp_path)
    import_baseline(db,file(tmp_path,'b2.json',disputed_baseline()))
    import_recheck(db,file(tmp_path,'r1.json',recheck()))
    import_recheck(db,file(tmp_path,'r2.json',recheck(id='r2',agency='eco-monitor',authorization='ecological_monitor')))
    assert report(db)['acceptance'][0]['status']=='accepted'
    import_recheck(db,file(tmp_path,'r3.json',recheck(id='r3',coordinate='D',supported_coordinate='D',
                                                       track=[dict(lat=1.1,lon=103.1,coordinate='D')])))
    data=report(db)
    assert data['plots'][0]['baseline_status']=='frozen'
    assert data['acceptance'][0]['status']=='rework'
    decisions=[x['decision'] for x in data['dispute_decisions'] if x['tier']=='high']
    assert decisions[:3]==['insufficient_evidence','single_source','original_upheld']
    assert decisions[-1]=='conflicting_agencies'
    # Every earlier ruling and acceptance version stays addressable.
    assert len(data['dispute_decisions'])==len(decisions)
    assert [h['status'] for h in data['acceptance_history']]==['accepted','rework','accepted','rework']
    assert len(data['acceptance_reports'])==4
    assert data['acceptance_reports'][0]['version']==1
    assert data['acceptance_reports'][0]['status']=='accepted'
    assert audit(db)['status']=='matched'

def test_recheck_source_must_not_be_contractor(tmp_path):
    db=accepted_setup(tmp_path)
    import_baseline(db,file(tmp_path,'b2.json',disputed_baseline()))
    with pytest.raises(ValueError,match='recheck_not_independent'):
        import_recheck(db,file(tmp_path,'r1.json',recheck(agency='build-a')))
    with pytest.raises(ValueError,match='recheck_authorization'):
        import_recheck(db,file(tmp_path,'r2.json',recheck(id='r2',authorization='self_declared')))

def test_recheck_season_must_match_disputed_season(tmp_path):
    db=accepted_setup(tmp_path)
    import_baseline(db,file(tmp_path,'b2.json',disputed_baseline()))
    with pytest.raises(ValueError,match='season_not_comparable'):
        import_recheck(db,file(tmp_path,'autumn.json',recheck(season='autumn')))
    assert report(db)['plots'][0]['baseline_status']=='frozen'

def test_route_b_new_survey_only_after_real_resampling_of_adopted_coordinate(tmp_path):
    db=accepted_setup(tmp_path)
    import_baseline(db,file(tmp_path,'b2.json',disputed_baseline()))
    import_recheck(db,file(tmp_path,'r1.json',recheck(id='r1',coordinate='D',supported_coordinate='D',
                                                       track=[dict(lat=1.1,lon=103.1,coordinate='D')])))
    import_recheck(db,file(tmp_path,'r2.json',recheck(id='r2',coordinate='D',supported_coordinate='D',
                                                       agency='eco-monitor',authorization='ecological_monitor',
                                                       track=[dict(lat=1.1,lon=103.1,coordinate='D')])))
    # Reusing the old A-based sample file cannot restore acceptance.
    with pytest.raises(ValueError,match='survey_coverage_mismatch'):
        import_survey(db,file(tmp_path,'old.json',survey(id='s9',survey_id='S9',at='2026-04-05T10:00:00Z')))
    # A new survey at D is still conservatively rework until it also covers work cells;
    # the point is that it is a genuinely new observation, not the old S1.
    resampled=survey(id='s10',survey_id='S10',at='2026-04-05T10:00:00Z',sample_coords=dict(high='D',mid='B',low='C'))
    out=import_survey(db,file(tmp_path,'new.json',resampled))
    data=report(db)
    assert len(data['surveys'])==2
    # W1's high cell was installed at A, so a D sample does not cover it.
    assert out['status']=='rework'
    assert data['acceptance'][0]['reason']=='work_cell_outside_sampled_coordinate'
    # All three cells are still alive, so the score stays 80; the reason for
    # rework is the spatial mismatch, not survival.
    assert data['acceptance'][0]['weighted_survival']==80
    spatial=[x for x in data['spatial_checks'] if x['object_type']=='work_cell'][-1]
    assert spatial['covered']==0
    assert audit(db)['status']=='matched'
