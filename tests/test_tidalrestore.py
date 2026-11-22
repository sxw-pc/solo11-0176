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
    out=subprocess.run([sys.executable,'-B','-m','tidalrestore','baseline-import','--db',str(db),'--file',str(p)],capture_output=True,text=True)
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
    cli=lambda *args:subprocess.run([sys.executable,'-B','-m','tidalrestore',*args,'--db',str(db)],capture_output=True,text=True)
    result=cli('demo');assert result.returncode==0,result.stderr
    assert json.loads(result.stdout)['acceptance'][0]['status']=='accepted'
    assert json.loads(cli('audit').stdout)['status']=='matched'
    for command,fixture in [('material-import','material-bad.json'),('survey-import','survey-bad.json')]:
        p=cli(command,'--file',str(__import__('pathlib').Path(__file__).parent.parent/'fixtures'/fixture))
        if command=='material-import':assert json.loads(p.stdout)['status']=='quarantined'
        else:assert p.returncode!=0
