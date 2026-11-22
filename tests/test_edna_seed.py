from pathlib import Path
from tidalrestore.baseline import import_baseline
from tidalrestore.edna import import_sample
from tidalrestore.store import report
from tidalrestore.audit import audit

FIX=Path(__file__).resolve().parent.parent/'fixtures'
def test_edna_capture_baseline(tmp_path):
    db=tmp_path/'edna.sqlite'
    import_baseline(db,FIX/'baseline-ok.json')
    assert import_sample(db,FIX/'edna-ok.json')['status']=='captured'
    assert import_sample(db,FIX/'edna-ok.json')['replayed']
    assert report(db)['edna_samples'][0]['read_count']==36
    assert audit(db)['status']=='matched'
