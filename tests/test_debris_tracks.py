import json,subprocess,sys
from pathlib import Path
from contextlib import closing
import pytest
from tidalrestore.baseline import import_baseline
from tidalrestore.material import import_material
from tidalrestore.works import import_contract,import_work
from tidalrestore.survey import import_survey
from tidalrestore.debris import import_observation,import_track,import_plan,import_recovery,import_handover
from tidalrestore.store import report,connect
from tidalrestore.audit import audit

FIX=Path(__file__).resolve().parent.parent/'fixtures'

def write(tmp,name,doc):
    p=tmp/name;p.write_text(json.dumps(doc),encoding='utf8');return p

def p2_baseline():
    return dict(id='b-p2',kind='baseline',plot_id='P2',area=80,season='spring',
                transects=[dict(id='t4',tier='high',coordinate='D',cover=25),
                           dict(id='t5',tier='mid',coordinate='E',cover=15),
                           dict(id='t6',tier='low',coordinate='F',cover=5)])

def setup(tmp):
    db=tmp/'t.sqlite'
    import_baseline(db,FIX/'baseline-ok.json')
    import_baseline(db,write(tmp,'p2.json',p2_baseline()))
    assert import_track(db,FIX/'debris-cross-camera.json')['status']=='captured'
    return db

def plan_doc():
    return dict(id='plan-src-1',kind='debris-plan',plan_id='OP1',track_id='T1',crew='crew-7',shift='2026-04-03T10:30:00Z',
                routes=[dict(route_id='R-net',action='net-boundary',at_m=60,deploy_at='2026-04-03T10:35:00Z'),
                        dict(route_id='R-boat',action='boat-verify',target_obs='D-B',depart_at='2026-04-03T10:35:00Z')])

def recovery(rid,bag,boat,at,pos,pieces,claims):
    return dict(id=rid,kind='debris-recovery',track_id='T1',bag_id=bag,boat=boat,at=at,position_m=pos,pieces=pieces,claims=claims)

def handover(rid,receipt,sack,at,bags,total,weight):
    return dict(id=rid,kind='debris-handover',track_id='T1',receipt_id=receipt,sack_id=sack,at=at,bags=bags,total_pieces=total,weight_kg=weight)

def hyps(db):return report(db)['debris_hypotheses']
def rel(h,obs):return h['doc']['relations'][obs]['relation']
def parents(h,obs):return h['doc']['relations'][obs]['parents']

# 一、固定副本 D1 采录/report/audit 与缺基线拒绝
def test_legacy_capture_report_audit_and_missing_baseline(tmp_path):
    db=tmp_path/'d.sqlite'
    with pytest.raises(ValueError,match='unknown_plot'):
        import_observation(db,FIX/'debris-ok.json')
    import_baseline(db,FIX/'baseline-ok.json')
    assert import_observation(db,FIX/'debris-ok.json')['status']=='captured'
    assert import_observation(db,FIX/'debris-ok.json')['replayed']
    row=report(db)['debris'][0]
    assert (row['id'],row['camera'],row['position_m'],row['pieces'])==('D1','cam-east',12,2)
    assert audit(db)['status']=='matched'

def test_track_requires_known_plots(tmp_path):
    db=tmp_path/'x.sqlite'
    import_baseline(db,FIX/'baseline-ok.json')
    with pytest.raises(ValueError,match='unknown_plot'):
        import_track(db,FIX/'debris-cross-camera.json')

# 二、潮流反向、桥墩遮挡、物体分裂合并和两镜头交叠下的身份与件数范围
def test_identity_hypotheses_reversal_occlusion_split_merge_overlap(tmp_path):
    db=setup(tmp_path)
    hs=hyps(db)
    assert len(hs)>10 and all(h['status']=='unverified' for h in hs)
    # A=B(同一物体顺流通过桥墩遮挡段)与 A≠B(遮挡期间上游入口送入 B)两种互斥假设都保留
    assert any(rel(h,'D-B')=='continue' for h in hs)
    assert any(rel(h,'D-B')=='new' and parents(h,'D-B')==['inlet:upstream-inlet'] for h in hs)
    # 潮流反向把残件带回东镜头:按分裂残件链接到同一物体,而不是凭 shape 判成第二个来源
    assert any(rel(h,'D-R')=='split' and 'D-B' in parents(h,'D-R') for h in hs)
    assert any(rel(h,'D-R')=='split' and 'D-A' in parents(h,'D-R') for h in hs)
    # 几件并成一团:合并假设存在且件数守恒(根物体件数=末端分支件数)
    assert any(rel(h,'D-M')=='merge' for h in hs)
    for h in hs:
        doc=h['doc']
        new_pieces=sum(o['pieces'] for o in report(db)['debris_tracks'][0]['doc']['observations'] if doc['relations'][o['obs_id']]['relation']=='new')
        assert doc['total_pieces']==new_pieces==sum(b['pieces'] for b in doc['branches'])
        # 两镜头视场交叠区(75-85 m)内的 D-R 只被计一次,不重复产生清理对象
        assert sum(1 for b in doc['branches'] if 'D-R' in b['observations'])==1
        assert doc['min_unintercepted']==doc['max_unintercepted']==doc['total_pieces']
    # 遮挡只意味着未知:A≠B 假设里 A 的 2 件仍在账上(unaccounted),没有被当成已清除
    assert any(rel(h,'D-B')=='new' and any(b['observations']==['D-A'] and b['pieces']==2 for b in h['doc']['branches']) for h in hs)
    ab=[h for h in hs if rel(h,'D-B')=='continue'][0]
    assert 'P2/P1@60' in ab['doc']['boundaries']
    assert {'P1','P2','occlusion:bridge-pier'}<=set(ab['doc']['segments'])
    inlet=[h for h in hs if rel(h,'D-B')=='new' and parents(h,'D-B')==['inlet:upstream-inlet']][0]
    assert 'inlet:upstream-inlet' in inlet['doc']['segments']
    # 每条假设都给出需要复拍的窗口
    assert all(h['doc']['reobserve'] for h in hs)
    assert all({'branch','camera','start','end'}<=set(w) for h in hs for w in h['doc']['reobserve'])
    assert audit(db)['status']=='matched'

# 三、同一输入两条授权作业路线产生不同实拦截位置及次班待办,并显示未证实假设;验收不被未核实影像改变
def test_two_authorised_routes_diverge_and_acceptance_untouched(tmp_path):
    db=tmp_path/'x.sqlite'
    for name,fn in [('baseline-ok.json',import_baseline),('material-ok.json',import_material),
                    ('contract-ok.json',import_contract),('work-ok.json',import_work),('survey-ok.json',import_survey)]:
        fn(db,FIX/name)
    assert report(db)['acceptance'][0]['status']=='accepted'
    before=report(db)['acceptance']
    import_baseline(db,write(tmp_path,'p2.json',p2_baseline()))
    import_track(db,FIX/'debris-cross-camera.json')
    result=import_plan(db,write(tmp_path,'plan.json',plan_doc()))
    assert result['status']=='authorized' and result['routes']==['R-net','R-boat']
    routes={r['route_id']:r['doc'] for r in report(db)['debris_plans']}
    net,boat=routes['R-net'],routes['R-boat']
    # 同一影像/潮速/人手下两条路线都合法,但实际拦截位置不同
    assert net['intercept_at_m']==60 and boat['intercept_at_m']==25
    # 次班待办不同:网式保留身份核查与西岸滩面清理,船核路线把东侧残件留给下一班
    assert net['next_shift']==['identify-netted-pieces','clean-west-shore']
    assert 'follow-up:D-R' in boat['next_shift'] and 'clean-west-shore' not in boat['next_shift']
    # 两路都明确列出仍未证实的互斥假设,而不是把件数相加派工
    unverified={h['id'] for h in hyps(db) if h['status']=='unverified'}
    assert set(net['keeps_hypotheses'])==set(boat['keeps_hypotheses'])==unverified and len(unverified)>1
    assert net['mutually_exclusive'] and boat['mutually_exclusive']
    assert boat['distinguishes']['linked'] and boat['distinguishes']['separate']
    assert any(x['interceptable_max']>0 for x in net['hypotheses'])
    # 未核实的漂浮影像不改变既有修复验收
    assert report(db)['acceptance']==before
    assert audit(db)['status']=='matched'

# 四、两船一岸站:封袋、失联、迟到和重复签收不得双计已清件数,待核件可追索到来源
def test_bag_seal_handover_closed_loop_no_double_count(tmp_path):
    db=setup(tmp_path)
    # 船乙在西侧封袋 2 件,船端"已清"标签引用了同一蓝捆编号 D-A
    assert import_recovery(db,write(tmp_path,'rec-w.json',recovery('rec-w','BAG-W','boat-west','2026-04-03T10:25:00Z',30,2,[{'obs_id':'D-A','pieces':2}])))['status']=='sealed'
    led=report(db)['debris_ledger']
    # 船端已清不能直接算清:岸站未签收前 confirmed 为 0
    assert sum(r['pieces'] for r in led if r['category']=='confirmed')==0
    assert sum(r['pieces'] for r in led if r['category']=='in_transit')==2
    # 船甲无线确认丢失:岸站先收到一袋三件的称重单(内装 BAG-E 与 BAG-W)
    sack=handover('rec-sack1','RC-1','SACK-1','2026-04-03T10:40:00Z',[{'bag_id':'BAG-E','pieces':1},{'bag_id':'BAG-W','pieces':2}],3,4.2)
    assert import_handover(db,write(tmp_path,'sack1.json',sack))['status']=='accepted'
    led=report(db)['debris_ledger']
    assert sum(r['pieces'] for r in led if r['category']=='confirmed')==2
    unsealed=[r for r in led if r['category']=='received_unsealed']
    assert len(unsealed)==1 and unsealed[0]['bag_id']=='BAG-E' and unsealed[0]['receipt_id']=='RC-1'
    # 同一称重单重复回传:按证据原件去重重放,不双计
    assert import_handover(db,write(tmp_path,'sack1.json',sack))['replayed']
    assert sum(r['pieces'] for r in report(db)['debris_ledger'] if r['category']=='confirmed')==2
    # 不同单号再次签收同一袋:记冲突且不双计
    dup=handover('rec-sack1b','RC-1B','SACK-1','2026-04-03T10:41:00Z',[{'bag_id':'BAG-W','pieces':2}],2,2.8)
    assert import_handover(db,write(tmp_path,'sack1b.json',dup))['status']=='accepted_with_conflicts'
    bags=report(db)['debris_bags']
    assert sum(b['pieces'] for b in bags if b['status'] in('received','received_unsealed'))==3
    # 船甲迟到的封袋回执在岸站签收之后到达(顺序颠倒),同样写 D-A 已清
    assert import_recovery(db,write(tmp_path,'rec-e.json',recovery('rec-e','BAG-E','boat-east','2026-04-03T10:20:00Z',90,1,[{'obs_id':'D-A','pieces':1}])))['status']=='received'
    led=report(db)['debris_ledger']
    confirmed=[r for r in led if r['category']=='confirmed']
    # 两份船端"已清"不可相加:D-A 只有 2 件,确认已清封顶 2 件,多出的 1 件挂为待核且可追索到袋/船/单
    assert sum(r['pieces'] for r in confirmed)==2 and all(r['obs_id']=='D-A' for r in confirmed)
    un=[r for r in led if r['category']=='unattributed_received']
    assert sum(r['pieces'] for r in un)==1 and un[0]['bag_id'] in('BAG-E','BAG-W') and un[0]['receipt_id']=='RC-1'
    unclaimed={r['obs_id']:r['pieces'] for r in led if r['category']=='unclaimed'}
    assert unclaimed=={'D-B':2,'D-R':1,'D-M':3}
    # 岸站收到 3 件不回填两镜头身份:A=B 与 A≠B 都仍保留;实物件数只淘汰总件数装不下的假设
    hs=hyps(db)
    assert any(h['status']=='unverified' and rel(h,'D-B')=='continue' for h in hs)
    open4=[h for h in hs if h['status']=='unverified' and rel(h,'D-B')=='new']
    assert open4 and all(h['doc']['total_pieces']>=4 for h in open4)
    # A≠B 时可能还有一件未捞:最小未拦截件数 ≥1
    assert all(h['doc']['min_unintercepted']>=1 for h in open4 if h['doc']['total_pieces']==4)
    # 次班实物交接继续淘汰:船乙再封 BAG-C(认 D-B 两件)并由 SACK-2 签收,总实物 5 件
    import_recovery(db,write(tmp_path,'rec-c.json',recovery('rec-c','BAG-C','boat-west','2026-04-03T11:05:00Z',20,2,[{'obs_id':'D-B','pieces':2}])))
    sack2=handover('rec-sack2','RC-2','SACK-2','2026-04-03T11:15:00Z',[{'bag_id':'BAG-C','pieces':2}],2,2.6)
    assert import_handover(db,write(tmp_path,'sack2.json',sack2))['status']=='accepted'
    hs=hyps(db)
    refuted=[h for h in hs if h['status']=='refuted']
    assert refuted and all(h['doc']['total_pieces']<5 for h in refuted)
    assert all(h['doc']['refute_reason']=='physical_received_exceeds_hypothesis' for h in refuted)
    still=[h for h in hs if h['status']=='unverified']
    assert any(rel(h,'D-B')=='continue' for h in still) and any(rel(h,'D-B')=='new' for h in still)
    led=report(db)['debris_ledger']
    assert sum(r['pieces'] for r in led if r['category']=='confirmed')==4
    assert {r['obs_id'] for r in led if r['category']=='unclaimed'}=={'D-R','D-M'}
    assert audit(db)['status']=='matched'

# 五、原始观测或实物交接投影篡改可定位
def test_tamper_is_located(tmp_path):
    db=setup(tmp_path)
    import_observation(db,FIX/'debris-ok.json')
    # 原始观测文件被改(同 id 不同字节):证据哈希冲突直接定位
    doc=json.loads((FIX/'debris-cross-camera.json').read_text(encoding='utf8'))
    doc['observations'][1]['pieces']=3
    with pytest.raises(ValueError,match='evidence_id_conflict'):
        import_track(db,write(tmp_path,'tampered.json',doc))
    # 交接投影被改:重放审计定位到具体投影表
    with closing(connect(db)) as conn,conn:
        conn.execute("UPDATE debris_hypothesis SET status='refuted' WHERE id=(SELECT MIN(id) FROM debris_hypothesis)")
    res=audit(db)
    assert res['status']=='divergent' and 'debris_hypotheses' in res['differences']
    with closing(connect(db)) as conn,conn:
        conn.execute("UPDATE debris_observation SET pieces=9 WHERE id='D1'")
    res=audit(db)
    assert res['status']=='divergent' and 'debris' in res['differences']

def test_cli_track_import_and_report(tmp_path):
    db=tmp_path/'cli.sqlite'
    import_baseline(db,FIX/'baseline-ok.json')
    import_baseline(db,write(tmp_path,'p2.json',p2_baseline()))
    out=subprocess.run([sys.executable,'-m','tidalrestore','debris-track-import','--db',str(db),'--file',str(FIX/'debris-cross-camera.json')],capture_output=True,text=True)
    assert out.returncode==0,out.stderr
    assert json.loads(out.stdout)['hypotheses']>0
    rep=subprocess.run([sys.executable,'-m','tidalrestore','report','--db',str(db)],capture_output=True,text=True)
    data=json.loads(rep.stdout)
    assert data['debris_hypotheses'] and data['debris_tracks'][0]['doc']['track_id']=='T1'
