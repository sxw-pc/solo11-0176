"""Capture offline cross-shore elevation profiles and derive sediment budgets.

Profiles are stored raw. A budget unifies the tidal datum of two survey
periods, registers profiles alongshore, and propagates instrument error as
shared interval variables: one variable per (device, voyage) and error type,
never an independent sum over points. Deposition on a shared plot boundary is
credited once as a transfer interval; an unmeasured offshore exit stays an
explicit unknown flux and is never forced to zero. Mutually exclusive field
verification routes stay pending until a synthetic resurvey arrives.
"""
import json,math,tempfile
from pathlib import Path
from contextlib import closing
from datetime import datetime
from .store import connect,evidence,log

META_FIELDS=('period','voyage','alongshore_m','coverage_m','clock_error_s','vertical_error_cm','control_covariance_cm2')

def _ts(text):
    return datetime.fromisoformat(text.replace('Z','+00:00')).timestamp()

def _round(x):
    return round(float(x),6)

def _num(x):
    return isinstance(x,(int,float)) and not isinstance(x,bool)

def import_profile(path,file):
    with closing(connect(path)) as db,db:
        doc,fresh=evidence(db,file,'shore_profile')
        if not fresh:return {'id':doc['id'],'replayed':True}
        if not db.execute('SELECT 1 FROM plot WHERE id=?',(doc['plot_id'],)).fetchone():
            raise ValueError('unknown_plot')
        pts=doc.get('points',[])
        if len(pts)<2 or any(not _num(p.get('distance_m')) or not _num(p.get('elevation_cm')) for p in pts):
            raise ValueError('profile_points_invalid')
        if any(pts[i]['distance_m']>=pts[i+1]['distance_m'] for i in range(len(pts)-1)):
            raise ValueError('profile_order_invalid')
        present=[f for f in META_FIELDS if f in doc]
        if present:
            if len(present)!=len(META_FIELDS):raise ValueError('profile_metadata_incomplete')
            cov=doc['coverage_m']
            if not isinstance(cov,list) or len(cov)!=2 or not all(_num(x) for x in cov) or not cov[0]<cov[1]:
                raise ValueError('profile_coverage_invalid')
            if not cov[0]<=doc['alongshore_m']<=cov[1]:raise ValueError('profile_coverage_invalid')
            if not all(_num(doc[k]) and doc[k]>=0 for k in ('clock_error_s','vertical_error_cm','control_covariance_cm2')):
                raise ValueError('profile_error_invalid')
        cov=doc.get('coverage_m') or [None,None]
        db.execute('INSERT INTO shore_profile VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                   (doc['profile_id'],doc['plot_id'],doc['captured_at'],doc['device'],doc['water_level_cm'],
                    json.dumps(pts,separators=(',',':')),doc['id'],
                    doc.get('period'),doc.get('voyage'),doc.get('alongshore_m'),cov[0],cov[1],
                    doc.get('clock_error_s'),doc.get('vertical_error_cm'),doc.get('control_covariance_cm2')))
        return {'profile_id':doc['profile_id'],'status':'captured','point_count':len(pts)}

def import_tide(path,file):
    with closing(connect(path)) as db,db:
        doc,fresh=evidence(db,file,'tide_record')
        if not fresh:return {'id':doc['id'],'replayed':True}
        pts=doc.get('points',[])
        if len(pts)<2 or any(not isinstance(p.get('at'),str) or not _num(p.get('height_cm')) for p in pts):
            raise ValueError('tide_points_invalid')
        try:times=[_ts(p['at']) for p in pts]
        except ValueError:raise ValueError('tide_points_invalid')
        if any(times[i]>=times[i+1] for i in range(len(times)-1)):raise ValueError('tide_order_invalid')
        db.execute('INSERT INTO tide_record VALUES(?,?,?)',(doc['record_id'],json.dumps(pts,separators=(',',':')),doc['id']))
        return {'record_id':doc['record_id'],'status':'recorded','point_count':len(pts)}

def import_bundle(path,file):
    """Import a transport bundle: each sub-document becomes its own evidence row.

    Sub-documents import first through their normal single-document path; the
    bundle itself is registered as evidence last, so a failed sub-document
    leaves the same partial state as importing the files one by one.
    """
    raw=Path(file).read_bytes();doc=json.loads(raw)
    if doc.get('kind')!='shore_bundle' or not doc.get('id'):raise ValueError('evidence_kind_or_id')
    subs=doc.get('documents',[])
    if not subs:raise ValueError('bundle_empty')
    dispatch={'tide_record':import_tide,'shore_profile':import_profile,
              'shore_budget':import_budget,'shore_verification':import_verification}
    with tempfile.TemporaryDirectory(prefix='tidal-bundle-') as folder:
        for i,sub in enumerate(subs):
            fn=dispatch.get(sub.get('kind'))
            if not fn:raise ValueError('bundle_document_kind')
            tmp=Path(folder)/f'{i}.json'
            tmp.write_bytes(json.dumps(sub,sort_keys=True,separators=(',',':')).encode())
            fn(path,tmp)
    with closing(connect(path)) as db,db:
        _,fresh=evidence(db,file,'shore_bundle')
    if not fresh:return {'id':doc['id'],'replayed':True}
    return {'id':doc['id'],'imported':len(subs)}

def _tide_at(points,at):
    """Linear tide height and slope (cm/s) at a capture time."""
    t=_ts(at);times=[_ts(p['at']) for p in points]
    if t<times[0] or t>times[-1]:raise ValueError('tide_record_gap')
    for i in range(len(times)-1):
        if times[i]<=t<=times[i+1]:
            slope=(points[i+1]['height_cm']-points[i]['height_cm'])/(times[i+1]-times[i])
            return points[i]['height_cm']+slope*(t-times[i]),slope
    raise ValueError('tide_record_gap')

def _interp(pts,x):
    for i in range(len(pts)-1):
        x0,x1=pts[i]['distance_m'],pts[i+1]['distance_m']
        if x0<=x<=x1:
            z0,z1=pts[i]['elevation_cm'],pts[i+1]['elevation_cm']
            return z0+(z1-z0)*(x-x0)/(x1-x0)
    raise ValueError('profile_domain_gap')

def _raw_area(pts):
    return sum((pts[i+1]['distance_m']-pts[i]['distance_m'])*(pts[i]['elevation_cm']+pts[i+1]['elevation_cm'])/2
               for i in range(len(pts)-1))

def _pair_area_change(pts1,water1,pts2,water2):
    """Datum-corrected area change (cm*m) over the common cross-shore domain.

    Both profiles are piecewise linear, so integrating their difference on the
    union of breakpoints is exact for non-uniform point spacing.
    """
    lo=max(pts1[0]['distance_m'],pts2[0]['distance_m'])
    hi=min(pts1[-1]['distance_m'],pts2[-1]['distance_m'])
    if hi<=lo:return None,0.0
    xs=sorted({lo,hi}|{p['distance_m'] for p in pts1 if lo<p['distance_m']<hi}
                     |{p['distance_m'] for p in pts2 if lo<p['distance_m']<hi})
    vals=[(_interp(pts2,x)+water2)-(_interp(pts1,x)+water1) for x in xs]
    area=sum((xs[i+1]-xs[i])*(vals[i]+vals[i+1])/2 for i in range(len(xs)-1))
    return area,hi-lo

def _load_profile(db,prof_id):
    row=db.execute('SELECT * FROM shore_profile WHERE id=?',(prof_id,)).fetchone()
    if not row:raise ValueError('unknown_profile')
    if row['period'] is None:raise ValueError('profile_metadata_incomplete')
    return row

def _compute_budget(db,doc):
    """Derive every budget quantity from stored profiles and the tide record.

    Error terms are affine in shared variables keyed (device|voyage|kind), so a
    systematic offset common to one survey voyage widens each affected volume
    once, jointly across plots; it is never added point by point.
    """
    tide=db.execute('SELECT * FROM tide_record WHERE id=?',(doc['tide_record_id'],)).fetchone()
    if not tide:raise ValueError('unknown_tide_record')
    tide_pts=json.loads(tide['points_json'])
    periods=doc.get('periods',[])
    if len(periods)!=2:raise ValueError('periods_invalid')
    t1,t2=periods
    plots=doc['plots']
    spans=[]
    for p in plots:
        if not db.execute('SELECT 1 FROM plot WHERE id=?',(p['plot_id'],)).fetchone():raise ValueError('unknown_plot')
        span=p['alongshore_m']
        if not(isinstance(span,list) and len(span)==2 and span[0]<span[1]):raise ValueError('plot_span_invalid')
        spans.append((span[0],span[1]))
    spans.sort()
    if any(spans[i][1]>spans[i+1][0] for i in range(len(spans)-1)):raise ValueError('plot_span_overlap')
    layers=[];resurvey=[];plot_comp={};used=set();groups={}
    def coeffs(row):
        key=(row['device'],row['voyage'])
        triple=(row['vertical_error_cm'],row['clock_error_s'],row['control_covariance_cm2'])
        if key in groups and groups[key]!=triple:raise ValueError('systematic_error_mismatch')
        groups[key]=triple
        height,slope=_tide_at(tide_pts,row['captured_at'])
        if abs(row['water_level_cm']-height)>5+abs(slope)*row['clock_error_s']:
            raise ValueError('water_level_mismatch')
        dev,voy=key
        return {f'{dev}|{voy}|vertical':row['vertical_error_cm'],
                f'{dev}|{voy}|clock':abs(slope)*row['clock_error_s'],
                f'{dev}|{voy}|control':math.sqrt(row['control_covariance_cm2'])},height,slope
    for p in plots:
        pid=p['plot_id'];span_lo,span_hi=p['alongshore_m']
        by_along={};reach=[]
        for period in (t1,t2):
            covs=[]
            for prof_id in p['profiles'][period]:
                row=_load_profile(db,prof_id)
                if row['plot_id']!=pid:raise ValueError('profile_plot_mismatch')
                if row['period']!=period:raise ValueError('profile_period_mismatch')
                if row['id'] in used:raise ValueError('profile_role_conflict')
                used.add(row['id'])
                if row['coverage_lo']<span_lo or row['coverage_hi']>span_hi:raise ValueError('coverage_outside_plot')
                covs.append((row['coverage_lo'],row['coverage_hi']))
                var,_,slope=coeffs(row)
                pts=json.loads(row['points_json'])
                reach.append(pts[-1]['distance_m'])
                raw=_raw_area(pts);span_x=pts[-1]['distance_m']-pts[0]['distance_m']
                layers.append(('profile_area',prof_id,{
                    'plot_id':pid,'period':period,'alongshore_m':row['alongshore_m'],
                    'coverage_m':[row['coverage_lo'],row['coverage_hi']],
                    'water_level_cm':row['water_level_cm'],'tide_slope_cm_s':_round(slope),
                    'raw_area_cm_m':_round(raw),'datum_area_cm_m':_round(raw+row['water_level_cm']*span_x),
                    'error_group':f"{row['device']}|{row['voyage']}",
                    'error_coeffs_cm':{k:_round(v) for k,v in var.items()}}))
                by_along.setdefault(row['alongshore_m'],{})[period]=(row,pts,var)
            covs.sort()
            if any(covs[i][1]>covs[i+1][0] for i in range(len(covs)-1)):raise ValueError('coverage_overlap')
            cursor=span_lo
            for lo_c,hi_c in covs:
                if lo_c>cursor:resurvey.append((pid,'alongshore',[cursor,lo_c],'alongshore_gap'))
                cursor=max(cursor,hi_c)
            if cursor<span_hi:resurvey.append((pid,'alongshore',[cursor,span_hi],'alongshore_gap'))
        center=0.0;varcoeff={}
        for along in sorted(by_along):
            pair=by_along[along]
            if t1 not in pair or t2 not in pair:
                resurvey.append((pid,'alongshore',[along,along],'period_unmatched'));continue
            r1,pts1,c1=pair[t1];r2,pts2,c2=pair[t2]
            change,dom=_pair_area_change(pts1,r1['water_level_cm'],pts2,r2['water_level_cm'])
            if change is None:
                resurvey.append((pid,'cross_shore',[along,along],'domain_gap'));continue
            w=min(r1['coverage_hi'],r2['coverage_hi'])-max(r1['coverage_lo'],r2['coverage_lo'])
            if w<=0:
                resurvey.append((pid,'alongshore',[along,along],'period_unmatched'));continue
            pair_vars={}
            for k,v in c2.items():pair_vars[k]=pair_vars.get(k,0.0)+dom*v
            for k,v in c1.items():pair_vars[k]=pair_vars.get(k,0.0)-dom*v
            center+=w*change/100
            for k,v in pair_vars.items():varcoeff[k]=varcoeff.get(k,0.0)+w*v/100
            layers.append(('profile_pair',f"{pid}@{'%g'%along}",{
                't1_profile':r1['id'],'t2_profile':r2['id'],'domain_m':_round(dom),
                'area_change_cm_m':_round(change),'weight_m':_round(w),
                'variable_coeffs':{k:_round(v) for k,v in pair_vars.items()}}))
        hw=sum(abs(v) for v in varcoeff.values())
        plot_comp[pid]={'change':(center-hw,center+hw),'vars':varcoeff,'fill_target':p['fill_target_m3']}
        layers.append(('plot_volume',pid,{
            'change_m3':[_round(center-hw),_round(center+hw)],'center_m3':_round(center),
            'half_width_m3':_round(hw),'shared_variables':{k:_round(v) for k,v in varcoeff.items()}}))
        if reach and min(reach)<p['offshore_m']:
            resurvey.append((pid,'cross_shore',[_round(min(reach)),p['offshore_m']],'offshore_exit_unmeasured'))
    boundaries={}
    for b in doc.get('shared_boundaries',[]):
        left,right=b['between']
        pl=next((p for p in plots if p['plot_id']==left),None)
        pr=next((p for p in plots if p['plot_id']==right),None)
        if not pl or not pr:raise ValueError('boundary_plot_missing')
        if not(pl['alongshore_m'][1]==b['at_m']==pr['alongshore_m'][0]):raise ValueError('boundary_mismatch')
        strip=[b['at_m']-b['strip_m']/2,b['at_m']+b['strip_m']/2]
        rb1=_load_profile(db,b['profiles'][t1]);rb2=_load_profile(db,b['profiles'][t2])
        for row in (rb1,rb2):
            if row['id'] in used:raise ValueError('profile_role_conflict')
            used.add(row['id'])
            if row['plot_id'] not in (left,right):raise ValueError('boundary_profile_plot')
        if rb1['period']!=t1 or rb2['period']!=t2:raise ValueError('profile_period_mismatch')
        if [rb1['coverage_lo'],rb1['coverage_hi']]!=strip or [rb2['coverage_lo'],rb2['coverage_hi']]!=strip:
            raise ValueError('boundary_coverage_mismatch')
        c1,_,_=coeffs(rb1);c2,_,_=coeffs(rb2)
        pts1=json.loads(rb1['points_json']);pts2=json.loads(rb2['points_json'])
        change,dom=_pair_area_change(pts1,rb1['water_level_cm'],pts2,rb2['water_level_cm'])
        if change is None:raise ValueError('profile_domain_gap')
        w=b['strip_m'];center=w*change/100;varc={}
        for k,v in c2.items():varc[k]=varc.get(k,0.0)+w*dom*v/100
        for k,v in c1.items():varc[k]=varc.get(k,0.0)-w*dom*v/100
        hw=sum(abs(v) for v in varc.values())
        measured=(center-hw,center+hw)
        cap=min(max(0.0,-plot_comp[left]['change'][0]),max(0.0,plot_comp[right]['change'][1]))
        transfer=(max(0.0,min(measured[0],cap)),max(0.0,min(measured[1],cap)))
        boundaries[b['boundary_id']]={'between':[left,right],'strip_m':b['strip_m'],
                                      'measured':measured,'transfer':transfer,'cap':cap}
        layers.append(('boundary',b['boundary_id'],{
            'between':[left,right],'strip_m':b['strip_m'],
            'measured_m3':[_round(measured[0]),_round(measured[1])],'cap_m3':_round(cap),
            'transfer_m3':[_round(transfer[0]),_round(transfer[1])],
            'variable_coeffs':{k:_round(v) for k,v in varc.items()}}))
    for p in plots:
        pid=p['plot_id'];change=plot_comp[pid]['change']
        t_in=t_out=(0.0,0.0)
        for bnd in boundaries.values():
            if bnd['between'][0]==pid:t_out=bnd['transfer']
            if bnd['between'][1]==pid:t_in=bnd['transfer']
        loss=(max(0.0,-change[1]),max(0.0,-change[0]))
        gain=(max(0.0,change[0]),max(0.0,change[1]))
        un_loss=(max(0.0,loss[0]-t_out[1]),max(0.0,loss[1]-t_out[0]))
        un_gain=(max(0.0,gain[0]-t_in[1]),max(0.0,gain[1]-t_in[0]))
        transfer=t_out if t_out[1] else t_in
        unattrib=un_loss if loss[1]>0 else un_gain
        plot_comp[pid].update({'transfer':transfer,'unattributable':unattrib,'loss':loss,'gain':gain})
        layers.append(('attribution',pid,{
            'transfer_in_m3':[_round(t_in[0]),_round(t_in[1])],
            'transfer_out_m3':[_round(t_out[0]),_round(t_out[1])],
            'unattributable_loss_m3':[_round(un_loss[0]),_round(un_loss[1])],
            'unattributable_gain_m3':[_round(un_gain[0]),_round(un_gain[1])]}))
    routes={}
    for r in doc.get('routes',[]):
        if not _num(r.get('claim_m3')) or r['claim_m3']<=0:raise ValueError('route_claim_invalid')
        routes[r['route_id']]={'target':r['target'],'claim':r['claim_m3'],
                               'scenario':_route_scenario(r,r['claim_m3'],doc,boundaries,plot_comp)}
    # Round every stored interval once so replayed derivations compare equal.
    for pid,c in plot_comp.items():
        c['change']=(_round(c['change'][0]),_round(c['change'][1]))
        c['transfer']=(_round(c['transfer'][0]),_round(c['transfer'][1]))
        c['unattributable']=(_round(c['unattributable'][0]),_round(c['unattributable'][1]))
        c['loss']=(_round(c['loss'][0]),_round(c['loss'][1]))
        c['gain']=(_round(c['gain'][0]),_round(c['gain'][1]))
    return {'plots':plot_comp,'boundaries':boundaries,'layers':layers,'resurvey':resurvey,'routes':routes}

def _route_scenario(route,amount,doc,boundaries,plot_comp):
    """One feasible budget under a verification route proving `amount` m3."""
    targets={p['plot_id']:p['fill_target_m3'] for p in doc['plots']}
    if route['target']=='shared_boundary':
        bnd=boundaries.get(route.get('boundary_id'))
        if not bnd:raise ValueError('route_target_unknown')
        left,right=bnd['between'];transfer=bnd['transfer']
        verified=min(amount,transfer[1])
        loss=plot_comp[left]['loss']
        purchases=dict(targets);purchases[right]=max(0.0,targets[right]-verified)
        return {'status':'conditional','purchases_m3':purchases,
                'attribution':{'internal_transfer_m3':verified,'from':left,'to':right,
                               'supportable_transfer_m3':[_round(transfer[0]),_round(transfer[1])]},
                'unknown_flux':{'plot':left,
                                'residual_loss_m3':[_round(max(0.0,loss[0]-verified)),_round(max(0.0,loss[1]-verified))],
                                'offshore_exit':'unknown'}}
    if route['target']=='offshore_exit':
        pid=route.get('plot_id')
        if pid not in plot_comp:raise ValueError('route_target_unknown')
        comp=plot_comp[pid]
        verified=min(amount,comp['unattributable'][1])
        t_out=comp['transfer'];loss=comp['loss']
        purchases=dict(targets);purchases[pid]=targets[pid]+verified
        return {'status':'conditional','purchases_m3':purchases,
                'attribution':{'offshore_loss_verified_m3':verified,'plot':pid,'cross_plot_credit':False},
                'unknown_flux':{'plot':pid,
                                'residual_loss_m3':[_round(max(0.0,loss[0]-t_out[1]-verified)),
                                                    _round(max(0.0,loss[1]-t_out[0]-verified))],
                                'offshore_exit':'partially_verified'}}
    raise ValueError('route_target_unknown')

def _store_budget(db,doc,comp):
    bid=doc['budget_id']
    db.execute('INSERT INTO shore_budget VALUES(?,?,?,?,?)',
               (bid,doc['tide_record_id'],json.dumps(doc['periods']),'pending_verification',doc['id']))
    for p in doc['plots']:
        pid=p['plot_id'];c=comp['plots'][pid]
        offers=[r['scenario']['purchases_m3'][pid] for r in comp['routes'].values()]
        purchases=offers or [p['fill_target_m3']]
        db.execute('INSERT INTO budget_plot VALUES(?,?,?,?,?,?,?,?,?,?,?)',
                   (bid,pid,c['change'][0],c['change'][1],c['transfer'][0],c['transfer'][1],
                    c['unattributable'][0],c['unattributable'][1],p['fill_target_m3'],
                    _round(min(purchases)),_round(max(purchases))))
    for rid,r in comp['routes'].items():
        db.execute('INSERT INTO budget_route VALUES(?,?,?,?,?,?,?)',
                   (bid,rid,r['target'],r['claim'],'pending',None,
                    json.dumps(r['scenario'],separators=(',',':'))))
    for layer,item,value in comp['layers']:
        db.execute('INSERT INTO budget_layer VALUES(?,?,?,?)',
                   (bid,layer,item,json.dumps(value,separators=(',',':'))))
    for pid,axis,segment,reason in comp['resurvey']:
        db.execute('INSERT INTO budget_resurvey VALUES(?,?,?,?,?)',
                   (bid,pid,axis,json.dumps(segment,separators=(',',':')),reason))

def import_budget(path,file):
    with closing(connect(path)) as db,db:
        doc,fresh=evidence(db,file,'shore_budget')
        if not fresh:return {'id':doc['id'],'replayed':True}
        if db.execute('SELECT 1 FROM shore_budget WHERE id=?',(doc['budget_id'],)).fetchone():
            raise ValueError('budget_id_conflict')
        comp=_compute_budget(db,doc)
        _store_budget(db,doc,comp)
        log(db,doc['budget_id'],'computed','routes_pending',doc['id'])
        return {'budget_id':doc['budget_id'],'status':'pending_verification'}

def import_verification(path,file):
    with closing(connect(path)) as db,db:
        doc,fresh=evidence(db,file,'shore_verification')
        if not fresh:return {'id':doc['id'],'replayed':True}
        budget=db.execute('SELECT * FROM shore_budget WHERE id=?',(doc['budget_id'],)).fetchone()
        if not budget:raise ValueError('unknown_budget')
        if budget['status']!='pending_verification':raise ValueError('budget_already_verified')
        route=db.execute('SELECT * FROM budget_route WHERE budget_id=? AND route_id=?',
                         (doc['budget_id'],doc['route_id'])).fetchone()
        if not route:raise ValueError('unknown_route')
        if route['status']!='pending':raise ValueError('route_not_pending')
        proven=doc.get('proven_m3')
        if not _num(proven) or proven<=0:raise ValueError('verification_amount_invalid')
        raw=db.execute('SELECT raw FROM evidence WHERE id=?',(budget['source_id'],)).fetchone()['raw']
        budget_doc=json.loads(raw)
        route_doc=next((r for r in budget_doc.get('routes',[]) if r['route_id']==doc['route_id']),None)
        if not route_doc:raise ValueError('unknown_route')
        comp=_compute_budget(db,budget_doc)
        if route_doc['target']=='shared_boundary':
            cap=comp['boundaries'][route_doc['boundary_id']]['transfer'][1]
        else:
            cap=comp['plots'][route_doc['plot_id']]['unattributable'][1]
        if proven>cap:raise ValueError('verification_exceeds_supportable')
        scenario=_route_scenario(route_doc,proven,budget_doc,comp['boundaries'],comp['plots'])
        scenario['status']='confirmed'
        db.execute("UPDATE budget_route SET status='confirmed',proven_m3=?,scenario_json=? WHERE budget_id=? AND route_id=?",
                   (proven,json.dumps(scenario,separators=(',',':')),doc['budget_id'],doc['route_id']))
        db.execute("UPDATE budget_route SET status='void' WHERE budget_id=? AND route_id!=?",
                   (doc['budget_id'],doc['route_id']))
        db.execute("UPDATE shore_budget SET status='verified' WHERE id=?",(doc['budget_id'],))
        for pid,amount in scenario['purchases_m3'].items():
            db.execute('UPDATE budget_plot SET purchase_lo=?,purchase_hi=? WHERE budget_id=? AND plot_id=?',
                       (_round(amount),_round(amount),doc['budget_id'],pid))
        log(db,doc['budget_id'],'route_confirmed',doc['route_id'],doc['id'])
        for other in comp['routes']:
            if other!=doc['route_id']:log(db,doc['budget_id'],'route_void',other,doc['id'])
        return {'budget_id':doc['budget_id'],'route_id':doc['route_id'],'status':'confirmed',
                'purchases_m3':scenario['purchases_m3']}
