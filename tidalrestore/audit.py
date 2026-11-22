import json,tempfile
from pathlib import Path
from contextlib import closing
from .store import connect,report
from .baseline import import_baseline
from .works import import_contract,import_work
from .survey import import_survey
from .material import import_material
from .tender import import_tender_bundle,import_tender_award,import_tender_withdrawal,reopen_gap_award

REPLAY={'baseline':import_baseline,'material':import_material,'contract':import_contract,
        'work':import_work,'survey':import_survey,'tender_bundle':import_tender_bundle,
        'tender_award':import_tender_award,'tender_withdrawal':import_tender_withdrawal}

def audit(path):
    with closing(connect(path)) as db:
        events=[dict(x) for x in db.execute('SELECT kind,raw FROM evidence ORDER BY seq')]
    original=report(path)
    with tempfile.TemporaryDirectory(prefix='tidal-audit-') as folder:
        replay=Path(folder)/'replay.sqlite'
        for i,event in enumerate(events):
            file=Path(folder)/f'{i}.json';file.write_bytes(event['raw'])
            kind=event['kind']
            # A tender_award event reopens open gaps when the replay state has
            # them, otherwise it is the original computed award.
            if kind=='tender_award':
                doc=json.loads(event['raw'])
                fn=reopen_gap_award if _has_open_gap_target(replay,doc) else import_tender_award
            else:
                fn=REPLAY[kind]
            fn(replay,file)
        rebuilt=report(replay)
    keys=('plots','baseline','baseline_disputes','materials','contracts','works','work_cells','surveys','acceptance','acceptance_history',
          'tender_captures','tender_pages','tender_bids','tender_awards','tender_award_items','tender_gaps','material_arrivals','decisions')
    return {'status':'matched' if all(original[k]==rebuilt[k] for k in keys) else 'divergent','event_count':len(events),
            'differences':[k for k in keys if original[k]!=rebuilt[k]]}

def _has_open_gap_target(replay_path,doc):
    with closing(connect(replay_path)) as db:
        return db.execute("SELECT 1 FROM tender_gap WHERE tender_id=? AND status='open'",
                          (doc['tender_id'],)).fetchone() is not None
