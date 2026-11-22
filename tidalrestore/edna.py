"""eDNA capture and lane-level joint attribution.

The fixed baseline ``import_sample`` only collects raw reads; it never claims
species occupancy.  ``import_lane`` ingests a whole sequencing lane (samples,
extraction / PCR blanks, positive control, cross-talk matrix and reference
sequence version) and re-solves the *joint* feasible assignment set every time
the lane's membership or reference changes.

Read conservation is enforced as a bounded integer flow: each read observed in
a well is either genuinely local, shared cross-talk from another well's source
pool, or shared background bounded by the blank controls.  Cross-talk from a
single source shares one total amount across every destination and every
barcode, so subtracting a fixed blank number per sample is structurally
impossible here.
"""
import json,math
from collections import deque
from contextlib import closing
from .store import connect,evidence

INVASIVE='BIRD-A'


def import_sample(path,file):
    with closing(connect(path)) as db,db:
        doc,fresh=evidence(db,file,'edna')
        if not fresh:return {'id':doc['id'],'replayed':True}
        if not db.execute('SELECT 1 FROM plot WHERE id=?',(doc['plot_id'],)).fetchone():
            raise ValueError('unknown_plot')
        if not doc.get('site') or not doc.get('batch_id') or not doc.get('barcode') or not doc.get('collected_at'):
            raise ValueError('sample_identity_invalid')
        reads,blank=doc.get('read_count'),doc.get('blank_reads')
        if not isinstance(reads,int) or not isinstance(blank,int) or reads<0 or blank<0:
            raise ValueError('sample_counts_invalid')
        db.execute('INSERT INTO edna_sample VALUES(?,?,?,?,?,?,?,?,?)',
                   (doc['sample_id'],doc['plot_id'],doc['site'],doc['collected_at'],doc['batch_id'],doc['barcode'],reads,blank,doc['id']))
        return {'sample_id':doc['sample_id'],'status':'captured'}


# --------------------------------------------------------------------------- #
# Bounded integer flow (Dinic) — pure standard library, no external solver.
# --------------------------------------------------------------------------- #
class _Dinic:
    def __init__(self,n):
        self.g=[[] for _ in range(n)]
    def add(self,u,v,cap,tag=None):
        self.g[u].append([v,cap,len(self.g[v]),tag])
        self.g[v].append([u,0,len(self.g[u])-1,None])
        return len(self.g[u])-1
    def maxflow(self,s,t):
        flow=0
        while True:
            level=[-1]*len(self.g);level[s]=0;q=deque([s])
            while q:
                u=q.popleft()
                for v,cap,rev,_ in self.g[u]:
                    if cap>0 and level[v]<0:
                        level[v]=level[u]+1;q.append(v)
            if level[t]<0:return flow
            it=[0]*len(self.g)
            def dfs(u,pushed):
                if u==t:return pushed
                while it[u]<len(self.g[u]):
                    e=self.g[u][it[u]]
                    if e[1]>0 and level[u]+1==level[e[0]]:
                        d=dfs(e[0],min(pushed,e[1]))
                        if d:
                            e[1]-=d;self.g[e[0]][e[2]][1]+=d;return d
                    it[u]+=1
                return 0
            while True:
                f=dfs(s,10**18)
                if not f:break
                flow+=f
        return flow


def _bounded_feasible(nodes,edges):
    """Return (feasible, witness edges) for a bounded flow network.

    Standard lower-bound transform: add an unbounded ``snk -> src`` return
    edge so the open network becomes a circulation, then balance each node's
    forced lower-bound flows against a super source/sink.  Witness edges carry
    their tag so realised flows can be extracted.
    """
    idx={n:i for i,n in enumerate(nodes)}
    S,T=len(nodes),len(nodes)+1
    dinic=_Dinic(len(nodes)+2);witness=[]
    balance=[0]*len(nodes)
    dinic.add(idx['snk'],idx['src'],10**12)
    for u,v,lo,hi,tag in edges:
        a,b=idx[u],idx[v]
        ei=dinic.add(a,b,hi-lo,tag)
        if tag is not None:
            ri=dinic.g[a][ei][2]
            witness.append((tag,b,ri,lo))
        # u must expel lo (burden -> drain), v receives lo (credit -> supply)
        balance[a]-=lo;balance[b]+=lo
    need=0
    for i,b in enumerate(balance):
        if b>0:
            dinic.add(S,i,b);need+=b
        elif b<0:
            dinic.add(i,T,-b)
    if dinic.maxflow(S,T)!=need:return False,{}
    # total realised flow = mandatory lower bound + residual reverse capacity
    flows={tag:lo+dinic.g[node][ri][1] for tag,node,ri,lo in witness}
    return True,flows


def _build_network(lane,barcodes,wells,matrix,barcode_spec,force=None):
    """Bounded transportation model for one lane's joint feasible assignments.

    Each originating well (samples + positive control) owns a pool equal to
    the reads it observed.  That pool is distributed to destination wells:
    locally (the diagonal) or as cross-talk through the measured upper-bound
    matrix, with one shared capacity per source->destination pair across all
    barcodes.  Blank controls do not originate DNA; they bound a shared
    background contamination pool.  Every destination's observed reads are
    conserved exactly by the demand edges.
    """
    samples=[w for w in wells if w['role']=='sample']
    blanks=[w for w in wells if w['role'] in ('extraction_blank','pcr_blank')]
    controls=[w for w in wells if w['role']=='positive_control']
    origins=samples+controls
    destinations=samples+controls
    bg_cap={bc:sum(w['reads'].get(bc,0) for w in blanks) for bc in barcodes}
    nodes=['src','snk']
    for bc in barcodes:
        nodes.append(f'sup:{bc}')
        if bg_cap[bc]:
            nodes.append(f'bg:{bc}')
        for a in origins:
            nodes.append(f'so:{a["well_id"]}:{bc}')
        for a in origins:
            for b in destinations:
                if a['well_id']!=b['well_id']:
                    nodes.append(f'xpin:{a["well_id"]}>{b["well_id"]}:{bc}')
        if bg_cap[bc]:
            for w in samples:
                nodes.append(f'bgout:{w["well_id"]}:{bc}')
        for w in destinations:
            nodes.append(f'dem:{w["well_id"]}:{bc}')
    for a in origins:
        for b in destinations:
            if a['well_id']!=b['well_id']:
                nodes.append(f'capin:{a["well_id"]}>{b["well_id"]}')
                nodes.append(f'capout:{a["well_id"]}>{b["well_id"]}')
    edges=[]
    control_ids={w['well_id'] for w in controls}
    for bc in barcodes:
        edges.append(('src',f'sup:{bc}',0,10**12,('sup',bc)))
        if bg_cap[bc]:
            edges.append(('src',f'bg:{bc}',0,bg_cap[bc],('bgsup',bc)))
        for a in origins:
            # Origin pool: the reads the source carries plus bounded leakage
            # headroom.  Cross-talk is a shared-lane artifact explained by the
            # matrix; the exact-demand edges below guarantee no read is counted
            # at two destinations and no read is invented.
            obs=a['reads'].get(bc,0)
            leak=sum(matrix.get(a['well_id'],{}).get(b['well_id'],0)
                     for b in destinations if b['well_id']!=a['well_id'])
            if a['role']=='positive_control':
                cap=obs+leak
            else:
                cap=obs+min(obs,leak)
            edges.append((f'sup:{bc}',f'so:{a["well_id"]}:{bc}',0,cap,
                          ('srcpool',a['well_id'],bc)))
        for w in destinations:
            obs=w['reads'].get(bc,0)
            if obs:
                edges.append((f'dem:{w["well_id"]}:{bc}','snk',obs,obs,('demand',w['well_id'],bc)))
        spec=barcode_spec[bc];is_control_bc=spec['identity']=='control'
        for a in origins:
            # diagonal local retention; control amplicons are local only at the
            # positive control well
            forced=force[2] if force and a['well_id']==force[0] and bc==force[1] else 0
            if is_control_bc and a['well_id'] not in control_ids:
                lo,local_hi=0,0
            else:
                lo=forced
                local_hi=max(a['reads'].get(bc,0),forced)
            edges.append((f'so:{a["well_id"]}:{bc}',f'dem:{a["well_id"]}:{bc}',
                          lo,local_hi,('local',a['well_id'],bc)))
            # off-diagonal cross-talk; only the control emits control amplicons
            for b in destinations:
                if a['well_id']==b['well_id']:continue
                if is_control_bc and a['well_id'] not in control_ids:continue
                matrix_cap=matrix.get(a['well_id'],{}).get(b['well_id'],0)
                hi=min(matrix_cap,a['reads'].get(bc,0))
                edges.append((f'so:{a["well_id"]}:{bc}',
                              f'xpin:{a["well_id"]}>{b["well_id"]}:{bc}',0,hi,
                              ('xtalk',a['well_id'],b['well_id'],bc)))
                edges.append((f'xpin:{a["well_id"]}>{b["well_id"]}:{bc}',
                              f'capin:{a["well_id"]}>{b["well_id"]}',0,10**9,None))
        if bg_cap[bc]:
            for b in samples:
                edges.append((f'bg:{bc}',f'bgout:{b["well_id"]}:{bc}',0,10**9,None))
                edges.append((f'bgout:{b["well_id"]}:{bc}',f'dem:{b["well_id"]}:{bc}',
                              0,10**9,('bg',b['well_id'],bc)))
    # shared cross-talk capacity per off-diagonal pair, spanning all barcodes
    for a in origins:
        for b in destinations:
            if a['well_id']==b['well_id']:continue
            bound=matrix.get(a['well_id'],{}).get(b['well_id'],0)
            edges.append((f'capin:{a["well_id"]}>{b["well_id"]}',
                          f'capout:{a["well_id"]}>{b["well_id"]}',0,bound,None))
            for bc in barcodes:
                edges.append((f'capout:{a["well_id"]}>{b["well_id"]}',
                              f'dem:{b["well_id"]}:{bc}',0,10**9,
                              ('xtalkout',a['well_id'],b['well_id'],bc)))
    return nodes,edges


def _self_range(lane,barcodes,wells,matrix,barcode_spec,well_id,bc):
    """Inclusive [lower, upper] of genuinely local reads for one well/barcode."""
    obs=next(w for w in wells if w['well_id']==well_id)['reads'].get(bc,0)
    if obs==0:return 0,0
    # control amplicons have an exclusive external origin at the control well
    well=next(w for w in wells if w['well_id']==well_id)
    if barcode_spec[bc]['identity']=='control' and well['role']!='positive_control':
        return 0,0
    def feasible(k):
        # pin local flow to exactly k via a [k,k] bounded edge
        nodes,edges=_build_network(lane,barcodes,wells,matrix,barcode_spec,force=(well_id,bc,k))
        edges=[(u,v,(k if tag==('local',well_id,bc) else lo),
                (k if tag==('local',well_id,bc) else hi),tag) for u,v,lo,hi,tag in edges]
        ok,_=_bounded_feasible(nodes,edges)
        return ok
    lo,hi=0,obs
    first=None
    while lo<=hi:
        mid=(lo+hi)//2
        if feasible(mid):first=mid;hi=mid-1
        else:lo=mid+1
    if first is None:raise ValueError('lane_infeasible')
    last=first;lo,hi=first,obs
    while lo<=hi:
        mid=(lo+hi)//2
        if feasible(mid):last=mid;lo=mid+1
        else:hi=mid-1
    return first,last


def solve_lane(doc):
    """Re-solve the full joint feasible assignment set for one lane document."""
    lane=doc['lane_id'];matrix=doc['cross_talk_upper'];barcodes=doc['barcodes']
    wells=doc['wells']
    barcode_spec={b['code']:b for b in barcodes};codes=[b['code'] for b in barcodes]
    for w in wells:
        for bc in w['reads']:
            if bc not in barcode_spec:raise ValueError('barcode_unknown')
    # one unrestricted run proves lane feasibility and yields a conserved witness
    nodes,edges=_build_network(lane,codes,wells,matrix,barcode_spec)
    ok,witness=_bounded_feasible(nodes,edges)
    if not ok:raise ValueError('lane_infeasible')
    blank_ids={w['well_id'] for w in wells if 'blank' in w['role']}
    # exact per-destination conservation: local + cross-talk + background inflow
    inflow={}
    for k,v in witness.items():
        if not v:continue
        if k[0]=='local':inflow[(k[1],k[2])]=inflow.get((k[1],k[2]),0)+v
        elif k[0]=='xtalkout':inflow[(k[2],k[3])]=inflow.get((k[2],k[3]),0)+v
        elif k[0]=='bg':inflow[(k[1],k[2])]=inflow.get((k[1],k[2]),0)+v
    for w in wells:
        if w['well_id'] in blank_ids:continue
        for bc,n in w['reads'].items():
            if inflow.get((w['well_id'],bc),0)!=n:
                raise ValueError('conservation_violation')
    total_observed=sum(sum(w['reads'].values()) for w in wells if w['well_id'] not in blank_ids)
    total_assigned=sum(inflow.values())
    conservation={'observed_reads':total_observed,'assigned_reads':total_assigned,
                  'background_cap':sum(sum(w['reads'].values()) for w in wells if w['well_id'] in blank_ids)}
    calibration=_calibrate(doc,wells,codes,barcode_spec)
    plots={}
    for w in wells:
        if w['role']!='sample':continue
        lower,upper,unattr=0,0,0
        bounds={}
        for bc in codes:
            lo,hi=_self_range(lane,codes,wells,matrix,barcode_spec,w['well_id'],bc)
            bounds[bc]=[lo,hi]
            spec=barcode_spec[bc]
            if spec['identity']=='invasive':
                lower+=lo;upper+=hi
            elif spec['identity']=='ambiguous':
                # indistinguishable reads stay inside the candidate set:
                # never promoted to an invasive call, counted as unattributed
                unattr+=w['reads'].get(bc,0)
        prob=_presence_probability(w,codes,calibration)
        plots[w['plot_id']]={'site':w['site'],'invasive_lower':lower,'invasive_upper':upper,
                             'unattributed_reads':unattr,'local_bounds':bounds,
                             'presence_probability':prob}
    return {'lane_id':lane,'ref_version':doc['ref_version'],'plots':plots,
            'conservation':conservation,'calibration':calibration,'witness':witness}


def _calibrate(doc,wells,codes,spec):
    """Independent positive-control calibration of the realised cross-talk rate.

    The control's own amplicon only appears elsewhere through cross-talk, so
    leaked control reads over control reads estimate the per-read transfer
    rate.  Blanks independently estimate the shared background contamination
    rate per observed read.
    """
    pc=next((w for w in wells if w['role']=='positive_control'),None)
    blanks=[w for w in wells if w['role'] in ('extraction_blank','pcr_blank')]
    blank_total=sum(sum(w['reads'].values()) for w in blanks)
    sample_total=sum(sum(w['reads'].values()) for w in wells if w['role']=='sample')
    background_rate=blank_total/sample_total if sample_total else 0.0
    if pc is None:
        return {'cross_talk_rate':background_rate,'background_rate':background_rate,
                'control_well':None,'leaked_reads':0,'control_reads':0}
    control_code=next(bc for bc in codes if spec[bc]['identity']=='control')
    control_reads=pc['reads'].get(control_code,0)
    leaked=sum(w['reads'].get(control_code,0) for w in wells if w['well_id']!=pc['well_id'])
    rate=leaked/control_reads if control_reads else 0.0
    return {'cross_talk_rate':rate,'background_rate':background_rate,
            'control_well':pc['well_id'],'leaked_reads':leaked,'control_reads':control_reads}


def _presence_probability(well,codes,calibration):
    """Calibrated probability at least one observed read is genuinely local.

    Under the independent-error model, cross-talk/background explain a read
    with rate p; the probability all n reads are non-local is p**n.  The
    result is always strictly below 1.0 for unconfirmed wells — a calibrated
    hot-spot, never a proof, so the no-local-event mass is floored at 1e-4.
    """
    rate=calibration['cross_talk_rate']+calibration['background_rate']
    n=sum(well['reads'].get(bc,0) for bc in codes)
    if n==0:return 0.0
    rate=min(max(rate,1e-12),0.9999)
    p_none=max(math.exp(n*math.log(rate)),1e-4)
    return round(1.0-p_none,6)


# --------------------------------------------------------------------------- #
# Persistence: every lane revision re-solves the whole joint feasible set.
# --------------------------------------------------------------------------- #
def import_lane(path,file):
    with closing(connect(path)) as db,db:
        doc,fresh=evidence(db,file,'edna_lane')
        if not fresh:return {'id':doc['id'],'replayed':True}
        lane=doc['lane_id']
        prev=db.execute('SELECT MAX(version) FROM edna_lane_history WHERE lane_id=?',(lane,)).fetchone()[0]
        version=(prev or 0)+1
        for w in doc['wells']:
            if w['role']=='sample' and not db.execute('SELECT 1 FROM plot WHERE id=?',(w['plot_id'],)).fetchone():
                raise ValueError('unknown_plot')
        result=solve_lane(doc)
        db.execute('INSERT INTO edna_lane VALUES(?,?,?,?,?,?,?)',
                   (lane,doc['ref_version'],version,json.dumps(doc['cross_talk_upper']),
                    json.dumps(doc['barcodes']),json.dumps(result['calibration']),doc['id']))
        for w in doc['wells']:
            db.execute('INSERT INTO edna_lane_well VALUES(?,?,?,?,?,?,?,?,?)',
                       (lane,version,w['well_id'],w['role'],w.get('plot_id'),w.get('site'),
                        w.get('sample_id'),json.dumps(w['reads']),doc['id']))
        db.execute('INSERT INTO edna_lane_history VALUES(?,?,?,?)',
                   (lane,version,doc['ref_version'],doc['id']))
        for plot_id,r in result['plots'].items():
            lower_map={bc:b[0] for bc,b in r['local_bounds'].items()}
            upper_map={bc:b[1] for bc,b in r['local_bounds'].items()}
            db.execute('INSERT INTO edna_attribution VALUES(?,?,?,?,?,?,?,?,?,?,?)',
                       (lane,version,plot_id,r['site'],r['invasive_lower'],r['invasive_upper'],
                        r['unattributed_reads'],json.dumps(lower_map),json.dumps(upper_map),
                        r['presence_probability'],doc['id']))
        return {'lane_id':lane,'version':version,'status':'joint_set_solved',
                'plots':result['plots'],'conservation':result['conservation']}


# --------------------------------------------------------------------------- #
# Two authorised output pathways — identical inputs and hard constraints.
# --------------------------------------------------------------------------- #
HOTSPOT_THRESHOLD=0.8


def _current_attribution(db):
    rows=[dict(r) for r in db.execute('SELECT * FROM edna_attribution ORDER BY lane_id,version,plot_id')]
    latest={}
    for r in rows:
        key=(r['lane_id'],r['plot_id'])
        if key not in latest or r['version']>latest[key]['version']:
            latest[key]=r
    return list(latest.values())


def conservative_plan(path):
    """Publish only conclusions true in *every* feasible assignment."""
    with closing(connect(path)) as db:
        attrs=_current_attribution(db)
    confirmed_sites,priority_sites,rerun_lanes=[],[],set()
    for r in attrs:
        entry={'plot_id':r['plot_id'],'site':r['site'],'lane_id':r['lane_id'],
               'invasive_lower':r['invasive_lower'],'invasive_upper':r['invasive_upper'],
               'unattributed_reads':r['unattributed_reads']}
        if r['invasive_lower']>0:
            entry['verdict']='confirmed_present'
            confirmed_sites.append(entry)
        else:
            entry['verdict']='unconfirmed_priority_check'
            priority_sites.append(entry)
            if r['invasive_upper']>0 or r['unattributed_reads']>0:
                rerun_lanes.add(r['lane_id'])
    return {'pathway':'conservative_corroboration',
            'confirmed_sites':confirmed_sites,'unconfirmed_sites':priority_sites,
            'field_visit_sites':confirmed_sites,
            'field_visit_count':len(confirmed_sites),
            'rerun_lanes':sorted(rerun_lanes),
            'certainty':'only conclusions holding in every feasible assignment are published'}


def probabilistic_plan(path):
    """Keep the whole feasible set; rank hotspots with control-calibrated odds."""
    with closing(connect(path)) as db:
        attrs=_current_attribution(db)
    hotspots,confirmed_sites=[],[]
    for r in attrs:
        entry={'plot_id':r['plot_id'],'site':r['site'],'lane_id':r['lane_id'],
               'invasive_lower':r['invasive_lower'],'invasive_upper':r['invasive_upper'],
               'unattributed_reads':r['unattributed_reads'],
               'presence_probability':round(r['presence_probability'],6)}
        if r['invasive_lower']>0:
            entry['verdict']='confirmed_present';confirmed_sites.append(entry)
        elif r['presence_probability']>=HOTSPOT_THRESHOLD:
            # a hotspot to verify — never written up as a confirmed invasive
            entry['verdict']='hotspot_to_verify';hotspots.append(entry)
        else:
            entry['verdict']='below_hotspot_threshold';hotspots.append(entry)
    visits=confirmed_sites+[h for h in hotspots if h['verdict']=='hotspot_to_verify']
    return {'pathway':'probabilistic_patrol',
            'confirmed_sites':confirmed_sites,'hotspots':hotspots,
            'field_visit_sites':visits,
            'field_visit_count':len(visits),
            'rerun_lanes':[],
            'certainty':'calibrated patrol priorities; unconfirmed plots are never stated as confirmed'}


def naive_blank_subtraction(path):
    """The deliberately wrong reference: subtract a fixed blank per sample.

    Independent per-sample subtraction ignores that high-abundance cross-talk
    shares one source total across the lane, so it fabricates confirmation on
    every plot even when the joint set says the reads could all be leakage.
    """
    with closing(connect(path)) as db:
        wells=[dict(r) for r in db.execute('SELECT * FROM edna_lane_well ORDER BY lane_id,version,well_id')]
        latest_ver={r['lane_id']:r['m'] for r in db.execute(
            'SELECT lane_id,MAX(version) AS m FROM edna_lane_well GROUP BY lane_id')}
    current=[w for w in wells if w['version']==latest_ver[w['lane_id']]]
    blanks={}
    for w in current:
        reads=json.loads(w['reads_json'])
        if 'blank' in w['role']:
            blanks[w['lane_id']]=blanks.get(w['lane_id'],0)+reads.get(INVASIVE,0)
    calls=[]
    for w in current:
        if w['role']!='sample':continue
        reads=json.loads(w['reads_json'])
        n=blanks.get(w['lane_id'],0)
        sample_count=sum(1 for x in current if x['lane_id']==w['lane_id'] and x['role']=='sample')
        per_sample=n//max(1,sample_count)
        residual=reads.get(INVASIVE,0)-per_sample
        calls.append({'plot_id':w['plot_id'],'site':w['site'],'observed':reads.get(INVASIVE,0),
                      'blank_subtracted':per_sample,'residual':residual,
                      'naive_call':'confirmed_positive' if residual>0 else 'negative'})
    return {'pathway':'naive_per_sample_blank_subtraction','calls':calls,
            'warning':'violates single-lane cross-talk conservation; do not publish'}
