"""Store offline blue-carbon chamber readings before credit accounting."""
from contextlib import closing
from .store import connect,evidence

def import_flux(path,file):
    with closing(connect(path)) as db,db:
        doc,fresh=evidence(db,file,'flux')
        if not fresh:return {'id':doc['id'],'replayed':True}
        if not db.execute('SELECT 1 FROM plot WHERE id=?',(doc['plot_id'],)).fetchone():
            raise ValueError('unknown_plot')
        if doc['tide'] not in ('high','mid','low') or not doc['chamber_id'] or not doc['captured_at']:
            raise ValueError('flux_identity_invalid')
        if not isinstance(doc['co2_mg'],int) or not isinstance(doc['ch4_mg'],int):
            raise ValueError('flux_value_invalid')
        db.execute('INSERT INTO chamber_flux VALUES(?,?,?,?,?,?,?,?)',
                   (doc['flux_id'],doc['plot_id'],doc['chamber_id'],doc['captured_at'],doc['tide'],doc['co2_mg'],doc['ch4_mg'],doc['id']))
        return {'flux_id':doc['flux_id'],'status':'captured'}
