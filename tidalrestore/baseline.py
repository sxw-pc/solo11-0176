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
        existing=db.execute('SELECT * FROM plot WHERE id=?',(doc['plot_id'],)).fetchone()
        if existing:
            old={r['tier']:r for r in db.execute('SELECT * FROM baseline_transect WHERE plot_id=?',(doc['plot_id'],))}
            changed=[t for t in tiers if t['tier'] in old and t['coordinate']!=old[t['tier']]['coordinate']]
            if not changed:raise ValueError('baseline_revision_conflict')
            for t in changed:
                db.execute('INSERT INTO baseline_dispute VALUES(?,?,?,?,?)',
                           (doc['id']+'-'+t['tier'],doc['plot_id'],t['coordinate'],'same_tier_coordinate_conflict',doc['id']))
            db.execute("UPDATE plot SET baseline_status='frozen' WHERE id=?",(doc['plot_id'],))
            db.execute("UPDATE acceptance SET status='rework',reason='baseline_coordinate_conflict',version=version+1 WHERE plot_id=?",(doc['plot_id'],))
            current=db.execute('SELECT * FROM acceptance WHERE plot_id=?',(doc['plot_id'],)).fetchone()
            if current:
                db.execute('INSERT INTO acceptance_history VALUES(?,?,?,?,?,?)',
                           (doc['plot_id'],current['version'],'rework',current['weighted_survival'],'baseline_coordinate_conflict',doc['id']))
            log(db,doc['plot_id'],'frozen','same_tier_coordinate_conflict',doc['id'])
            return {'plot_id':doc['plot_id'],'status':'frozen','conflicts':len(changed)}
        db.execute('INSERT INTO plot VALUES(?,?,?,?)',(doc['plot_id'],doc['area'],doc['season'],'confirmed'))
        for t in tiers:db.execute('INSERT INTO baseline_transect VALUES(?,?,?,?,?,?)',(t['id'],doc['plot_id'],t['tier'],t['coordinate'],t['cover'],doc['id']))
        log(db,doc['plot_id'],'confirmed','three_tidal_tiers',doc['id'])
        return {'plot_id':doc['plot_id'],'status':'confirmed'}
