import tempfile
from pathlib import Path
from contextlib import closing
from .store import connect,report
from .baseline import import_baseline
from .works import import_contract,import_work
from .survey import import_survey
from .material import import_material
from .review import import_recheck

KEYS=('plots','baseline','baseline_revisions','baseline_revision_transects','baseline_disputes',
      'dispute_states','dispute_decisions','rechecks','spatial_checks','materials','contracts',
      'works','work_cells','surveys','acceptance','acceptance_snapshots','acceptance_history',
      'acceptance_reports','current_reports','decisions')

def audit(path):
    """Replay evidence bytes in their original arrival order into a fresh database.

    Adjudication is a function of stored evidence, so late or reordered imports
    reproduce the same dispute versions and acceptance snapshots.
    """
    with closing(connect(path)) as db:
        events=[dict(x) for x in db.execute('SELECT kind,raw FROM evidence ORDER BY seq')]
    original=report(path)
    with tempfile.TemporaryDirectory(prefix='tidal-audit-') as folder:
        replay=Path(folder)/'replay.sqlite'
        for i,event in enumerate(events):
            file=Path(folder)/f'{i}.json';file.write_bytes(event['raw'])
            {'baseline':import_baseline,'material':import_material,'contract':import_contract,
             'work':import_work,'survey':import_survey,'recheck':import_recheck}[event['kind']](replay,file)
        rebuilt=report(replay)
    return {'status':'matched' if all(original[k]==rebuilt[k] for k in KEYS) else 'divergent',
            'event_count':len(events),
            'differences':[k for k in KEYS if original[k]!=rebuilt[k]]}
