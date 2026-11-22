from contextlib import closing
from .store import connect,evidence,log,update_acceptance

def current_baseline(db,plot_id):
    return {r['tier']:r['coordinate'] for r in db.execute('SELECT tier,coordinate FROM baseline_transect WHERE plot_id=?',(plot_id,))}

def import_contract(path,file):
    with closing(connect(path)) as db,db:
        doc,fresh=evidence(db,file,'contract')
        if not fresh:return {'id':doc['id'],'replayed':True}
        plot=db.execute('SELECT * FROM plot WHERE id=?',(doc['plot_id'],)).fetchone()
        if not plot:raise ValueError('unknown_plot')
        if plot['baseline_status']!='confirmed':raise ValueError('baseline_frozen')
        if doc['planned_area']<=0 or doc['planned_area']>plot['area'] or doc['window_start']>=doc['window_end']:
            raise ValueError('contract_area_or_window')
        if doc['repair_start']>=doc['repair_end'] or doc['repair_start']<=doc['window_end']:
            raise ValueError('repair_window_invalid')
        if doc['species'] not in ('native-grass','native-shell'):raise ValueError('non_native_material')
        db.execute('INSERT INTO work_contract VALUES(?,?,?,?,?,?,?,?,?)',(doc['contract_id'],doc['plot_id'],doc['species'],doc['planned_area'],doc['window_start'],doc['window_end'],doc['repair_start'],doc['repair_end'],doc['contractor']))
        log(db,doc['contract_id'],'signed','native_material_and_area',doc['id'])
        return {'contract_id':doc['contract_id'],'status':'signed'}

def import_work(path,file):
    with closing(connect(path)) as db,db:
        doc,fresh=evidence(db,file,'work')
        if not fresh:return {'id':doc['id'],'replayed':True}
        contract=db.execute('SELECT * FROM work_contract WHERE id=?',(doc['contract_id'],)).fetchone()
        if not contract:raise ValueError('unknown_contract')
        if doc['event_type'] not in ('install','storm_damage','replant') or doc['area']<=0:raise ValueError('invalid_work')
        if doc['event_type'] in ('install','replant'):
            start,end=(contract['window_start'],contract['window_end']) if doc['event_type']=='install' else (contract['repair_start'],contract['repair_end'])
            if not start<=doc['at']<=end:raise ValueError('outside_tide_window')
            material=db.execute('SELECT * FROM material_batch WHERE id=?',(doc['material_id'],)).fetchone()
            if not material or material['status']!='approved' or material['species']!=contract['material_species'] or material['valid_until']<doc['at']:
                raise ValueError('material_not_eligible')
            used=db.execute("SELECT COALESCE(SUM(area),0) FROM work_cell WHERE material_id=? AND status!='damaged'",(doc['material_id'],)).fetchone()[0]
            if used+doc['area']>material['quantity']:raise ValueError('material_quantity_exceeded')
            cells=doc['cells']
            if not cells or sum(x['area'] for x in cells)!=doc['area'] or any(x['tier'] not in ('high','mid','low') or x['area']<=0 for x in cells):
                raise ValueError('invalid_work_cells')
            if doc['event_type']=='install':
                installed=db.execute("SELECT COALESCE(SUM(area),0) FROM work_cell WHERE contract_id=?",(doc['contract_id'],)).fetchone()[0]
                if installed+doc['area']>contract['planned_area']:raise ValueError('installed_area_exceeded')
                anchors=current_baseline(db,contract['plot_id'])
                for cell in cells:
                    if db.execute('SELECT 1 FROM work_cell WHERE id=?',(cell['id'],)).fetchone():
                        raise ValueError('work_cell_overlap')
                    db.execute('INSERT INTO work_cell VALUES(?,?,?,?,?,?,?,?)',
                               (cell['id'],doc['contract_id'],cell['tier'],cell['area'],doc['material_id'],'active',doc['id'],anchors[cell['tier']]))
            else:
                for cell in cells:
                    old=db.execute('SELECT * FROM work_cell WHERE id=? AND contract_id=?',(cell['id'],doc['contract_id'])).fetchone()
                    if not old or old['status']!='damaged' or old['area']!=cell['area']:raise ValueError('replant_cell_mismatch')
                    db.execute("UPDATE work_cell SET status='active',material_id=?,source_id=? WHERE id=?",(doc['material_id'],doc['id'],cell['id']))
        else:
            cells=doc.get('cells',[])
            if not cells:raise ValueError('storm_cells_required')
            total=0
            for cell_id in cells:
                old=db.execute('SELECT * FROM work_cell WHERE id=? AND contract_id=?',(cell_id,doc['contract_id'])).fetchone()
                if not old or old['status']!='active':raise ValueError('storm_cell_not_active')
                total+=old['area']
            if total!=doc['area']:raise ValueError('storm_area_mismatch')
            for cell_id in cells:db.execute("UPDATE work_cell SET status='damaged',source_id=? WHERE id=?",(doc['id'],cell_id))
        db.execute('INSERT INTO work_event VALUES(?,?,?,?,?,?)',(doc['event_id'],doc['contract_id'],doc['event_type'],doc['area'],doc['at'],doc['id']))
        if doc['event_type']=='storm_damage' and contract['plot_id']:
            current=db.execute('SELECT * FROM acceptance WHERE plot_id=?',(contract['plot_id'],)).fetchone()
            update_acceptance(db,contract['plot_id'],'rework',current['weighted_survival'] if current else 0,
                              'storm_damage',doc['id'],create=False)
        log(db,contract['plot_id'],doc['event_type'],'area_'+str(doc['area']),doc['id'])
        return {'event_id':doc['event_id'],'status':doc['event_type']}
