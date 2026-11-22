from contextlib import closing
from .store import connect,evidence,log

def import_contract(path,file):
    with closing(connect(path)) as db,db:
        doc,fresh=evidence(db,file,'contract')
        if not fresh:return {'id':doc['id'],'replayed':True}
        plot=db.execute('SELECT * FROM plot WHERE id=?',(doc['plot_id'],)).fetchone()
        if not plot:raise ValueError('unknown_plot')
        if doc['planned_area']<=0 or doc['planned_area']>plot['area'] or doc['window_start']>=doc['window_end']:
            raise ValueError('contract_area_or_window')
        if doc['species'] not in ('native-grass','native-shell'):raise ValueError('non_native_material')
        db.execute('INSERT INTO work_contract VALUES(?,?,?,?,?,?,?)',(doc['contract_id'],doc['plot_id'],doc['species'],doc['planned_area'],doc['window_start'],doc['window_end'],doc['contractor']))
        log(db,doc['contract_id'],'signed','native_material_and_area',doc['id'])
        return {'contract_id':doc['contract_id'],'status':'signed'}

def import_work(path,file):
    with closing(connect(path)) as db,db:
        doc,fresh=evidence(db,file,'work')
        if not fresh:return {'id':doc['id'],'replayed':True}
        contract=db.execute('SELECT * FROM work_contract WHERE id=?',(doc['contract_id'],)).fetchone()
        if not contract:raise ValueError('unknown_contract')
        if doc['event_type'] not in ('install','storm_damage') or doc['area']<=0:raise ValueError('invalid_work')
        if doc['event_type']=='install':
            if not contract['window_start']<=doc['at']<=contract['window_end']:raise ValueError('outside_tide_window')
            installed=db.execute("SELECT COALESCE(SUM(area),0) FROM work_event WHERE contract_id=? AND event_type='install'",(doc['contract_id'],)).fetchone()[0]
            if installed+doc['area']>contract['planned_area']:raise ValueError('installed_area_exceeded')
        db.execute('INSERT INTO work_event VALUES(?,?,?,?,?,?)',(doc['event_id'],doc['contract_id'],doc['event_type'],doc['area'],doc['at'],doc['id']))
        if doc['event_type']=='storm_damage':
            db.execute("UPDATE acceptance SET status='rework',reason='storm_damage',version=version+1 WHERE plot_id=?",(contract['plot_id'],))
        log(db,contract['plot_id'],doc['event_type'],'area_'+str(doc['area']),doc['id'])
        return {'event_id':doc['event_id'],'status':doc['event_type']}
