from contextlib import closing
from .store import connect,evidence,log

def import_survey(path,file):
    with closing(connect(path)) as db,db:
        doc,fresh=evidence(db,file,'survey')
        if not fresh:return {'id':doc['id'],'replayed':True}
        plot=db.execute('SELECT * FROM plot WHERE id=?',(doc['plot_id'],)).fetchone()
        if not plot:raise ValueError('unknown_plot')
        contract=db.execute('SELECT * FROM work_contract WHERE plot_id=?',(doc['plot_id'],)).fetchone()
        if not contract:raise ValueError('no_work_contract')
        if doc['observer']==contract['contractor']:raise ValueError('survey_not_independent')
        if doc['season']!=plot['season']:raise ValueError('season_not_comparable')
        if doc['at']<=contract['window_end']:raise ValueError('survey_before_work_end')
        latest=db.execute('SELECT MAX(observed_at) FROM survey WHERE plot_id=?',(doc['plot_id'],)).fetchone()[0]
        if latest and doc['at']<=latest:raise ValueError('survey_time_not_latest')
        baseline={r['tier']:r['coordinate'] for r in db.execute('SELECT tier,coordinate FROM baseline_transect WHERE plot_id=?',(doc['plot_id'],))}
        if doc.get('sample_coords')!=baseline:raise ValueError('survey_coverage_mismatch')
        vals=[doc['high_alive'],doc['mid_alive'],doc['low_alive']]
        if any(not isinstance(x,int) or not 0<=x<=100 for x in vals):raise ValueError('invalid_survival')
        cells=list(db.execute('SELECT tier,area,status FROM work_cell WHERE contract_id=?',(contract['id'],)))
        installed=sum(x['area'] for x in cells)
        damage=sum(x['area'] for x in cells if x['status']=='damaged')
        by_tier=dict(zip(('high','mid','low'),vals))
        score=sum(x['area']*by_tier[x['tier']] for x in cells if x['status']=='active')//max(1,contract['planned_area'])
        status='accepted' if plot['baseline_status']=='confirmed' and installed>=contract['planned_area'] and damage==0 and score>=70 else 'rework'
        reason='survival_and_area' if status=='accepted' else ('storm_damage' if damage else 'insufficient_survival_or_area')
        db.execute('INSERT INTO survey VALUES(?,?,?,?,?,?,?,?,?)',(doc['survey_id'],doc['plot_id'],doc['season'],doc['observer'],*vals,doc['at'],doc['id']))
        old=db.execute('SELECT version FROM acceptance WHERE plot_id=?',(doc['plot_id'],)).fetchone();version=(old['version']+1) if old else 1
        db.execute('INSERT INTO acceptance VALUES(?,?,?,?,?) ON CONFLICT(plot_id) DO UPDATE SET status=excluded.status,weighted_survival=excluded.weighted_survival,reason=excluded.reason,version=excluded.version',(doc['plot_id'],status,score,reason,version))
        db.execute('INSERT INTO acceptance_history VALUES(?,?,?,?,?,?)',(doc['plot_id'],version,status,score,reason,doc['id']))
        log(db,doc['plot_id'],status,reason,doc['id'])
        return {'plot_id':doc['plot_id'],'status':status,'weighted_survival':score,'version':version}
