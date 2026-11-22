import tempfile
from pathlib import Path
from contextlib import closing
from .store import connect,report
from .baseline import import_baseline
from .works import import_contract,import_work
from .survey import import_survey

def audit(path):
    with closing(connect(path)) as db:
        events=[dict(x) for x in db.execute('SELECT kind,raw FROM evidence ORDER BY seq')]
    original=report(path)
    with tempfile.TemporaryDirectory(prefix='tidal-audit-') as folder:
        replay=Path(folder)/'replay.sqlite'
        for i,event in enumerate(events):
            file=Path(folder)/f'{i}.json';file.write_bytes(event['raw'])
            {'baseline':import_baseline,'contract':import_contract,'work':import_work,'survey':import_survey}[event['kind']](replay,file)
        rebuilt=report(replay)
    keys=('plots','baseline','contracts','works','surveys','acceptance')
    return {'status':'matched' if all(original[k]==rebuilt[k] for k in keys) else 'divergent','event_count':len(events),
            'differences':[k for k in keys if original[k]!=rebuilt[k]]}
