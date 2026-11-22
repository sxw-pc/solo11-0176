"""Floating debris capture: legacy single-image summaries and cross-camera identity hypotheses.

The legacy `debris` kind stores one camera summary per file; it proves an image
summary was archived, never object identity between cameras, and pieces must not
be summed into a removal count.  The `debris-track` kind captures a whole scene
(tide velocity interval, camera coverage and clock error, occlusion segments,
inlets) and enumerates every kinematically feasible identity hypothesis instead
of nearest-neighbour matching: splits and merges keep parent-child identity with
piece conservation, occlusion means unknown (not removed), and a tide reversal
may bring the same object back to its original camera without becoming a second
source.  `debris-plan` records authorised response routes without resolving
hypotheses.  `debris-recovery` and `debris-handover` close the loop physically:
a boat-sealed bag only confirms clearance once a shore weigh slip receives it,
duplicate or late receipts never double count, and unclaimed pieces stay
traceable to their source observation.  No single-database transaction is
assumed to roll back a physical handover; the ledger is recomputed from the
full evidence set whenever new recovery or handover evidence arrives.
"""
import json
from contextlib import closing
from datetime import datetime,timezone
from itertools import combinations
from .store import connect,evidence,log

def _ts(s):return int(datetime.fromisoformat(s.replace('Z','+00:00')).timestamp())
def _iso(t):return datetime.fromtimestamp(int(t),timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
def _family(shape):return shape.split('-')[0]

def import_observation(path,file):
    with closing(connect(path)) as db,db:
        doc,fresh=evidence(db,file,'debris')
        if not fresh:return {'id':doc['id'],'replayed':True}
        if not db.execute('SELECT 1 FROM plot WHERE id=?',(doc['plot_id'],)).fetchone():
            raise ValueError('unknown_plot')
        if not doc.get('camera') or not doc.get('observed_at') or not doc.get('shape'):
            raise ValueError('debris_identity_invalid')
        if not isinstance(doc.get('position_m'),int) or not isinstance(doc.get('pieces'),int) or doc['pieces']<1:
            raise ValueError('debris_count_invalid')
        db.execute('INSERT INTO debris_observation VALUES(?,?,?,?,?,?,?,?)',
                   (doc['obs_id'],doc['plot_id'],doc['camera'],doc['observed_at'],doc['position_m'],doc['shape'],doc['pieces'],doc['id']))
        return {'obs_id':doc['obs_id'],'status':'captured'}

# ---------- tide kinematics ----------

def _tide_at(tide,t):
    for r in tide.get('reversals',[]):
        if _ts(r['start'])<=t<_ts(r['end']):return r['velocity_min_mps'],r['velocity_max_mps']
    return tide['velocity_min_mps'],tide['velocity_max_mps']

def _drift(tide,t0,t1):
    """(min,max) displacement in metres over [t0,t1] under the piecewise tide schedule."""
    if t1<=t0:return 0.0,0.0
    cuts={t0,t1}
    for r in tide.get('reversals',[]):
        for b in (_ts(r['start']),_ts(r['end'])):
            if t0<b<t1:cuts.add(b)
    lo=hi=0.0;pts=sorted(cuts)
    for a,b in zip(pts,pts[1:]):
        vn,vx=_tide_at(tide,(a+b)//2);lo+=vn*(b-a);hi+=vx*(b-a)
    return lo,hi

def _window_points(tide,t0,t1):
    pts={t0,t1}
    for r in tide.get('reversals',[]):
        for b in (_ts(r['start']),_ts(r['end'])):
            if t0<=b<=t1:pts.add(b)
    return sorted(pts)

def _link_ok(tide,p0,t0,e0,p1,t1,e1):
    """True if an object seen at (p0,t0±e0) can kinematically reach (p1,t1±e1)."""
    best=None
    for s in _window_points(tide,t0-e0,t0+e0):
        for f in _window_points(tide,t1-e1,t1+e1):
            if f<s:continue
            lo,hi=_drift(tide,s,f)
            best=(lo,hi) if best is None else (min(best[0],lo),max(best[1],hi))
    if best is None:return p0==p1
    return best[0]<=p1-p0<=best[1]

def _envelope(tide,p,t,e,at):
    """(min,max) possible position at time `at` for an object anchored at (p,t±e)."""
    pts=[s for s in _window_points(tide,t-e,t+e) if s<=at]
    if not pts:return float(p),float(p)
    los=[];his=[]
    for s in pts:
        lo,hi=_drift(tide,s,at);los.append(lo);his.append(hi)
    return p+min(los),p+max(his)

def _inlet_ok(track,inlet,obs,cams):
    """True if the inlet could have delivered this observation during some feasible window."""
    t1=_ts(obs['at']);e=cams[obs['camera']]['clock_error_s']
    starts={_ts(o['at']) for o in track['observations']}
    for r in track['tide'].get('reversals',[]):starts|={_ts(r['start']),_ts(r['end'])}
    for o in track.get('occlusions',[]):starts|={_ts(o['start']),_ts(o['end'])}
    starts.add(t1-e)
    return any(s<=t1+e and _link_ok(track['tide'],inlet['at_m'],s,0,obs['position_m'],t1,e) for s in sorted(starts))

# ---------- identity hypothesis enumeration ----------

def _enumerate(track):
    """Enumerate all feasible identity hypotheses as (assignments, branches) states.

    Each observation is assigned to exactly one of: continuation of a live branch
    (same shape, same piece count), a split child (fewer pieces, same shape family,
    parent remainder survives as an unobserved ghost branch), a merge of branches
    (piece counts sum exactly), or a new object (inlet delivery or unknown source).
    Kinematic feasibility with camera clock error gates every link; shape alone
    never forces a new source.
    """
    cams={c['id']:c for c in track['cameras']}
    tide=track['tide']
    observations=sorted(track['observations'],key=lambda o:(_ts(o['at']),o['obs_id']))
    states=[((),[])]
    for o in observations:
        anchor=(o['position_m'],_ts(o['at']),cams[o['camera']]['clock_error_s'])
        nxt=[]
        for assigns,branches in states:
            options=[('new',(),('unknown',))]
            for inlet in track.get('inlets',[]):
                if _inlet_ok(track,inlet,o,cams):options.append(('new',(),('inlet:'+inlet['name'],)))
            for i,b in enumerate(branches):
                if not _link_ok(tide,*b['anchor'],*anchor):continue
                if b['pieces']==o['pieces'] and b['shape']==o['shape']:
                    options.append(('continue',(i,),(b['anchor_obs'],)))
                if b['pieces']>o['pieces'] and _family(b['shape'])==_family(o['shape']):
                    options.append(('split',(i,),(b['anchor_obs'],)))
            for r in range(2,len(branches)+1):
                for combo in combinations(range(len(branches)),r):
                    if sum(branches[i]['pieces'] for i in combo)!=o['pieces']:continue
                    if any(_family(branches[i]['shape'])!=_family(o['shape']) for i in combo):continue
                    if all(_link_ok(tide,*branches[i]['anchor'],*anchor) for i in combo):
                        options.append(('merge',combo,tuple(sorted(branches[i]['anchor_obs'] for i in combo))))
            for rel,idx,canon in options:
                nb=[dict(b) for b in branches]
                if rel=='continue':
                    b=branches[idx[0]]
                    nb[idx[0]]={'pieces':o['pieces'],'shape':o['shape'],'anchor':anchor,'anchor_obs':o['obs_id'],'obs':b['obs']+[o['obs_id']],'source':b['source']}
                elif rel=='split':
                    b=branches[idx[0]]
                    nb[idx[0]]={'pieces':o['pieces'],'shape':o['shape'],'anchor':anchor,'anchor_obs':o['obs_id'],'obs':b['obs']+[o['obs_id']],'source':b['source']}
                    nb.append({'pieces':b['pieces']-o['pieces'],'shape':b['shape'],'anchor':b['anchor'],'anchor_obs':b['anchor_obs'],'obs':[],'source':b['source']})
                elif rel=='merge':
                    history=[]
                    for k in sorted(idx,reverse=True):history=nb[k]['obs']+history;del nb[k]
                    nb.append({'pieces':o['pieces'],'shape':o['shape'],'anchor':anchor,'anchor_obs':o['obs_id'],'obs':history+[o['obs_id']],'source':'merged'})
                else:
                    nb.append({'pieces':o['pieces'],'shape':o['shape'],'anchor':anchor,'anchor_obs':o['obs_id'],'obs':[o['obs_id']],'source':canon[0]})
                nxt.append((assigns+((o['obs_id'],rel,canon),),nb))
        merged={}
        for a,b in nxt:
            if a not in merged:merged[a]=b
        states=sorted(merged.items(),key=lambda kv:kv[0])
        if len(states)>600:raise ValueError('hypothesis_explosion')
    return states

def _span(track,chain,horizon):
    """(min,max) position envelope covered by a branch's anchor chain up to horizon."""
    tide=track['tide']
    marks=sorted({b for r in tide.get('reversals',[]) for b in (_ts(r['start']),_ts(r['end']))})
    los=[];his=[]
    for i,(p,t,e) in enumerate(chain):
        los.append(p);his.append(p)
        t1=chain[i+1][1] if i+1<len(chain) else horizon
        for tt in sorted({t1}|{m for m in marks if t<m<t1}):
            lo,hi=_envelope(tide,p,t,e,tt);los.append(lo);his.append(hi)
    return min(los),max(his)

def _hypo_docs(track,states):
    tide=track['tide']
    horizon=max([_ts(o['at']) for o in track['observations']]+([_ts(track['horizon_end'])] if track.get('horizon_end') else []))
    obs_by_id={o['obs_id']:o for o in track['observations']}
    cams={c['id']:c for c in track['cameras']}
    plots=sorted(track['plots'],key=lambda p:p['from_m'])
    docs=[]
    for assigns,branches in states:
        los=[];his=[];reobs=[]
        for bi,b in enumerate(branches):
            chain=[(obs_by_id[x]['position_m'],_ts(obs_by_id[x]['at']),cams[obs_by_id[x]['camera']]['clock_error_s']) for x in b['obs']]
            if not chain:chain=[b['anchor']]
            lo,hi=_span(track,chain,horizon);los.append(lo);his.append(hi)
            p,t,e=b['anchor']
            for c in track['cameras']:
                run=None;tt=horizon
                while tt<=horizon+1800:
                    el,eh=_envelope(tide,p,t,e,tt)
                    if el<=c['covers'][1] and eh>=c['covers'][0]:
                        if run is None:run=tt
                    elif run is not None:
                        reobs.append({'branch':bi,'camera':c['id'],'start':_iso(run),'end':_iso(tt)});run=None
                    tt+=30
                if run is not None:reobs.append({'branch':bi,'camera':c['id'],'start':_iso(run),'end':_iso(horizon+1800)})
        lo,hi=min(los),max(his)
        segments=[pl['plot_id'] for pl in plots if lo<pl['to_m'] and hi>pl['from_m']]
        segments+=['occlusion:'+o['cause'] for o in track.get('occlusions',[]) if lo<o['to_m'] and hi>o['from_m']]
        segments+=sorted({b['source'] for b in branches if b['source'].startswith('inlet:')})
        bounds=[f"{a['plot_id']}/{b['plot_id']}@{a['to_m']}" for a,b in zip(plots,plots[1:]) if lo<a['to_m']<hi]
        total=sum(b['pieces'] for b in branches)
        docs.append({'relations':{oid:{'relation':rel,'parents':list(par)} for oid,rel,par in assigns},
                     'branches':[{'pieces':b['pieces'],'shape':b['shape'],'observations':b['obs'],'anchor_obs':b['anchor_obs'],'source':b['source'],'observed':bool(b['obs'])} for b in branches],
                     'total_pieces':total,'segments':segments,'boundaries':bounds,'reobserve':reobs,
                     'confirmed_pieces':0,'min_unintercepted':total,'max_unintercepted':total})
    return docs

def import_track(path,file):
    with closing(connect(path)) as db,db:
        doc,fresh=evidence(db,file,'debris-track')
        if not fresh:return {'id':doc['id'],'replayed':True}
        tid=doc['track_id']
        if db.execute('SELECT 1 FROM debris_track WHERE id=?',(tid,)).fetchone():raise ValueError('track_conflict')
        plots=doc.get('plots') or []
        for pl in plots:
            if not db.execute('SELECT 1 FROM plot WHERE id=?',(pl['plot_id'],)).fetchone():raise ValueError('unknown_plot')
        seg=doc['segment']
        ordered=sorted(plots,key=lambda p:p['from_m'])
        if not ordered or ordered[0]['from_m']!=seg['from_m'] or ordered[-1]['to_m']!=seg['to_m'] or any(a['to_m']!=b['from_m'] for a,b in zip(ordered,ordered[1:])):
            raise ValueError('plot_segments_invalid')
        tide=doc['tide']
        if tide['velocity_min_mps']>tide['velocity_max_mps']:raise ValueError('tide_invalid')
        for r in tide.get('reversals',[]):
            if r['velocity_min_mps']>r['velocity_max_mps'] or r['start']>=r['end']:raise ValueError('tide_invalid')
        cams=doc['cameras']
        if len({c['id'] for c in cams})!=len(cams):raise ValueError('camera_invalid')
        for c in cams:
            if c['clock_error_s']<0 or c['covers'][0]>=c['covers'][1] or c['covers'][0]<seg['from_m'] or c['covers'][1]>seg['to_m']:
                raise ValueError('camera_invalid')
        occs=doc.get('occlusions',[])
        for o in occs:
            if o['from_m']>=o['to_m'] or o['start']>=o['end']:raise ValueError('occlusion_invalid')
        obs=doc.get('observations') or []
        if not obs or len({o['obs_id'] for o in obs})!=len(obs):raise ValueError('observation_invalid')
        cmap={c['id']:c for c in cams}
        for o in obs:
            c=cmap.get(o['camera'])
            if not c or not isinstance(o.get('pieces'),int) or o['pieces']<1 or not o.get('shape'):raise ValueError('observation_invalid')
            if not c['covers'][0]<=o['position_m']<=c['covers'][1]:raise ValueError('outside_camera_coverage')
            t=_ts(o['at'])
            if doc.get('horizon_end') and t>_ts(doc['horizon_end']):raise ValueError('observation_invalid')
            for oc in occs:
                if oc['from_m']<=o['position_m']<=oc['to_m'] and _ts(oc['start'])<=t<=_ts(oc['end']):
                    raise ValueError('observation_during_occlusion')
        states=_enumerate(doc)
        docs=_hypo_docs(doc,states)
        db.execute('INSERT INTO debris_track VALUES(?,?,?)',(tid,json.dumps(doc,sort_keys=True),doc['id']))
        for n,h in enumerate(docs,1):
            hid=f"{tid}-H{n:03d}";h['hypothesis_id']=hid;h['track_id']=tid
            db.execute('INSERT INTO debris_hypothesis VALUES(?,?,?,?)',(hid,tid,'unverified',json.dumps(h,sort_keys=True)))
        log(db,tid,'captured',f'{len(docs)}_hypotheses',doc['id'])
        return {'track_id':tid,'status':'captured','hypotheses':len(docs)}

# ---------- authorised operation routes ----------

def _evaluate_route(track,route,hyps,obs_by_id,horizon):
    tide=track['tide']
    cams={c['id']:c for c in track['cameras']}
    def anchor_of(b):
        o=obs_by_id[b['anchor_obs']];return o['position_m'],_ts(o['at']),cams[o['camera']]['clock_error_s']
    if route.get('action')=='net-boundary':
        x=route.get('at_m')
        if not isinstance(x,int) or not track['segment']['from_m']<=x<=track['segment']['to_m'] or not route.get('deploy_at'):
            raise ValueError('route_invalid')
        deploy=_ts(route['deploy_at'])
        per=[]
        for hid,h in hyps:
            mn=mx=0
            for b in h['branches']:
                p,t,e=anchor_of(b)
                lo0,hi0=_envelope(tide,p,t,e,deploy);lo1,hi1=_envelope(tide,p,t,e,horizon+1800)
                if min(lo0,lo1)<=x<=max(hi0,hi1):mx+=b['pieces']
                if (hi0<x and lo1>x) or (lo0>x and hi1<x):mn+=b['pieces']
            per.append({'hypothesis_id':hid,'interceptable_min':mn,'interceptable_max':mx})
        return {'route_id':route['route_id'],'action':'net-boundary','intercept_at_m':x,'deploy_at':route['deploy_at'],
                'hypotheses':per,'keeps_hypotheses':[hid for hid,_ in hyps],'mutually_exclusive':True,
                'west_shore_cleanup':'deferred','next_shift':['identify-netted-pieces','clean-west-shore']}
    if route.get('action')=='boat-verify':
        o=obs_by_id.get(route.get('target_obs'))
        if not o:raise ValueError('route_invalid')
        linked=[hid for hid,h in hyps if h['relations'][o['obs_id']]['relation']!='new']
        separate=[hid for hid,h in hyps if h['relations'][o['obs_id']]['relation']=='new']
        others=sorted({x['obs_id'] for x in track['observations'] if x['camera']!=o['camera']})
        return {'route_id':route['route_id'],'action':'boat-verify','target_obs':o['obs_id'],'intercept_at_m':o['position_m'],
                'expected_pieces':o['pieces'],'distinguishes':{'linked':linked,'separate':separate},
                'keeps_hypotheses':[hid for hid,_ in hyps],'mutually_exclusive':True,
                'west_shore_cleanup':'partial','next_shift':['follow-up:'+x for x in others]}
    raise ValueError('route_invalid')

def import_plan(path,file):
    with closing(connect(path)) as db,db:
        doc,fresh=evidence(db,file,'debris-plan')
        if not fresh:return {'id':doc['id'],'replayed':True}
        tid=doc['track_id']
        row=db.execute('SELECT doc FROM debris_track WHERE id=?',(tid,)).fetchone()
        if not row:raise ValueError('unknown_track')
        track=json.loads(row['doc'])
        if db.execute('SELECT 1 FROM debris_plan WHERE plan_id=?',(doc['plan_id'],)).fetchone():raise ValueError('plan_conflict')
        if not doc.get('crew') or not doc.get('shift') or not doc.get('routes'):raise ValueError('plan_invalid')
        if len({r['route_id'] for r in doc['routes']})!=len(doc['routes']):raise ValueError('plan_invalid')
        obs_by_id={o['obs_id']:o for o in track['observations']}
        horizon=max([_ts(o['at']) for o in track['observations']]+([_ts(track['horizon_end'])] if track.get('horizon_end') else []))
        hyps=[(r['id'],json.loads(r['doc'])) for r in db.execute("SELECT id,doc FROM debris_hypothesis WHERE track_id=? AND status='unverified' ORDER BY id",(tid,))]
        for route in doc['routes']:
            evaluated=_evaluate_route(track,route,hyps,obs_by_id,horizon)
            db.execute('INSERT INTO debris_plan VALUES(?,?,?,?)',(doc['plan_id'],route['route_id'],json.dumps(evaluated,sort_keys=True),doc['id']))
        log(db,doc['plan_id'],'authorized',f"{len(doc['routes'])}_routes",doc['id'])
        return {'plan_id':doc['plan_id'],'status':'authorized','routes':[r['route_id'] for r in doc['routes']]}

# ---------- physical recovery / shore handover closed loop ----------

def _recompute(db,tid):
    """Rebuild the piece ledger and hypothesis statuses from all bag and handover evidence.

    Order-independent given the same evidence set: allocation is by (sealed_at,bag_id),
    confirmation requires both a boat seal and a shore receipt, claims against an
    observation are capped at its piece count so two boats writing the same bundle id
    can never sum past it, and excess or unsealed pieces stay pending and traceable.
    """
    track=json.loads(db.execute('SELECT doc FROM debris_track WHERE id=?',(tid,)).fetchone()['doc'])
    remaining={o['obs_id']:o['pieces'] for o in track['observations']}
    bags=[dict(r) for r in db.execute('SELECT * FROM debris_bag WHERE track_id=?',(tid,))]
    db.execute('DELETE FROM debris_ledger WHERE track_id=?',(tid,))
    rows=[]
    for b in sorted((b for b in bags if b['status']=='received'),key=lambda b:(b['sealed_at'],b['id'])):
        for c in json.loads(b['claims']):
            alloc=min(c['pieces'],remaining.get(c['obs_id'],0))
            if alloc:
                remaining[c['obs_id']]-=alloc
                rows.append((tid,'confirmed',c['obs_id'],b['id'],b['boat'],b['receipt_id'],alloc))
            if c['pieces']-alloc:
                rows.append((tid,'unattributed_received',c['obs_id'],b['id'],b['boat'],b['receipt_id'],c['pieces']-alloc))
    for b in bags:
        if b['status']=='received_unsealed':rows.append((tid,'received_unsealed',None,b['id'],None,b['receipt_id'],b['pieces']))
        elif b['status']=='sealed':
            for c in json.loads(b['claims']):rows.append((tid,'in_transit',c['obs_id'],b['id'],b['boat'],None,c['pieces']))
    for oid,left in remaining.items():
        if left:rows.append((tid,'unclaimed',oid,None,None,None,left))
    db.executemany('INSERT INTO debris_ledger(track_id,category,obs_id,bag_id,boat,receipt_id,pieces) VALUES(?,?,?,?,?,?,?)',rows)
    physical=sum(b['pieces'] for b in bags if b['status'] in('received','received_unsealed'))
    confirmed=sum(r[6] for r in rows if r[1]=='confirmed')
    maybe=sum(r[6] for r in rows if r[1] in('unattributed_received','received_unsealed','in_transit'))
    for h in db.execute('SELECT id,status,doc FROM debris_hypothesis WHERE track_id=?',(tid,)):
        doc=json.loads(h['doc']);status=h['status']
        if physical>doc['total_pieces']:
            status='refuted';doc['refute_reason']='physical_received_exceeds_hypothesis'
        doc['confirmed_pieces']=min(confirmed,doc['total_pieces'])
        doc['max_unintercepted']=doc['total_pieces']-doc['confirmed_pieces']
        doc['min_unintercepted']=max(0,doc['max_unintercepted']-maybe)
        db.execute('UPDATE debris_hypothesis SET status=?,doc=? WHERE id=?',(status,json.dumps(doc,sort_keys=True),h['id']))

def import_recovery(path,file):
    with closing(connect(path)) as db,db:
        doc,fresh=evidence(db,file,'debris-recovery')
        if not fresh:return {'id':doc['id'],'replayed':True}
        row=db.execute('SELECT doc FROM debris_track WHERE id=?',(doc['track_id'],)).fetchone()
        if not row:raise ValueError('unknown_track')
        track=json.loads(row['doc']);obs_ids={o['obs_id'] for o in track['observations']}
        if not isinstance(doc.get('pieces'),int) or doc['pieces']<1 or not doc.get('boat') or not doc.get('at'):
            raise ValueError('recovery_invalid')
        claims=doc.get('claims') or []
        if not claims or any(c['obs_id'] not in obs_ids or not isinstance(c['pieces'],int) or c['pieces']<1 for c in claims):
            raise ValueError('unknown_observation')
        if sum(c['pieces'] for c in claims)!=doc['pieces']:raise ValueError('claim_pieces_mismatch')
        bag=db.execute('SELECT * FROM debris_bag WHERE id=?',(doc['bag_id'],)).fetchone()
        if bag:
            if bag['status']!='received_unsealed':raise ValueError('bag_seal_conflict')
            if bag['pieces']!=doc['pieces']:raise ValueError('bag_pieces_conflict')
            db.execute("UPDATE debris_bag SET boat=?,sealed_at=?,position_m=?,claims=?,status='received',source_id=? WHERE id=?",
                       (doc['boat'],doc['at'],doc['position_m'],json.dumps(claims,sort_keys=True),doc['id'],doc['bag_id']))
            status='received'
        else:
            db.execute('INSERT INTO debris_bag VALUES(?,?,?,?,?,?,?,?,?,?)',
                       (doc['bag_id'],doc['track_id'],doc['boat'],doc['at'],doc['position_m'],doc['pieces'],json.dumps(claims,sort_keys=True),'sealed',None,doc['id']))
            status='sealed'
        _recompute(db,doc['track_id'])
        log(db,doc['bag_id'],status,'boat_'+doc['boat'],doc['id'])
        return {'bag_id':doc['bag_id'],'status':status}

def import_handover(path,file):
    with closing(connect(path)) as db,db:
        doc,fresh=evidence(db,file,'debris-handover')
        if not fresh:return {'id':doc['id'],'replayed':True}
        if not db.execute('SELECT 1 FROM debris_track WHERE id=?',(doc['track_id'],)).fetchone():raise ValueError('unknown_track')
        if db.execute('SELECT 1 FROM debris_handover WHERE id=?',(doc['receipt_id'],)).fetchone():raise ValueError('receipt_conflict')
        bags=doc.get('bags') or []
        if not bags or any(not isinstance(b['pieces'],int) or b['pieces']<1 for b in bags):raise ValueError('handover_invalid')
        if sum(b['pieces'] for b in bags)!=doc['total_pieces'] or doc['total_pieces']<1 or doc['weight_kg']<=0:
            raise ValueError('handover_invalid')
        conflicts=[]
        for entry in bags:
            bag=db.execute('SELECT * FROM debris_bag WHERE id=?',(entry['bag_id'],)).fetchone()
            if not bag:
                db.execute('INSERT INTO debris_bag VALUES(?,?,?,?,?,?,?,?,?,?)',
                           (entry['bag_id'],doc['track_id'],None,None,None,entry['pieces'],None,'received_unsealed',doc['receipt_id'],doc['id']))
            elif bag['status']=='sealed':
                if bag['pieces']!=entry['pieces']:raise ValueError('bag_pieces_conflict')
                db.execute("UPDATE debris_bag SET status='received',receipt_id=? WHERE id=?",(doc['receipt_id'],entry['bag_id']))
            else:conflicts.append(entry['bag_id'])
        status='accepted' if not conflicts else 'accepted_with_conflicts'
        db.execute('INSERT INTO debris_handover VALUES(?,?,?,?,?,?,?,?,?)',
                   (doc['receipt_id'],doc['track_id'],doc['sack_id'],doc['at'],doc['total_pieces'],doc['weight_kg'],json.dumps(bags,sort_keys=True),status,doc['id']))
        _recompute(db,doc['track_id'])
        log(db,doc['receipt_id'],status,f"{doc['total_pieces']}_pieces",doc['id'])
        return {'receipt_id':doc['receipt_id'],'status':status,'conflicts':conflicts}
