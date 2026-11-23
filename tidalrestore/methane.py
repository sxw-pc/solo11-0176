"""Offline methane source attribution for one tide segment.

Mass conservation plus isotope endmember mixing yield a JOINT FEASIBLE SET of
local (sediment) and external (upstream) contributions - never a lone point
estimate.  The chamber calibration drift is one batch-shared bias: it cannot
be tuned per chamber and it does not shrink by averaging readings.  When the
endmembers are linearly dependent in the available isotope channels the split
is unidentifiable, so the assessment keeps the full feasible set, the unknown
degrees of freedom and the pre-authorized resampling routes, instead of
picking whichever source fits slightly better.
"""
import json
from contextlib import closing
from datetime import datetime,timedelta
from fractions import Fraction
from itertools import combinations
from .store import connect,evidence,log

PHASES=('high','mid','low')
ROUTE_SAMPLES={'A':'porewater','B':'transect'}
# Endmembers closer than this in every available channel cannot separate sources.
ENDMEMBER_MIN_SEPARATION=1

DISPOSITIONS={
 'local_dominant':{'target':'P1','action':'list P1 as sediment treatment target',
                   'accountable':'plot sediment management','suspend':'upstream interception works'},
 'external_dominant':{'target':'upstream','action':'list upstream inlet for interception',
                      'accountable':'upstream water authority','suspend':'P1 contractor attribution'},
 'undetermined':{'target':'none','action':'retain the joint feasible set and run an authorized resampling route',
                 'accountable':'unassigned','suspend':'P1 contractor attribution and upstream interception works'}}

def _parse(ts):return datetime.fromisoformat(ts.replace('Z','+00:00'))
def _minutes(start,end):return Fraction(int((_parse(end)-_parse(start)).total_seconds()),60)

def _rank(rows):
    a=[[Fraction(x) for x in r] for r in rows if any(x!=0 for x in r)]
    if not a:return 0
    rank=0
    for col in range(len(a[0])):
        piv=next((r for r in range(rank,len(a)) if a[r][col]!=0),None)
        if piv is None:continue
        a[rank],a[piv]=a[piv],a[rank]
        for r in range(len(a)):
            if r!=rank and a[r][col]!=0:
                f=a[r][col]/a[rank][col];a[r]=[x-f*y for x,y in zip(a[r],a[rank])]
        rank+=1
        if rank==len(a):break
    return rank

def _solve(rows,rhs):
    n=len(rows);a=[[Fraction(x) for x in rows[i]]+[Fraction(rhs[i])] for i in range(n)]
    for col in range(n):
        piv=next((r for r in range(col,n) if a[r][col]!=0),None)
        if piv is None:return None
        a[col],a[piv]=a[piv],a[col]
        for r in range(n):
            if r!=col and a[r][col]!=0:
                f=a[r][col]/a[col][col];a[r]=[x-f*y for x,y in zip(a[r],a[col])]
    return [a[i][n]/a[i][i] for i in range(n)]

def _vertices(n,eqs,ineq):
    """Exact vertices of {x : eqs x = b, ineq x <= b} with Fraction arithmetic."""
    need=n-len(eqs)
    if need<0:return []
    out=[]
    for active in combinations(range(len(ineq)),need):
        sol=_solve([c for c,_ in eqs]+[ineq[i][0] for i in active],
                   [b for _,b in eqs]+[ineq[i][1] for i in active])
        if sol is None:continue
        if all(sum(c*x for c,x in zip(co,sol))<=b for co,b in ineq):
            if not any(all(s==t for s,t in zip(sol,v)) for v in out):out.append(sol)
    return out

def _validate_sample(s,batch):
    if not s.get('id') or not s.get('chamber_id'):raise ValueError('methane_sample_identity')
    if s.get('phase') not in PHASES:raise ValueError('methane_phase_invalid')
    if not s.get('started_at') or not s.get('ended_at') or not s['started_at']<s['ended_at']:
        raise ValueError('methane_interval_invalid')
    if s.get('calib_batch')!=batch:raise ValueError('calibration_batch_mismatch')
    if not isinstance(s.get('ch4_mg'),int) or s['ch4_mg']<0:raise ValueError('methane_value_invalid')
    iso=s.get('iso')
    if iso is not None and not(isinstance(iso,list) and len(iso) in (2,3) and all(isinstance(x,int) for x in iso)):
        raise ValueError('methane_isotope_invalid')

def _insert_sample(db,campaign_id,s,eid):
    db.execute('INSERT INTO methane_sample VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',
               (campaign_id,s['id'],s['role'],s['chamber_id'],s['phase'],s['started_at'],s['ended_at'],
                s['calib_batch'],s['ch4_mg'],json.dumps(s['iso']) if s.get('iso') else None,s.get('tol_mg'),eid))

def _analyze(db,campaign_id,eid):
    camp=db.execute('SELECT * FROM methane_campaign WHERE id=?',(campaign_id,)).fetchone()
    samples=[dict(r) for r in db.execute('SELECT * FROM methane_sample WHERE campaign_id=? ORDER BY id',(campaign_id,))]
    chambers=[s for s in samples if s['role']=='chamber']
    upstream=[s for s in samples if s['role']=='upstream']
    pore=[s for s in samples if s['role']=='porewater']
    tran=[s for s in samples if s['role']=='transect']
    k=len(chambers);n=2*k+1;di=2*k;drift=Fraction(camp['drift_mg'])
    def vec():return [Fraction(0)]*n
    # Mass conservation: L_i + E_i = c_i - D, one shared drift D for the whole batch.
    eqs=[]
    for i,s in enumerate(chambers):
        v=vec();v[i]=1;v[k+i]=1;v[di]=1;eqs.append((v,Fraction(s['ch4_mg'])))
    # Isotope endmember mixing per channel: L_i*(sed_c - r_ic) + E_i*(ext_c - r_ic) = 0.
    # Endmembers too close in every channel are linearly dependent here and add no equation.
    sed=json.loads(camp['sed_iso']);ext=json.loads(camp['ext_iso'])
    close=all(abs(Fraction(a)-Fraction(b))<=ENDMEMBER_MIN_SEPARATION for a,b in zip(sed,ext))
    iso_rows=0
    if not close:
        for i,s in enumerate(chambers):
            r=json.loads(s['iso']) if s['iso'] else None
            if not r:continue
            for c in range(min(len(r),len(sed),len(ext))):
                v=vec();v[i]=Fraction(sed[c]-r[c]);v[k+i]=Fraction(ext[c]-r[c])
                if any(x!=0 for x in v):eqs.append((v,Fraction(0)));iso_rows+=1
    # A route measurement characterizes one shared source term for the plot.
    if pore and k>1:
        for i in range(1,k):
            v=vec();v[0]=1;v[i]=-1;eqs.append((v,Fraction(0)))
    if tran and k>1:
        for i in range(1,k):
            v=vec();v[k]=1;v[k+i]=-1;eqs.append((v,Fraction(0)))
    ineq=[]
    for i in range(k):
        v=vec();v[i]=-1;ineq.append((v,Fraction(0)))
        v=vec();v[k+i]=-1;ineq.append((v,Fraction(0)))
    v=vec();v[di]=1;ineq.append((v,drift))
    v=vec();v[di]=-1;ineq.append((v,drift))
    # Tidal lag: the upstream reading only bounds delivery if its lagged arrival
    # window overlaps a chamber collection interval.
    lag_lo,lag_hi=camp['lag_lo'],camp['lag_hi']
    aligned=False
    if upstream:
        arrive=(_parse(upstream[0]['started_at'])+timedelta(minutes=lag_lo),
                _parse(upstream[0]['ended_at'])+timedelta(minutes=lag_hi))
        for s in chambers:
            if arrive[0]<=_parse(s['ended_at']) and _parse(s['started_at'])<=arrive[1]:aligned=True
    missing=[]
    if not upstream:missing.append('upstream')
    elif not aligned:missing.append('lag_alignment')
    for s in chambers:
        if not s['iso']:missing.append('isotope:'+s['chamber_id'])
    gamma=None
    if upstream and aligned:
        # P1 chambers accumulate external methane for longer than the upstream
        # chamber did, so delivery can exceed the upstream reading by at most
        # the collection-interval ratio.
        gamma=max(_minutes(s['started_at'],s['ended_at']) for s in chambers)/_minutes(upstream[0]['started_at'],upstream[0]['ended_at'])
        u=Fraction(upstream[0]['ch4_mg'])
        for i in range(k):
            v=vec();v[k+i]=1;v[di]=gamma;ineq.append((v,gamma*u))
    for group,idx in ((pore,0),(tran,1)):
        if group:
            lo=Fraction(group[0]['ch4_mg']-group[0]['tol_mg']);hi=Fraction(group[0]['ch4_mg']+group[0]['tol_mg'])
            for i in range(k):
                v=vec();v[idx*k+i]=1;ineq.append((v,hi))
                v=vec();v[idx*k+i]=-1;ineq.append((v,-lo))
    verts=_vertices(n,eqs,ineq)
    if not verts:raise ValueError('methane_infeasible')
    def alpha(v,idxs):
        den=sum(Fraction(chambers[i]['ch4_mg'])-v[di] for i in idxs)
        return sum(v[i] for i in idxs)/den if den>0 else None
    def bounds(idxs):
        vals=[(alpha(v,idxs),v) for v in verts];vals=[x for x in vals if x[0] is not None]
        return min(vals,key=lambda x:x[0]),max(vals,key=lambda x:x[0])
    plo,phi=bounds(tuple(range(k)))
    def witness(pair):
        val,v=pair
        return {'local_fraction':float(val),'delta_mg':float(v[di]),
                'chambers':{chambers[i]['chamber_id']:{'local_mg':float(v[i]),'external_mg':float(v[k+i])} for i in range(k)},
                'uses_readings':[s['id'] for s in chambers]+[s['id'] for s in upstream]}
    lo,hi=plo[0],phi[0]
    status='local_dominant' if lo>Fraction(1,2) else 'external_dominant' if hi<Fraction(1,2) else 'undetermined'
    dofs=n-_rank([c for c,_ in eqs])
    rank_deficient=iso_rows==0 and not pore and not tran
    plan=[]
    if status=='undetermined' or missing:
        plan=[{'route':'A','location':'P1 porewater endmember','tide':'low',
               'measures':'local sediment flux directly at the exposed flat',
               'expected_discrimination':'identifies the local contribution within its tolerance band',
               'if_decisive':DISPOSITIONS['local_dominant']},
              {'route':'B','location':'upstream inlet','tide':camp['tide'],'lag_minutes':[lag_lo,lag_hi],
               'measures':'external delivery via a time-lagged transect',
               'expected_discrimination':'identifies the external contribution within its tolerance band',
               'if_decisive':DISPOSITIONS['external_dominant']}]
    result={'campaign_id':campaign_id,'plot_id':camp['plot_id'],'tide':camp['tide'],'status':status,
            'local_fraction':{'plot':[float(lo),float(hi)],
                              'chambers':{chambers[i]['chamber_id']:[float(b[0][0]),float(b[1][0])]
                                          for i,b in enumerate(bounds((i,)) for i in range(k))}},
            'shared_drift_mg':camp['drift_mg'],
            'drift_note':'one batch-shared drift covers all chambers; it is never tuned per point and never shrinks by averaging',
            'rank_deficient':rank_deficient,'unidentified_dofs':dofs,
            'scenarios':{'local_80':{'feasible':hi>=Fraction(4,5),'witness':witness(phi)},
                         'external_80':{'feasible':lo<=Fraction(1,5),'witness':witness(plo)}},
            'lag':{'window_minutes':[lag_lo,lag_hi],'aligned':aligned},
            'delivery_ratio':float(gamma) if gamma is not None else None,
            'missing':missing,'resampling_plan':plan,
            'decision':DISPOSITIONS[status],'dispositions':DISPOSITIONS,
            'readings':{s['id']:{'role':s['role'],'chamber_id':s['chamber_id'],'ch4_mg':s['ch4_mg'],
                                 'iso':json.loads(s['iso']) if s['iso'] else None,'phase':s['phase'],
                                 'started_at':s['started_at'],'ended_at':s['ended_at']} for s in samples}}
    db.execute('INSERT INTO methane_assessment VALUES(?,?,?,?,?,?,?,?,?) '
               'ON CONFLICT(campaign_id) DO UPDATE SET plot_id=excluded.plot_id,status=excluded.status,'
               'local_lo=excluded.local_lo,local_hi=excluded.local_hi,dofs=excluded.dofs,'
               'rank_deficient=excluded.rank_deficient,result=excluded.result,source_id=excluded.source_id',
               (campaign_id,camp['plot_id'],status,str(lo),str(hi),dofs,1 if rank_deficient else 0,
                json.dumps(result,sort_keys=True),eid))
    return result

def import_methane(path,file):
    with closing(connect(path)) as db,db:
        doc,fresh=evidence(db,file,'methane')
        if not fresh:return {'id':doc['id'],'replayed':True}
        if not db.execute('SELECT 1 FROM plot WHERE id=?',(doc['plot_id'],)).fetchone():
            raise ValueError('unknown_plot')
        if doc['tide'] not in PHASES:raise ValueError('methane_tide_invalid')
        lag=doc['lag_window_minutes']
        if not(isinstance(lag,list) and len(lag)==2 and all(isinstance(x,int) for x in lag) and 0<=lag[0]<=lag[1]):
            raise ValueError('lag_window_invalid')
        cal=doc['calibration']
        if not cal.get('batch_id') or not isinstance(cal.get('shared_drift_mg'),int) or cal['shared_drift_mg']<=0:
            raise ValueError('calibration_invalid')
        ends=doc['endmembers']
        sed,ext=ends.get('sediment',{}).get('iso'),ends.get('external',{}).get('iso')
        if not(isinstance(sed,list) and isinstance(ext,list) and len(sed)>=2 and len(ext)>=2):
            raise ValueError('endmembers_invalid')
        if db.execute('SELECT 1 FROM methane_campaign WHERE id=?',(doc['campaign_id'],)).fetchone():
            raise ValueError('methane_campaign_exists')
        samples=doc['samples']
        if not any(s.get('role')=='chamber' for s in samples):raise ValueError('chamber_required')
        ids=set()
        for s in samples:
            if s.get('role') not in ('chamber','upstream'):raise ValueError('route_sample_in_campaign')
            _validate_sample(s,cal['batch_id'])
            if s['id'] in ids:raise ValueError('methane_sample_duplicate')
            ids.add(s['id'])
        db.execute('INSERT INTO methane_campaign VALUES(?,?,?,?,?,?,?,?,?,?)',
                   (doc['campaign_id'],doc['plot_id'],doc['tide'],lag[0],lag[1],cal['batch_id'],
                    cal['shared_drift_mg'],json.dumps(sed),json.dumps(ext),doc['id']))
        for s in samples:_insert_sample(db,doc['campaign_id'],s,doc['id'])
        result=_analyze(db,doc['campaign_id'],doc['id'])
        log(db,doc['campaign_id'],result['status'],'methane_source',doc['id'])
        return {'campaign_id':doc['campaign_id'],'status':result['status'],
                'local_fraction_plot':result['local_fraction']['plot'],
                'unidentified_dofs':result['unidentified_dofs']}

def import_methane_route(path,file):
    with closing(connect(path)) as db,db:
        doc,fresh=evidence(db,file,'methane_route')
        if not fresh:return {'id':doc['id'],'replayed':True}
        camp=db.execute('SELECT * FROM methane_campaign WHERE id=?',(doc['campaign_id'],)).fetchone()
        if not camp:raise ValueError('unknown_campaign')
        route=doc.get('route')
        if route not in ROUTE_SAMPLES:raise ValueError('route_invalid')
        role=ROUTE_SAMPLES[route]
        if db.execute('SELECT 1 FROM methane_sample WHERE campaign_id=? AND role=?',(doc['campaign_id'],role)).fetchone():
            raise ValueError('route_already_applied')
        samples=doc['samples']
        if not samples:raise ValueError('route_samples_required')
        for s in samples:
            if s.get('role')!=role:raise ValueError('route_sample_mismatch')
            _validate_sample(s,camp['calib_batch'])
            if not isinstance(s.get('tol_mg'),int) or s['tol_mg']<=0:raise ValueError('route_tolerance_required')
            if role=='porewater' and s['phase']!='low':raise ValueError('porewater_phase_invalid')
            if role=='transect' and not(isinstance(s.get('lag_minutes'),int) and camp['lag_lo']<=s['lag_minutes']<=camp['lag_hi']):
                raise ValueError('transect_lag_invalid')
        for s in samples:_insert_sample(db,doc['campaign_id'],s,doc['id'])
        result=_analyze(db,doc['campaign_id'],doc['id'])
        log(db,doc['campaign_id'],result['status'],'methane_route_'+route,doc['id'])
        return {'campaign_id':doc['campaign_id'],'route':route,'status':result['status'],
                'target':result['decision']['target']}
