"""Capture camera observations of floating debris without tracking identity."""
from contextlib import closing
from .store import connect,evidence

def import_observation(path,file):
    with closing(connect(path)) as db,db:
        doc,fresh=evidence(db,file,'debris')
        if not fresh:return {'id':doc['id'],'replayed':True}
        if not db.execute('SELECT 1 FROM plot WHERE id=?',(doc['plot_id'],)).fetchone():
            raise ValueError('unknown_plot')
        if not doc.get('camera') or not doc.get('observed_at') or not doc.get('shape'):
            raise ValueError('debris_identity_invalid')
        if not isinstance(doc.get('position_m'),int) or not isinstance(doc.get('pieces'),int) or doc['pieces']<1:
            raise ValueError('debris_count_invalid')
        db.execute('INSERT INTO debris_observation VALUES(?,?,?,?,?,?,?,?)',
                   (doc['obs_id'],doc['plot_id'],doc['camera'],doc['observed_at'],doc['position_m'],doc['shape'],doc['pieces'],doc['id']))
        return {'obs_id':doc['obs_id'],'status':'captured'}
