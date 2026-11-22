"""Independent native planting batch provenance and usable quantity."""
from contextlib import closing
from .store import connect,evidence,log

def import_material(path,file):
    with closing(connect(path)) as db,db:
        doc,fresh=evidence(db,file,'material')
        if not fresh:return {'id':doc['id'],'replayed':True}
        if not doc['batch_id'] or doc['quantity']<=0 or not doc['origin'] or not doc['valid_until']:
            raise ValueError('invalid_material_batch')
        status='approved' if doc['species'] in ('native-grass','native-shell') and doc['origin']=='local' else 'quarantined'
        db.execute('INSERT INTO material_batch VALUES(?,?,?,?,?,?,?)',
                   (doc['batch_id'],doc['species'],doc['origin'],doc['quantity'],doc['valid_until'],status,doc['id']))
        log(db,doc['batch_id'],status,'native_origin' if status=='approved' else 'non_native_origin',doc['id'])
        return {'batch_id':doc['batch_id'],'status':status}
