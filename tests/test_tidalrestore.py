import json,sys,subprocess
import pytest
from tidalrestore.baseline import import_baseline
from tidalrestore.works import import_contract,import_work
from tidalrestore.survey import import_survey
from tidalrestore.store import report
from tidalrestore.audit import audit

def file(tmp,name,doc):
    p=tmp/name;p.write_text(json.dumps(doc),encoding='utf8');return p
def baseline(**changes):
    d=dict(id='b1',kind='baseline',plot_id='P1',area=100,season='spring',transects=[dict(id='t1',tier='high',coordinate='A',cover=30),dict(id='t2',tier='mid',coordinate='B',cover=20),dict(id='t3',tier='low',coordinate='C',cover=10)]);d.update(changes);return d
def contract(**changes):
    d=dict(id='c1',kind='contract',contract_id='C1',plot_id='P1',species='native-grass',planned_area=60,window_start='2026-04-01T08:00:00Z',window_end='2026-04-01T12:00:00Z',contractor='build-a');d.update(changes);return d
def work(**changes):
    d=dict(id='w1',kind='work',event_id='W1',contract_id='C1',event_type='install',area=60,at='2026-04-01T10:00:00Z');d.update(changes);return d
def survey(**changes):
    d=dict(id='s1',kind='survey',survey_id='S1',plot_id='P1',season='spring',observer='eco-b',high_alive=80,mid_alive=80,low_alive=80);d.update(changes);return d

def setup(tmp):
    db=tmp/'x.sqlite';import_baseline(db,file(tmp,'base.json',baseline()));import_contract(db,file(tmp,'contract.json',contract()));return db
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
    import_contract(db,file(tmp_path,'contract.json',contract()))
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
    import_work(db,file(tmp_path,'storm.json',work(id='w2',event_id='W2',event_type='storm_damage',area=20,at='2026-04-03T10:00:00Z')))
    assert report(db)['acceptance'][0]['status']=='rework'

def test_public_cli(tmp_path):
    p=file(tmp_path,'base.json',baseline());db=tmp_path/'cli.sqlite'
    out=subprocess.run([sys.executable,'-m','tidalrestore','baseline-import','--db',str(db),'--file',str(p)],capture_output=True,text=True)
    assert out.returncode==0,out.stderr
    assert json.loads(out.stdout)['status']=='confirmed'
