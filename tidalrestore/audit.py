import tempfile
from pathlib import Path
from contextlib import closing
from .store import connect,report
from .baseline import import_baseline
from .works import import_contract,import_work
from .survey import import_survey
from .material import import_material
from .salinity import import_logger,summary as salinity_summary,pending as salinity_pending

FIELD_IMPORTS={'baseline':import_baseline,'material':import_material,'contract':import_contract,
               'work':import_work,'survey':import_survey}


def _salinity_state(db):
    """Snapshot of every protocol table, independent of row insertion time."""
    tables={}
    for name in ('salinity_source','salinity_sample','salinity_manifest','salinity_piece',
                 'salinity_conflict','salinity_publication'):
        if name=='salinity_conflict':
            # The sealed rowid/time differ between a straight import and a
            # replay; identity of the sealed packet is what matters.
            tables[name]=[dict(r) for r in db.execute(
                'SELECT scope,object_key,piece_index,existing_sha,received_sha,reason '
                'FROM salinity_conflict ORDER BY scope,object_key,piece_index,received_sha')]
        elif name=='salinity_source':
            tables[name]=[dict(r) for r in db.execute(
                'SELECT source_key,source_id,kind,status,sha,group_key,manifest_key,revision,'
                'device_id,seq_start,seq_end FROM salinity_source ORDER BY source_key')]
        elif name=='salinity_manifest':
            tables[name]=[dict(r) for r in db.execute(
                'SELECT manifest_key,manifest_id,revision,device_id,sha,total_sha,calibration_piece,'
                'calibration_from,calibration_to,supersedes_key,supersedes_sha,signature_ok,'
                'revocation_ok,status,reason FROM salinity_manifest ORDER BY manifest_key')]
        elif name=='salinity_piece':
            tables[name]=[dict(r) for r in db.execute(
                'SELECT manifest_key,piece_index,sha,device_id,signature_ok,status '
                'FROM salinity_piece ORDER BY manifest_key,piece_index,sha')]
        elif name=='salinity_publication':
            tables[name]=[dict(r) for r in db.execute(
                'SELECT group_key,source_key,manifest_key,revision,status '
                'FROM salinity_publication ORDER BY id')]
        else:
            tables[name]=[dict(r) for r in db.execute(
                'SELECT source_key,source_id,seq,station,at,conductivity,tide '
                'FROM salinity_sample ORDER BY source_key,seq,station')]
    return tables


def _history_same(left,right):
    keys=('group_key','source_key','manifest_key','revision','status','stations')
    norm=lambda rows:[{k:r.get(k) for k in keys} for r in rows]
    return norm(left)==norm(right)


def _history(db):
    rows=[]
    for pub in db.execute('SELECT * FROM salinity_publication ORDER BY id'):
        entry=dict(pub)
        entry['stations']=[dict(r) for r in db.execute(
            'SELECT station,MAX(conductivity) AS peak,COUNT(*) AS count FROM salinity_sample '
            'WHERE source_key=? GROUP BY station ORDER BY station',(pub['source_key'],))]
        rows.append(entry)
    return rows


def audit(path):
    with closing(connect(path)) as db:
        events=[dict(x) for x in db.execute('SELECT kind,raw FROM evidence ORDER BY seq')]
        packets=[dict(r) for r in db.execute('SELECT * FROM salinity_receipt ORDER BY id')]
        original_state=_salinity_state(db)
        original_history=_history(db)
        summary=salinity_summary(path)
        pending=salinity_pending(path)
    original=report(path)
    with tempfile.TemporaryDirectory(prefix='tidal-audit-') as folder:
        replay=Path(folder)/'replay.sqlite'
        for i,event in enumerate(events):
            file=Path(folder)/f'{i}.json';file.write_bytes(event['raw'])
            FIELD_IMPORTS[event['kind']](replay,file)
        # Rebuild salinity from the raw received packets, in receipt order.
        for i,packet in enumerate(packets):
            if packet['raw']:
                file=Path(folder)/f'salinity-{i}.json'
                file.write_bytes(packet['raw'])
                import_logger(replay,file,collector=packet['collector'] or 'logger',
                              at=packet['received_at'])
        # Packets rejected at receipt time leave no receipt row; their sealed
        # raw bytes are the only record, so replay those too.  They must again
        # be rejected and sealed, never silently accepted.
        with closing(connect(path)) as live:
            conflicts=[dict(r) for r in live.execute(
                'SELECT id,raw FROM salinity_conflict ORDER BY id')]
        for i,conflict in enumerate(conflicts):
            if conflict['raw']:
                file=Path(folder)/f'conflict-{i}.json'
                file.write_bytes(conflict['raw'])
                try:
                    import_logger(replay,file,collector='audit')
                except ValueError:
                    pass
        with closing(connect(replay)) as db:
            rebuilt_state=_salinity_state(db)
            rebuilt_history=_history(db)
        rebuilt_summary=salinity_summary(replay)
        rebuilt_pending=salinity_pending(replay)
        rebuilt_report=report(replay)
    keys=('plots','baseline','baseline_disputes','materials','contracts','works','work_cells',
          'surveys','acceptance','acceptance_history','decisions')
    field_diff=[k for k in keys if original[k]!=rebuilt_report[k]]
    salinity_diff=[name for name in original_state if original_state[name]!=rebuilt_state[name]]
    checks={'summary':summary==rebuilt_summary,'pending':pending==rebuilt_pending,
            'history':_history_same(original_history,rebuilt_history)}
    ok=not field_diff and not salinity_diff and all(checks.values())
    return {'status':'matched' if ok else 'divergent','event_count':len(events),
            'salinity_packet_count':len(packets),'differences':field_diff,
            'salinity_differences':salinity_diff,'salinity_checks':checks}
