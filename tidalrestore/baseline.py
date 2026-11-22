from contextlib import closing
from .store import connect,evidence,log

def import_baseline(path,file):
    with closing(connect(path)) as db,db:
        doc,fresh=evidence(db,file,'baseline')
        if not fresh:return {'id':doc['id'],'replayed':True}
        if doc['area']<=0 or doc['season'] not in ('spring','autumn'):raise ValueError('invalid_plot')
        tiers=doc['transects']
        if {x['tier'] for x in tiers}!={'high','mid','low'} or len(tiers)!=3 or len({x['coordinate'] for x in tiers})!=3:
            raise ValueError('baseline_coverage')
        if any(not 0<=x['cover']<=100 for x in tiers):raise ValueError('invalid_cover')
        if db.execute('SELECT 1 FROM plot WHERE id=?',(doc['plot_id'],)).fetchone():raise ValueError('baseline_revision_conflict')
        db.execute('INSERT INTO plot VALUES(?,?,?,?)',(doc['plot_id'],doc['area'],doc['season'],'confirmed'))
        for t in tiers:db.execute('INSERT INTO baseline_transect VALUES(?,?,?,?,?,?)',(t['id'],doc['plot_id'],t['tier'],t['coordinate'],t['cover'],doc['id']))
        log(db,doc['plot_id'],'confirmed','three_tidal_tiers',doc['id'])
        return {'plot_id':doc['plot_id'],'status':'confirmed'}
