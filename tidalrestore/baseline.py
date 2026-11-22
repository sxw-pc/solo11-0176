from contextlib import closing
from .store import connect,evidence,log,snapshot_acceptance,update_acceptance

def _validate(doc):
    if doc['area']<=0 or doc['season'] not in ('spring','autumn'):raise ValueError('invalid_plot')
    tiers=doc['transects']
    if {x['tier'] for x in tiers}!={'high','mid','low'} or len(tiers)!=3 or len({x['coordinate'] for x in tiers})!=3:
        raise ValueError('baseline_coverage')
    if any(not 0<=x['cover']<=100 for x in tiers):raise ValueError('invalid_cover')
    if any(not x.get('id') or not x.get('coordinate') for x in tiers):raise ValueError('baseline_coverage')
    return tiers

def import_baseline(path,file):
    with closing(connect(path)) as db,db:
        doc,fresh=evidence(db,file,'baseline')
        if not fresh:return {'id':doc['id'],'replayed':True}
        tiers=_validate(doc)
        existing=db.execute('SELECT * FROM plot WHERE id=?',(doc['plot_id'],)).fetchone()
        if not existing:
            previous=db.execute('SELECT 1 FROM baseline_revision WHERE plot_id=?',(doc['plot_id'],)).fetchone()
            if previous:raise ValueError('baseline_revision_conflict')
            db.execute('INSERT INTO plot VALUES(?,?,?,?)',(doc['plot_id'],doc['area'],doc['season'],'confirmed'))
            db.execute('INSERT INTO baseline_revision VALUES(?,?,?,?,?)',(doc['id']+'-r1',doc['plot_id'],1,doc['id'],'original'))
            for t in tiers:
                db.execute('INSERT INTO baseline_revision_transect VALUES(?,?,?,?)',(doc['id']+'-r1',t['tier'],t['coordinate'],t['cover']))
                db.execute('INSERT INTO baseline_transect VALUES(?,?,?,?,?,?)',(t['id'],doc['plot_id'],t['tier'],t['coordinate'],t['cover'],doc['id']))
            log(db,doc['plot_id'],'confirmed','three_tidal_tiers',doc['id'])
            return {'plot_id':doc['plot_id'],'status':'confirmed','revision':1}

        # A revision may not silently change the plot's ecological identity.
        if existing['area']!=doc['area'] or existing['season']!=doc['season']:
            raise ValueError('baseline_plot_mismatch')
        old={r['tier']:r for r in db.execute('SELECT * FROM baseline_transect WHERE plot_id=?',(doc['plot_id'],))}
        prior=[{r['tier']:(r['coordinate'],r['cover']) for r in db.execute(
                    'SELECT tier,coordinate,cover FROM baseline_revision_transect WHERE revision_id=?',(row['id'],))}
               for row in db.execute('SELECT id FROM baseline_revision WHERE plot_id=?',(doc['plot_id'],))]
        if any(sorted((t['tier'],t['coordinate'],t['cover']) for t in tiers)==
               sorted((tier,coord,cover) for tier,(coord,cover) in prior_row.items())
               for prior_row in prior):
            raise ValueError('duplicate_baseline_evidence')
        changed=[t for t in tiers if t['coordinate']!=old[t['tier']]['coordinate']]
        if not changed:raise ValueError('baseline_revision_conflict')
        # A late file may still open a dispute; it can never replace the effective
        # coordinates on its own. Only an independent, clock-checked recheck can
        # adopt a revision (see review.py).
        number=db.execute('SELECT COALESCE(MAX(revision),0)+1 FROM baseline_revision WHERE plot_id=?',(doc['plot_id'],)).fetchone()[0]
        revision_id=f'{doc["id"]}-r{number}'
        db.execute('INSERT INTO baseline_revision VALUES(?,?,?,?,?)',(revision_id,doc['plot_id'],number,doc['id'],'disputed'))
        for t in tiers:
            db.execute('INSERT INTO baseline_revision_transect VALUES(?,?,?,?)',(revision_id,t['tier'],t['coordinate'],t['cover']))
        for t in changed:
            dispute_id=f'{doc["id"]}-{t["tier"]}'
            db.execute('INSERT INTO baseline_dispute VALUES(?,?,?,?,?)',
                       (dispute_id,doc['plot_id'],t['coordinate'],'same_tier_coordinate_conflict',doc['id']))
            db.execute('INSERT INTO dispute_state VALUES(?,?,?,?) ON CONFLICT(plot_id,tier) DO UPDATE SET dispute_id=excluded.dispute_id,status=excluded.status',
                       (doc['plot_id'],t['tier'],dispute_id,'disputed'))
        frozen=snapshot_acceptance(db,doc['plot_id'],doc['id'])
        db.execute("UPDATE plot SET baseline_status='frozen' WHERE id=?",(doc['plot_id'],))
        version=update_acceptance(db,doc['plot_id'],'rework',
                                  frozen['weighted_survival'] if frozen else 0,
                                  'baseline_coordinate_conflict',doc['id'],create=False)
        log(db,doc['plot_id'],'frozen','same_tier_coordinate_conflict',doc['id'])
        result={'plot_id':doc['plot_id'],'status':'frozen','conflicts':len(changed),
                'revision':number,'disputed_tiers':[t['tier'] for t in changed],
                'acceptance_version':version}
        # A later or out-of-order file must be adjudicated against rechecks that
        # already arrived; the decision is recomputed from evidence, not arrival.
        from .review import adjudicate
        decisions=adjudicate(db,doc['plot_id'])
        if decisions:result['decisions']=decisions
        return result
