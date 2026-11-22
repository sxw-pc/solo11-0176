import json,hashlib,sqlite3
from pathlib import Path
from contextlib import closing

SCHEMA='''
PRAGMA foreign_keys=ON;
CREATE TABLE IF NOT EXISTS evidence(id TEXT PRIMARY KEY,kind TEXT NOT NULL,sha TEXT NOT NULL,raw BLOB NOT NULL,seq INTEGER NOT NULL UNIQUE);
CREATE TABLE IF NOT EXISTS plot(id TEXT PRIMARY KEY,area INTEGER NOT NULL,season TEXT NOT NULL,baseline_status TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS baseline_transect(id TEXT PRIMARY KEY,plot_id TEXT NOT NULL REFERENCES plot(id),tier TEXT NOT NULL,coordinate TEXT NOT NULL,cover INTEGER NOT NULL,source_id TEXT NOT NULL REFERENCES evidence(id));
CREATE TABLE IF NOT EXISTS baseline_revision(id TEXT PRIMARY KEY,plot_id TEXT NOT NULL REFERENCES plot(id),revision INTEGER NOT NULL,evidence_id TEXT NOT NULL REFERENCES evidence(id),status TEXT NOT NULL,UNIQUE(plot_id,revision));
CREATE TABLE IF NOT EXISTS baseline_revision_transect(revision_id TEXT NOT NULL REFERENCES baseline_revision(id),tier TEXT NOT NULL,coordinate TEXT NOT NULL,cover INTEGER NOT NULL,PRIMARY KEY(revision_id,tier));
CREATE TABLE IF NOT EXISTS baseline_dispute(id TEXT PRIMARY KEY,plot_id TEXT NOT NULL,coordinate TEXT NOT NULL,reason TEXT NOT NULL,source_id TEXT NOT NULL REFERENCES evidence(id));
CREATE TABLE IF NOT EXISTS dispute_state(plot_id TEXT NOT NULL,tier TEXT NOT NULL,dispute_id TEXT NOT NULL REFERENCES baseline_dispute(id),status TEXT NOT NULL,PRIMARY KEY(plot_id,tier));
CREATE TABLE IF NOT EXISTS dispute_decision(id INTEGER PRIMARY KEY,plot_id TEXT NOT NULL,tier TEXT NOT NULL,version INTEGER NOT NULL,status TEXT NOT NULL,decision TEXT NOT NULL,coordinate TEXT,evidence_id TEXT,missing TEXT NOT NULL,reason TEXT NOT NULL,source_id TEXT NOT NULL REFERENCES evidence(id),UNIQUE(plot_id,tier,version));
CREATE TABLE IF NOT EXISTS recheck(id TEXT PRIMARY KEY,plot_id TEXT NOT NULL,tier TEXT NOT NULL,agency TEXT NOT NULL,authorization TEXT NOT NULL,supported_coordinate TEXT NOT NULL,computed_coordinate TEXT,clock_synced INTEGER NOT NULL,valid INTEGER NOT NULL,reason TEXT NOT NULL,sha TEXT NOT NULL,source_id TEXT NOT NULL REFERENCES evidence(id));
CREATE TABLE IF NOT EXISTS spatial_check(id INTEGER PRIMARY KEY,plot_id TEXT NOT NULL,tier TEXT NOT NULL,object_type TEXT NOT NULL,object_id TEXT NOT NULL,expected_coordinate TEXT NOT NULL,actual_coordinate TEXT,covered INTEGER NOT NULL,source_id TEXT NOT NULL REFERENCES evidence(id),UNIQUE(plot_id,tier,object_type,object_id));
CREATE TABLE IF NOT EXISTS material_batch(id TEXT PRIMARY KEY,species TEXT NOT NULL,origin TEXT NOT NULL,quantity INTEGER NOT NULL,valid_until TEXT NOT NULL,status TEXT NOT NULL,source_id TEXT NOT NULL REFERENCES evidence(id));
CREATE TABLE IF NOT EXISTS work_contract(id TEXT PRIMARY KEY,plot_id TEXT NOT NULL REFERENCES plot(id),material_species TEXT NOT NULL,planned_area INTEGER NOT NULL,window_start TEXT NOT NULL,window_end TEXT NOT NULL,repair_start TEXT NOT NULL,repair_end TEXT NOT NULL,contractor TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS work_event(id TEXT PRIMARY KEY,contract_id TEXT NOT NULL REFERENCES work_contract(id),event_type TEXT NOT NULL,area INTEGER NOT NULL,at TEXT NOT NULL,source_id TEXT NOT NULL REFERENCES evidence(id));
CREATE TABLE IF NOT EXISTS work_cell(id TEXT PRIMARY KEY,contract_id TEXT NOT NULL REFERENCES work_contract(id),tier TEXT NOT NULL,area INTEGER NOT NULL,material_id TEXT REFERENCES material_batch(id),status TEXT NOT NULL,source_id TEXT NOT NULL REFERENCES evidence(id),coordinate TEXT);
CREATE TABLE IF NOT EXISTS survey(id TEXT PRIMARY KEY,plot_id TEXT NOT NULL REFERENCES plot(id),season TEXT NOT NULL,observer TEXT NOT NULL,high_alive INTEGER NOT NULL,mid_alive INTEGER NOT NULL,low_alive INTEGER NOT NULL,observed_at TEXT NOT NULL,source_id TEXT NOT NULL REFERENCES evidence(id),high_coord TEXT,mid_coord TEXT,low_coord TEXT);
CREATE TABLE IF NOT EXISTS acceptance(plot_id TEXT PRIMARY KEY,status TEXT NOT NULL,weighted_survival INTEGER NOT NULL,reason TEXT NOT NULL,version INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS acceptance_snapshot(plot_id TEXT PRIMARY KEY,version INTEGER NOT NULL,status TEXT NOT NULL,score INTEGER NOT NULL,reason TEXT NOT NULL,source_id TEXT NOT NULL REFERENCES evidence(id));
CREATE TABLE IF NOT EXISTS acceptance_history(plot_id TEXT NOT NULL,version INTEGER NOT NULL,status TEXT NOT NULL,score INTEGER NOT NULL,reason TEXT NOT NULL,source_id TEXT NOT NULL REFERENCES evidence(id),PRIMARY KEY(plot_id,version));
CREATE TABLE IF NOT EXISTS acceptance_report(id INTEGER PRIMARY KEY,plot_id TEXT NOT NULL,version INTEGER NOT NULL,status TEXT NOT NULL,score INTEGER NOT NULL,reason TEXT NOT NULL,evidence_version TEXT NOT NULL,payload TEXT NOT NULL,source_id TEXT NOT NULL REFERENCES evidence(id));
CREATE TABLE IF NOT EXISTS decision_log(id INTEGER PRIMARY KEY,object_id TEXT NOT NULL,status TEXT NOT NULL,reason TEXT NOT NULL,evidence_id TEXT NOT NULL);
'''

def connect(path):
    p=Path(path);p.parent.mkdir(parents=True,exist_ok=True)
    db=sqlite3.connect(p);db.row_factory=sqlite3.Row;db.executescript(SCHEMA)
    # Databases created by earlier releases predate spatial survey/work columns.
    survey_cols={r['name'] for r in db.execute('PRAGMA table_info(survey)')}
    for col in ('high_coord','mid_coord','low_coord'):
        if col not in survey_cols:db.execute(f'ALTER TABLE survey ADD COLUMN {col} TEXT')
    work_cols={r['name'] for r in db.execute('PRAGMA table_info(work_cell)')}
    if 'coordinate' not in work_cols:db.execute('ALTER TABLE work_cell ADD COLUMN coordinate TEXT')
    return db

def evidence(db,file,kind):
    raw=Path(file).read_bytes();doc=json.loads(raw)
    if doc.get('kind')!=kind or not doc.get('id'):raise ValueError('evidence_kind_or_id')
    digest=hashlib.sha256(raw).hexdigest();old=db.execute('SELECT sha FROM evidence WHERE id=?',(doc['id'],)).fetchone()
    if old:
        if old['sha']!=digest:raise ValueError('evidence_id_conflict')
        return doc,False
    # The same bytes filed again under a different id is still the same
    # observation; it must not be counted twice (e.g. as a second agency).
    twin=db.execute('SELECT id FROM evidence WHERE sha=?',(digest,)).fetchone()
    if twin:raise ValueError('duplicate_evidence_bytes')
    seq=db.execute('SELECT COALESCE(MAX(seq),0)+1 FROM evidence').fetchone()[0]
    db.execute('INSERT INTO evidence VALUES(?,?,?,?,?)',(doc['id'],kind,digest,raw,seq));return doc,True

def log(db,key,status,reason,eid):
    db.execute('INSERT INTO decision_log(object_id,status,reason,evidence_id) VALUES(?,?,?,?)',(key,status,reason,eid))

def effective_revision(db,plot_id):
    return db.execute("SELECT * FROM baseline_revision WHERE plot_id=? AND status IN ('original','adopted') ORDER BY revision DESC LIMIT 1",(plot_id,)).fetchone()

def report_payload(db,plot_id):
    coords=[dict(r) for r in db.execute('SELECT tier,coordinate FROM baseline_transect WHERE plot_id=? ORDER BY tier',(plot_id,))]
    surveys=[(r['id'],r['observed_at']) for r in db.execute('SELECT id,observed_at FROM survey WHERE plot_id=? ORDER BY id',(plot_id,))]
    disputes=[dict(r) for r in db.execute("SELECT tier,status FROM dispute_state WHERE plot_id=? ORDER BY tier",(plot_id,))]
    decisions=[{'tier':r['tier'],'version':r['version'],'status':r['status'],'decision':r['decision'],'coordinate':r['coordinate'],'missing':json.loads(r['missing'])}
               for r in db.execute('SELECT * FROM dispute_decision WHERE plot_id=? ORDER BY tier,version',(plot_id,))]
    return json.dumps({'baseline':coords,'surveys':surveys,'disputes':disputes,'decisions':decisions},
                      sort_keys=True,separators=(',',':'))

def evidence_version(db,plot_id,source_id):
    rev=effective_revision(db,plot_id)
    seq=db.execute('SELECT seq FROM evidence WHERE id=?',(source_id,)).fetchone()
    if rev is None:
        base='baseline:none#r0'
    else:
        base='baseline:%s#r%s(%s)' % (rev['id'],rev['revision'],rev['status'])
    open_disputes=list(db.execute("SELECT tier,dispute_id FROM dispute_state WHERE plot_id=? AND status='disputed' ORDER BY tier",(plot_id,)))
    if open_disputes:
        pending=db.execute('SELECT * FROM baseline_revision WHERE plot_id=? AND status=? ORDER BY revision DESC LIMIT 1',
                           (plot_id,'disputed')).fetchone()
        if pending:base+=';dispute:%s#r%s(disputed)' % (pending['id'],pending['revision'])
        base+=';disputed_tiers:'+','.join(x['tier'] for x in open_disputes)
    return '%s;evidence:%s@%s' % (base,source_id,seq['seq'] if seq else 0)

def update_acceptance(db,plot_id,status,score,reason,source_id,create=True):
    current=db.execute('SELECT * FROM acceptance WHERE plot_id=?',(plot_id,)).fetchone()
    if current is None and not create:return None
    version=(current['version']+1) if current else 1
    db.execute('INSERT INTO acceptance VALUES(?,?,?,?,?) ON CONFLICT(plot_id) DO UPDATE SET status=excluded.status,weighted_survival=excluded.weighted_survival,reason=excluded.reason,version=excluded.version',
               (plot_id,status,score,reason,version))
    db.execute('INSERT INTO acceptance_history VALUES(?,?,?,?,?,?)',(plot_id,version,status,score,reason,source_id))
    db.execute('INSERT INTO acceptance_report(plot_id,version,status,score,reason,evidence_version,payload,source_id) VALUES(?,?,?,?,?,?,?,?)',
               (plot_id,version,status,score,reason,evidence_version(db,plot_id,source_id),report_payload(db,plot_id),source_id))
    return version

def snapshot_acceptance(db,plot_id,fallback_source):
    current=db.execute('SELECT * FROM acceptance WHERE plot_id=?',(plot_id,)).fetchone()
    if not current:return None
    existing=db.execute('SELECT * FROM acceptance_snapshot WHERE plot_id=?',(plot_id,)).fetchone()
    # Preserve the last real ecological decision, not a previous dispute rework.
    if existing and current['reason'].startswith('baseline_'):return dict(current)
    hist=db.execute('SELECT source_id FROM acceptance_history WHERE plot_id=? ORDER BY version DESC LIMIT 1',(plot_id,)).fetchone()
    db.execute('INSERT INTO acceptance_snapshot VALUES(?,?,?,?,?,?) ON CONFLICT(plot_id) DO UPDATE SET version=excluded.version,status=excluded.status,score=excluded.score,reason=excluded.reason,source_id=excluded.source_id',
               (plot_id,current['version'],current['status'],current['weighted_survival'],current['reason'],hist['source_id'] if hist else fallback_source))
    return dict(current)

def report(path):
    with closing(connect(path)) as db:
        return {k:[dict(r) for r in db.execute(sql)] for k,sql in {
          'plots':'SELECT * FROM plot ORDER BY id','baseline':'SELECT * FROM baseline_transect ORDER BY id',
          'baseline_revisions':'SELECT * FROM baseline_revision ORDER BY plot_id,revision',
          'baseline_revision_transects':'SELECT * FROM baseline_revision_transect ORDER BY revision_id,tier',
          'baseline_disputes':'SELECT * FROM baseline_dispute ORDER BY id',
          'dispute_states':'SELECT * FROM dispute_state ORDER BY plot_id,tier',
          'dispute_decisions':'SELECT * FROM dispute_decision ORDER BY plot_id,tier,version',
          'rechecks':'SELECT * FROM recheck ORDER BY id',
          'spatial_checks':'SELECT * FROM spatial_check ORDER BY id',
          'materials':'SELECT * FROM material_batch ORDER BY id',
          'contracts':'SELECT * FROM work_contract ORDER BY id','works':'SELECT * FROM work_event ORDER BY id',
          'work_cells':'SELECT * FROM work_cell ORDER BY id',
          'surveys':'SELECT * FROM survey ORDER BY id','acceptance':'SELECT * FROM acceptance ORDER BY plot_id',
          'acceptance_snapshots':'SELECT * FROM acceptance_snapshot ORDER BY plot_id',
          'acceptance_history':'SELECT * FROM acceptance_history ORDER BY plot_id,version',
          'acceptance_reports':'SELECT * FROM acceptance_report ORDER BY plot_id,version,id',
          'current_reports':'SELECT ar.* FROM acceptance_report ar WHERE ar.id=(SELECT MAX(x.id) FROM acceptance_report x WHERE x.plot_id=ar.plot_id) ORDER BY ar.plot_id',
          'decisions':'SELECT * FROM decision_log ORDER BY id'}.items()}
