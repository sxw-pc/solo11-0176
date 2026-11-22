from pathlib import Path
from tidalrestore.baseline import import_baseline
from tidalrestore.carbon import import_flux
from tidalrestore.store import report
from tidalrestore.audit import audit

FIX=Path(__file__).resolve().parent.parent/'fixtures'
def test_flux_capture_baseline(tmp_path):
    db=tmp_path/'carbon.sqlite'
    import_baseline(db,FIX/'baseline-ok.json')
    assert import_flux(db,FIX/'flux-ok.json')['status']=='captured'
    assert import_flux(db,FIX/'flux-ok.json')['replayed']
    assert report(db)['fluxes'][0]['ch4_mg']==18
    assert audit(db)['status']=='matched'
