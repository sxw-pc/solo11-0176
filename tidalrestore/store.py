import json,hashlib,sqlite3
from pathlib import Path
from contextlib import closing

SCHEMA='''
PRAGMA foreign_keys=ON;
CREATE TABLE IF NOT EXISTS evidence(id TEXT PRIMARY KEY,kind TEXT NOT NULL,sha TEXT NOT NULL,raw BLOB NOT NULL,seq INTEGER NOT NULL UNIQUE);
CREATE TABLE IF NOT EXISTS plot(id TEXT PRIMARY KEY,area INTEGER NOT NULL,season TEXT NOT NULL,baseline_status TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS baseline_transect(id TEXT PRIMARY KEY,plot_id TEXT NOT NULL REFERENCES plot(id),tier TEXT NOT NULL,coordinate TEXT NOT NULL,cover INTEGER NOT NULL,source_id TEXT NOT NULL REFERENCES evidence(id));
CREATE TABLE IF NOT EXISTS baseline_dispute(id TEXT PRIMARY KEY,plot_id TEXT NOT NULL,coordinate TEXT NOT NULL,reason TEXT NOT NULL,source_id TEXT NOT NULL REFERENCES evidence(id));
CREATE TABLE IF NOT EXISTS material_batch(id TEXT PRIMARY KEY,species TEXT NOT NULL,origin TEXT NOT NULL,quantity INTEGER NOT NULL,valid_until TEXT NOT NULL,status TEXT NOT NULL,source_id TEXT NOT NULL REFERENCES evidence(id));
CREATE TABLE IF NOT EXISTS work_contract(id TEXT PRIMARY KEY,plot_id TEXT NOT NULL REFERENCES plot(id),material_species TEXT NOT NULL,planned_area INTEGER NOT NULL,window_start TEXT NOT NULL,window_end TEXT NOT NULL,repair_start TEXT NOT NULL,repair_end TEXT NOT NULL,contractor TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS work_event(id TEXT PRIMARY KEY,contract_id TEXT NOT NULL REFERENCES work_contract(id),event_type TEXT NOT NULL,area INTEGER NOT NULL,at TEXT NOT NULL,source_id TEXT NOT NULL REFERENCES evidence(id));
CREATE TABLE IF NOT EXISTS work_cell(id TEXT PRIMARY KEY,contract_id TEXT NOT NULL REFERENCES work_contract(id),tier TEXT NOT NULL,area INTEGER NOT NULL,material_id TEXT REFERENCES material_batch(id),status TEXT NOT NULL,source_id TEXT NOT NULL REFERENCES evidence(id));
CREATE TABLE IF NOT EXISTS survey(id TEXT PRIMARY KEY,plot_id TEXT NOT NULL REFERENCES plot(id),season TEXT NOT NULL,observer TEXT NOT NULL,high_alive INTEGER NOT NULL,mid_alive INTEGER NOT NULL,low_alive INTEGER NOT NULL,observed_at TEXT NOT NULL,source_id TEXT NOT NULL REFERENCES evidence(id));
CREATE TABLE IF NOT EXISTS acceptance(plot_id TEXT PRIMARY KEY,status TEXT NOT NULL,weighted_survival INTEGER NOT NULL,reason TEXT NOT NULL,version INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS acceptance_history(plot_id TEXT NOT NULL,version INTEGER NOT NULL,status TEXT NOT NULL,score INTEGER NOT NULL,reason TEXT NOT NULL,source_id TEXT NOT NULL REFERENCES evidence(id),PRIMARY KEY(plot_id,version));
CREATE TABLE IF NOT EXISTS decision_log(id INTEGER PRIMARY KEY,object_id TEXT NOT NULL,status TEXT NOT NULL,reason TEXT NOT NULL,evidence_id TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS edna_sample(id TEXT PRIMARY KEY,plot_id TEXT NOT NULL REFERENCES plot(id),site TEXT NOT NULL,collected_at TEXT NOT NULL,batch_id TEXT NOT NULL,barcode TEXT NOT NULL,read_count INTEGER NOT NULL,blank_reads INTEGER NOT NULL,source_id TEXT NOT NULL REFERENCES evidence(id));
'''

def connect(path):
    p=Path(path);p.parent.mkdir(parents=True,exist_ok=True)
    db=sqlite3.connect(p);db.row_factory=sqlite3.Row;db.executescript(SCHEMA);return db

def evidence(db,file,kind):
    raw=Path(file).read_bytes();doc=json.loads(raw)
    if doc.get('kind')!=kind or not doc.get('id'):raise ValueError('evidence_kind_or_id')
    digest=hashlib.sha256(raw).hexdigest();old=db.execute('SELECT sha FROM evidence WHERE id=?',(doc['id'],)).fetchone()
    if old:
        if old['sha']!=digest:raise ValueError('evidence_id_conflict')
        return doc,False
    seq=db.execute('SELECT COALESCE(MAX(seq),0)+1 FROM evidence').fetchone()[0]
    db.execute('INSERT INTO evidence VALUES(?,?,?,?,?)',(doc['id'],kind,digest,raw,seq));return doc,True

def log(db,key,status,reason,eid):
    db.execute('INSERT INTO decision_log(object_id,status,reason,evidence_id) VALUES(?,?,?,?)',(key,status,reason,eid))

def report(path):
    with closing(connect(path)) as db:
        return {k:[dict(r) for r in db.execute(sql)] for k,sql in {
          'plots':'SELECT * FROM plot ORDER BY id','baseline':'SELECT * FROM baseline_transect ORDER BY id',
          'baseline_disputes':'SELECT * FROM baseline_dispute ORDER BY id','materials':'SELECT * FROM material_batch ORDER BY id',
          'contracts':'SELECT * FROM work_contract ORDER BY id','works':'SELECT * FROM work_event ORDER BY id',
          'work_cells':'SELECT * FROM work_cell ORDER BY id',
          'surveys':'SELECT * FROM survey ORDER BY id','acceptance':'SELECT * FROM acceptance ORDER BY plot_id',
          'acceptance_history':'SELECT * FROM acceptance_history ORDER BY plot_id,version',
          'decisions':'SELECT * FROM decision_log ORDER BY id',
          'edna_samples':'SELECT * FROM edna_sample ORDER BY id'}.items()}
