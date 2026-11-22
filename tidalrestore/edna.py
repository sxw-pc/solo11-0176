"""Capture eDNA library results without assigning species occupancy."""
from contextlib import closing
from .store import connect,evidence

def import_sample(path,file):
    with closing(connect(path)) as db,db:
        doc,fresh=evidence(db,file,'edna')
        if not fresh:return {'id':doc['id'],'replayed':True}
        if not db.execute('SELECT 1 FROM plot WHERE id=?',(doc['plot_id'],)).fetchone():
            raise ValueError('unknown_plot')
        if not doc.get('site') or not doc.get('batch_id') or not doc.get('barcode') or not doc.get('collected_at'):
            raise ValueError('sample_identity_invalid')
        reads,blank=doc.get('read_count'),doc.get('blank_reads')
        if not isinstance(reads,int) or not isinstance(blank,int) or reads<0 or blank<0:
            raise ValueError('sample_counts_invalid')
        db.execute('INSERT INTO edna_sample VALUES(?,?,?,?,?,?,?,?,?)',
                   (doc['sample_id'],doc['plot_id'],doc['site'],doc['collected_at'],doc['batch_id'],doc['barcode'],reads,blank,doc['id']))
        return {'sample_id':doc['sample_id'],'status':'captured'}
