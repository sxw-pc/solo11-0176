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
        vals=[doc['high_alive'],doc['mid_alive'],doc['low_alive']]
        if any(not isinstance(x,int) or not 0<=x<=100 for x in vals):raise ValueError('invalid_survival')
        installed=db.execute("SELECT COALESCE(SUM(area),0) FROM work_event WHERE contract_id=? AND event_type='install'",(contract['id'],)).fetchone()[0]
        damage=db.execute("SELECT COALESCE(SUM(area),0) FROM work_event WHERE contract_id=? AND event_type='storm_damage'",(contract['id'],)).fetchone()[0]
        score=(vals[0]*2+vals[1]*3+vals[2]*5)//10
        status='accepted' if installed>=contract['planned_area'] and damage==0 and score>=70 else 'rework'
        reason='survival_and_area' if status=='accepted' else ('storm_damage' if damage else 'insufficient_survival_or_area')
        db.execute('INSERT INTO survey VALUES(?,?,?,?,?,?,?,?)',(doc['survey_id'],doc['plot_id'],doc['season'],doc['observer'],*vals,doc['id']))
        old=db.execute('SELECT version FROM acceptance WHERE plot_id=?',(doc['plot_id'],)).fetchone();version=(old['version']+1) if old else 1
        db.execute('INSERT INTO acceptance VALUES(?,?,?,?,?) ON CONFLICT(plot_id) DO UPDATE SET status=excluded.status,weighted_survival=excluded.weighted_survival,reason=excluded.reason,version=excluded.version',(doc['plot_id'],status,score,reason,version))
        log(db,doc['plot_id'],status,reason,doc['id'])
        return {'plot_id':doc['plot_id'],'status':status,'weighted_survival':score,'version':version}
