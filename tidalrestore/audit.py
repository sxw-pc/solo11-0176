import tempfile
from pathlib import Path
from contextlib import closing
from .store import connect,report
from .archive import read_cold_raw, ArchiveVerifyError, ArchiveError
from .baseline import import_baseline
from .works import import_contract,import_work
from .survey import import_survey
from .material import import_material

IMPORTERS={'baseline':import_baseline,'material':import_material,
           'contract':import_contract,'work':import_work,'survey':import_survey}

def audit(path, archive_root=None):
    """Replay every original by seq and compare the eleven business tables.

    Originals may live in the hot evidence.raw column or in a verified cold
    package. A tampered/missing package is reported per source_id instead of a
    bare divergent result, because divergence alone cannot explain unreadable
    evidence. Report contents never move with the bytes.
    """
    errors=[];cold_count=0
    with closing(connect(path)) as db:
        events=[dict(x) for x in db.execute('SELECT id,kind,raw,sha FROM evidence ORDER BY seq')]
        cache={}
        loaded=[]
        for event in events:
            if event['raw'] is not None:
                blob=event['raw']
            else:
                cold_count+=1
                try:
                    blob=read_cold_raw(db,archive_root,event['id'],cache)
                except ArchiveVerifyError as e:
                    errors.append({'source_id':event['id'],'error':str(e)});continue
                except ArchiveError as e:
                    errors.append({'source_id':event['id'],'error':str(e)});continue
            loaded.append({'id':event['id'],'kind':event['kind'],'raw':blob})
    original=report(path)
    if errors:
        return {'status':'cold_evidence_failed','event_count':len(events),'errors':errors,
                'differences':[],'cold_count':cold_count}
    with tempfile.TemporaryDirectory(prefix='tidal-audit-') as folder:
        replay=Path(folder)/'replay.sqlite'
        for i,event in enumerate(loaded):
            file=Path(folder)/f'{i}.json';file.write_bytes(event['raw'])
            IMPORTERS[event['kind']](replay,file)
        rebuilt=report(replay)
    keys=('plots','baseline','baseline_disputes','materials','contracts','works','work_cells','surveys','acceptance','acceptance_history','decisions')
    return {'status':'matched' if all(original[k]==rebuilt[k] for k in keys) else 'divergent','event_count':len(events),
            'differences':[k for k in keys if original[k]!=rebuilt[k]],'cold_count':cold_count}
