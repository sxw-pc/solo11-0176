from pathlib import Path
from tidalrestore.baseline import import_baseline
from tidalrestore.shoreline import import_profile
from tidalrestore.store import report
from tidalrestore.audit import audit

FIX=Path(__file__).resolve().parent.parent/'fixtures'
def test_shore_profile_baseline(tmp_path):
    db=tmp_path/'shore.sqlite'
    import_baseline(db,FIX/'baseline-ok.json')
    assert import_profile(db,FIX/'shore-profile-ok.json')['point_count']==3
    assert import_profile(db,FIX/'shore-profile-ok.json')['replayed']
    assert report(db)['shore_profiles'][0]['water_level_cm']==25
    assert audit(db)['status']=='matched'
