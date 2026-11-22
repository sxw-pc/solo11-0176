import hashlib,tempfile
from pathlib import Path
from contextlib import closing
from .store import connect,report
from .baseline import import_baseline
from .works import import_contract,import_work
from .survey import import_survey
from .material import import_material
from .shoreline import import_profile,import_tide,import_bundle,import_budget,import_verification

KEYS=('plots','baseline','baseline_disputes','materials','contracts','works','work_cells','surveys',
      'acceptance','acceptance_history','decisions','shore_profiles','tide_records','shore_budgets',
      'budget_plots','budget_routes','budget_layers','budget_resurvey')

def audit(path):
    with closing(connect(path)) as db:
        events=[dict(x) for x in db.execute('SELECT id,kind,sha,raw FROM evidence ORDER BY seq')]
    # Evidence bytes are integrity-checked before replay so a modified source
    # document is located by id, not just inferred from divergent output.
    tampered=[e['id'] for e in events if hashlib.sha256(e['raw']).hexdigest()!=e['sha']]
    original=report(path)
    dispatch={'baseline':import_baseline,'material':import_material,'contract':import_contract,
              'work':import_work,'survey':import_survey,'shore_profile':import_profile,
              'tide_record':import_tide,'shore_bundle':import_bundle,'shore_budget':import_budget,
              'shore_verification':import_verification}
    replay_errors=[]
    with tempfile.TemporaryDirectory(prefix='tidal-audit-') as folder:
        replay=Path(folder)/'replay.sqlite'
        for i,event in enumerate(events):
            file=Path(folder)/f'{i}.json';file.write_bytes(event['raw'])
            try:
                dispatch[event['kind']](replay,file)
            except Exception as exc:
                replay_errors.append({'seq':i+1,'id':event['id'],'kind':event['kind'],'error':str(exc)})
        rebuilt=report(replay)
    differences=[k for k in KEYS if original[k]!=rebuilt[k]]
    status='matched' if not differences and not tampered and not replay_errors else 'divergent'
    return {'status':status,'event_count':len(events),'differences':differences,
            'tampered_evidence':tampered,'replay_errors':replay_errors}
