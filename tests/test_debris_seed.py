from pathlib import Path
from tidalrestore.baseline import import_baseline
from tidalrestore.debris import import_observation
from tidalrestore.store import report
from tidalrestore.audit import audit

FIX=Path(__file__).resolve().parent.parent/'fixtures'
def test_debris_capture_baseline(tmp_path):
    db=tmp_path/'debris.sqlite'
    import_baseline(db,FIX/'baseline-ok.json')
    assert import_observation(db,FIX/'debris-ok.json')['status']=='captured'
    assert import_observation(db,FIX/'debris-ok.json')['replayed']
    assert report(db)['debris'][0]['pieces']==2
    assert audit(db)['status']=='matched'
