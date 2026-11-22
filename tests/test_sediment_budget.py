import json,sqlite3,subprocess,sys
from pathlib import Path
import pytest
from tidalrestore.baseline import import_baseline
from tidalrestore.material import import_material
from tidalrestore.works import import_contract,import_work
from tidalrestore.survey import import_survey
from tidalrestore.shoreline import import_profile,import_bundle,import_budget,import_verification
from tidalrestore.store import report
from tidalrestore.audit import audit

FIX=Path(__file__).resolve().parent.parent/'fixtures'

def write(tmp,name,doc):
    p=tmp/name;p.write_text(json.dumps(doc),encoding='utf8');return p

def baseline_p2():
    return dict(id='b-p2',kind='baseline',plot_id='P2',area=120,season='spring',
                transects=[dict(id='u1',tier='high',coordinate='D',cover=25),
                           dict(id='u2',tier='mid',coordinate='E',cover=15),
                           dict(id='u3',tier='low',coordinate='F',cover=5)])

def setup(tmp):
    db=tmp/'budget.sqlite'
    import_baseline(db,FIX/'baseline-ok.json')
    import_baseline(db,write(tmp,'p2.json',baseline_p2()))
    import_bundle(db,FIX/'shore-two-periods.json')
    return db

def plots(db):
    return {r['plot_id']:r for r in report(db)['budget_plots']}

def routes(db):
    return {r['route_id']:r for r in report(db)['budget_routes']}

def layers(db):
    return {(r['layer'],r['item']):json.loads(r['value_json']) for r in report(db)['budget_layers']}

def verification(route_id,proven=20,vid=None):
    return dict(id=vid or f'ver-{route_id.lower()}-1',kind='shore_verification',budget_id='SB1',
                route_id=route_id,proven_m3=proven,verified_at='2026-05-10T09:00:00Z',verifier='crew-x')

def budget_doc():
    bundle=json.loads((FIX/'shore-two-periods.json').read_text())
    return next(d for d in bundle['documents'] if d['kind']=='shore_budget')

# 验收一:固定起点 baseline→shore_profile→report/audit 与 unknown_plot 对照
def test_profile_without_baseline_rejected(tmp_path):
    with pytest.raises(ValueError,match='unknown_plot'):
        import_profile(tmp_path/'x.sqlite',FIX/'shore-profile-ok.json')

def test_seed_flow_still_intact(tmp_path):
    db=tmp_path/'seed.sqlite'
    import_baseline(db,FIX/'baseline-ok.json')
    assert import_profile(db,FIX/'shore-profile-ok.json')['point_count']==3
    assert report(db)['shore_profiles'][0]['water_level_cm']==25
    assert audit(db)['status']=='matched'

# 验收二:不等距剖面、跨潮位、共用误差、缺测外海边界下的可核体积区间
def test_two_period_volume_intervals(tmp_path):
    db=setup(tmp_path)
    p=plots(db)
    assert (p['P1']['change_lo'],p['P1']['change_hi'])==(-50.0,-30.0)
    assert (p['P2']['change_lo'],p['P2']['change_hi'])==(20.0,40.0)
    assert (p['P1']['unattributable_lo'],p['P1']['unattributable_hi'])==(5.0,40.0)
    assert (p['P2']['unattributable_lo'],p['P2']['unattributable_hi'])==(0.0,30.0)
    layer=layers(db)
    v1,v2=layer[('plot_volume','P1')],layer[('plot_volume','P2')]
    assert v1['half_width_m3']==10.0 and v2['half_width_m3']==10.0
    # 同一设备同一航次的系统误差是共同变量:两个地块的区间并非独立
    shared=set(v1['shared_variables'])&set(v2['shared_variables'])
    assert shared=={'sonar-1|V1|vertical','sonar-1|V1|clock','sonar-1|V1|control',
                    'sonar-1|V2|vertical','sonar-1|V2|clock','sonar-1|V2|control'}
    # 潮位基准统一:同一剖面原件水位不同,派生层保留原始面积与基准化面积
    pa=layer[('profile_area','SP-P1A-T1')]
    assert pa['water_level_cm']==54 and pa['raw_area_cm_m']==2820.0 and pa['datum_area_cm_m']==4980.0
    pa2=layer[('profile_area','SP-P1A-T2')]
    assert pa2['water_level_cm']==36 and pa2['datum_area_cm_m']==pa2['raw_area_cm_m']+36*40
    # 跨潮位配对后的面积变化(不等距点精确积分)
    assert layer[('profile_pair','P1@25')]['area_change_cm_m']==-30.0
    assert layer[('profile_pair','P1@75')]['area_change_cm_m']==-50.0
    assert layer[('boundary','BND-P1P2')]['measured_m3']==[10.0,25.0]
    # 未测到的外海边界:列为补测位置,不通量归零
    res=[(r['plot_id'],r['axis'],r['reason']) for r in report(db)['budget_resurvey']]
    assert ('P1','cross_shore','offshore_exit_unmeasured') in res
    assert ('P2','cross_shore','offshore_exit_unmeasured') in res
    assert audit(db)['status']=='matched'

# 验收三:同输入两条授权补测路线先并存,补测后分别给不同外购量与转移归属
def test_routes_pending_then_route_a_confirmed(tmp_path):
    db=setup(tmp_path)
    rs=routes(db)
    assert set(rs)=={'RA','RB'} and all(r['status']=='pending' for r in rs.values())
    assert report(db)['shore_budgets'][0]['status']=='pending_verification'
    a=json.loads(rs['RA']['scenario_json']);b=json.loads(rs['RB']['scenario_json'])
    # 两种可行预算同时保留,均未宣布为事实
    assert a['status']==b['status']=='conditional'
    assert a['purchases_m3']=={'P1':0,'P2':5}
    assert b['purchases_m3']=={'P1':20,'P2':25}
    p=plots(db)
    assert (p['P1']['purchase_lo'],p['P1']['purchase_hi'])==(0.0,20.0)
    assert (p['P2']['purchase_lo'],p['P2']['purchase_hi'])==(5.0,25.0)
    # 甲路线补测到位:区内转移成立,P2 无需同量外购
    import_verification(db,write(tmp_path,'ver-a.json',verification('RA')))
    rs=routes(db)
    assert rs['RA']['status']=='confirmed' and rs['RA']['proven_m3']==20
    assert rs['RB']['status']=='void'
    scen=json.loads(rs['RA']['scenario_json'])
    assert scen['status']=='confirmed'
    assert scen['attribution']['internal_transfer_m3']==20
    assert scen['unknown_flux']['residual_loss_m3']==[10.0,30.0]
    assert scen['unknown_flux']['offshore_exit']=='unknown'
    p=plots(db)
    assert (p['P1']['purchase_lo'],p['P1']['purchase_hi'])==(0.0,0.0)
    assert (p['P2']['purchase_lo'],p['P2']['purchase_hi'])==(5.0,5.0)
    # 互斥:同一份 P1 缺口不能再记外海流失
    with pytest.raises(ValueError,match='budget_already_verified'):
        import_verification(db,write(tmp_path,'ver-b.json',verification('RB',vid='ver-rb-late')))
    assert audit(db)['status']=='matched'

def test_route_b_offshore_loss_no_offset(tmp_path):
    db=setup(tmp_path)
    import_verification(db,write(tmp_path,'ver-b.json',verification('RB')))
    rs=routes(db)
    assert rs['RB']['status']=='confirmed' and rs['RA']['status']=='void'
    scen=json.loads(rs['RB']['scenario_json'])
    assert scen['attribution']['offshore_loss_verified_m3']==20
    assert scen['attribution']['cross_plot_credit']==False
    assert scen['unknown_flux']['residual_loss_m3']==[0.0,20.0]
    p=plots(db)
    # P1 需订补沙;P2 的现有增量不能抵扣,仍按原指标外购
    assert (p['P1']['purchase_lo'],p['P1']['purchase_hi'])==(20.0,20.0)
    assert (p['P2']['purchase_lo'],p['P2']['purchase_hi'])==(25.0,25.0)
    assert audit(db)['status']=='matched'

def test_verification_beyond_supportable_rejected(tmp_path):
    db=setup(tmp_path)
    with pytest.raises(ValueError,match='verification_exceeds_supportable'):
        import_verification(db,write(tmp_path,'ver-big.json',verification('RA',proven=30)))
    with pytest.raises(ValueError,match='unknown_route'):
        import_verification(db,write(tmp_path,'ver-x.json',verification('RX')))

# 验收四:P1/P2 边界守恒、共边不双计、未知不作零
def test_boundary_conservation_no_double_count_unknown_not_zero(tmp_path):
    db=setup(tmp_path)
    p=plots(db)
    # 边界守恒:同一转移区间在两侧各记一次,互为镜像
    assert (p['P1']['transfer_lo'],p['P1']['transfer_hi'])==(10.0,25.0)
    assert (p['P2']['transfer_lo'],p['P2']['transfer_hi'])==(10.0,25.0)
    layer=layers(db)
    # 共边不双计:边界剖面只进边界角色,不进入任一地块体积
    pair_profiles=set()
    for (layer_name,_),value in layer.items():
        if layer_name=='profile_pair':pair_profiles|={value['t1_profile'],value['t2_profile']}
    assert 'SPB-T1' not in pair_profiles and 'SPB-T2' not in pair_profiles
    # 未知不作零:P1 缺口没有被转移加零平账,外海通量保持 unknown
    assert p['P1']['unattributable_hi']==40.0
    a=json.loads(routes(db)['RA']['scenario_json'])
    assert a['unknown_flux']['offshore_exit']=='unknown'
    # 角色冲突:同一剖面不能既算地块体积又算边界
    doc=budget_doc();doc['id']='sb2';doc['budget_id']='SB2'
    doc['shared_boundaries'][0]['profiles']['T1']='SP-P1A-T1'
    with pytest.raises(ValueError,match='profile_role_conflict'):
        import_budget(db,write(tmp_path,'dup.json',doc))
    # 地块沿岸跨度重叠被拒绝
    doc=budget_doc();doc['id']='sb3';doc['budget_id']='SB3'
    doc['plots'][1]['alongshore_m']=[50,200]
    with pytest.raises(ValueError,match='plot_span_overlap'):
        import_budget(db,write(tmp_path,'overlap.json',doc))
    assert audit(db)['status']=='matched'

# 验收五:修改原始点列或派生预算能在回放中定位差异
def test_replay_locates_tampered_raw_points(tmp_path):
    db=setup(tmp_path)
    con=sqlite3.connect(db)
    raw=con.execute("SELECT raw FROM evidence WHERE id='ev-sp-p1a-t2'").fetchone()[0]
    doc=json.loads(raw);doc['points'][1]['elevation_cm']=999
    con.execute("UPDATE evidence SET raw=? WHERE id='ev-sp-p1a-t2'",(json.dumps(doc).encode(),))
    con.commit();con.close()
    result=audit(db)
    assert result['status']=='divergent'
    assert result['tampered_evidence']==['ev-sp-p1a-t2']

def test_replay_locates_tampered_derived_budget(tmp_path):
    db=setup(tmp_path)
    con=sqlite3.connect(db)
    con.execute("UPDATE budget_plot SET change_lo=-999 WHERE plot_id='P1'")
    con.commit();con.close()
    result=audit(db)
    assert result['status']=='divergent'
    assert 'budget_plots' in result['differences']

# 既有业务裁决不被未核实的声呐变化量覆盖
def test_sonar_budget_does_not_override_acceptance(tmp_path):
    db=tmp_path/'x.sqlite'
    import_baseline(db,FIX/'baseline-ok.json')
    import_material(db,write(tmp_path,'m.json',dict(id='m1',kind='material',batch_id='M1',species='native-grass',
                                                   origin='local',quantity=100,valid_until='2026-12-31T00:00:00Z')))
    import_contract(db,write(tmp_path,'c.json',dict(id='c1',kind='contract',contract_id='C1',plot_id='P1',
        species='native-grass',planned_area=60,window_start='2026-04-01T08:00:00Z',window_end='2026-04-01T12:00:00Z',
        repair_start='2026-04-04T08:00:00Z',repair_end='2026-04-05T12:00:00Z',contractor='build-a')))
    import_work(db,write(tmp_path,'w.json',dict(id='w1',kind='work',event_id='W1',contract_id='C1',event_type='install',
        area=60,at='2026-04-01T10:00:00Z',material_id='M1',
        cells=[dict(id='C-H',tier='high',area=20),dict(id='C-M',tier='mid',area=20),dict(id='C-L',tier='low',area=20)])))
    decision=import_survey(db,write(tmp_path,'s.json',dict(id='s1',kind='survey',survey_id='S1',plot_id='P1',
        season='spring',observer='eco-b',high_alive=80,mid_alive=80,low_alive=80,at='2026-04-03T10:00:00Z',
        sample_coords=dict(high='A',mid='B',low='C'))))
    assert decision['status']=='accepted'
    import_baseline(db,write(tmp_path,'p2.json',baseline_p2()))
    import_bundle(db,FIX/'shore-two-periods.json')
    import_verification(db,write(tmp_path,'ver-a.json',verification('RA')))
    # 声呐派生预算已确认,但验收裁决保持原样
    assert report(db)['acceptance'][0]['status']=='accepted'
    assert report(db)['acceptance'][0]['weighted_survival']==80
    assert audit(db)['status']=='matched'

def test_cli_bundle_report_audit(tmp_path):
    db=tmp_path/'cli.sqlite'
    run=lambda *a:subprocess.run([sys.executable,'-m','tidalrestore',*a],capture_output=True,text=True)
    assert run('baseline-import','--db',str(db),'--file',str(FIX/'baseline-ok.json')).returncode==0
    assert run('baseline-import','--db',str(db),'--file',str(write(tmp_path,'p2.json',baseline_p2()))).returncode==0
    out=run('shore-bundle-import','--db',str(db),'--file',str(FIX/'shore-two-periods.json'))
    assert out.returncode==0,out.stderr
    assert json.loads(out.stdout)['imported']==12
    rep=json.loads(run('report','--db',str(db)).stdout)
    assert len(rep['shore_profiles'])==10
    assert {r['plot_id'] for r in rep['budget_plots']}=={'P1','P2'}
    assert json.loads(run('audit','--db',str(db)).stdout)['status']=='matched'
