import json,hashlib,sqlite3
from pathlib import Path
from contextlib import closing

SCHEMA='''
PRAGMA foreign_keys=ON;
CREATE TABLE IF NOT EXISTS evidence(id TEXT PRIMARY KEY,kind TEXT NOT NULL,sha TEXT NOT NULL,raw BLOB,seq INTEGER NOT NULL UNIQUE);
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
'''

# Offline legal-preservation and cold-archive ledger. These tables never change
# the eleven business tables above; they only govern where evidence.raw lives.
ARCHIVE_SCHEMA='''
CREATE TABLE IF NOT EXISTS archive_permit(
  permit_id TEXT PRIMARY KEY,
  route TEXT NOT NULL,
  plot_id TEXT,
  scope TEXT NOT NULL,
  authorized_by TEXT NOT NULL,
  custodian TEXT NOT NULL,
  issued_at TEXT NOT NULL,
  expires_at TEXT,
  status TEXT NOT NULL,
  document_sha TEXT NOT NULL,
  revoked_at TEXT,
  revoke_reason TEXT);
CREATE TABLE IF NOT EXISTS legal_hold(
  hold_id TEXT PRIMARY KEY,
  plot_id TEXT,
  source_ids TEXT NOT NULL,
  order_ref TEXT NOT NULL,
  issued_by TEXT NOT NULL,
  issued_at TEXT NOT NULL,
  lifted_at TEXT,
  document_sha TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS archive_batch(
  batch_id TEXT PRIMARY KEY,
  permit_id TEXT NOT NULL REFERENCES archive_permit(permit_id),
  package_id TEXT NOT NULL,
  root TEXT NOT NULL,
  state TEXT NOT NULL,
  package_digest TEXT,
  created_at TEXT NOT NULL,
  sealed_at TEXT,
  committed_at TEXT,
  failure_reason TEXT,
  member_count INTEGER NOT NULL DEFAULT 0,
  bytes_moved INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS archived_evidence(
  evidence_id TEXT PRIMARY KEY REFERENCES evidence(id),
  batch_id TEXT NOT NULL REFERENCES archive_batch(batch_id),
  package_id TEXT NOT NULL,
  member TEXT NOT NULL,
  sha TEXT NOT NULL,
  raw_size INTEGER NOT NULL,
  archived_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS evidence_usage(
  evidence_id TEXT NOT NULL REFERENCES evidence(id),
  plot_id TEXT,
  object_type TEXT NOT NULL,
  object_id TEXT NOT NULL,
  PRIMARY KEY(evidence_id,plot_id,object_type,object_id));
CREATE TABLE IF NOT EXISTS archive_denial(
  id INTEGER PRIMARY KEY,
  batch_id TEXT,
  evidence_id TEXT NOT NULL,
  reason TEXT NOT NULL,
  legal_ref TEXT NOT NULL,
  at TEXT NOT NULL);
'''

def _migrate(db):
    # The original schema declared evidence.raw NOT NULL. Cold archive moves
    # those bytes into a read-only content-addressed package, so the hot copy
    # becomes optional; the immutable sha/seq identity always stays.
    # Runs before any write transaction so the foreign_keys pragma is live and
    # ALTER TABLE RENAME does not rewrite references held by business tables.
    db.execute('PRAGMA foreign_keys=OFF')
    cols=db.execute("PRAGMA table_info(evidence)").fetchall()
    if cols and all(c['notnull'] for c in cols if c['name']=='raw'):
        db.execute('ALTER TABLE evidence RENAME TO evidence_legacy')
        db.execute('CREATE TABLE evidence(id TEXT PRIMARY KEY,kind TEXT NOT NULL,sha TEXT NOT NULL,raw BLOB,seq INTEGER NOT NULL UNIQUE)')
        db.execute('INSERT INTO evidence(id,kind,sha,raw,seq) SELECT id,kind,sha,raw,seq FROM evidence_legacy')
        db.execute('DROP TABLE evidence_legacy')
        db.commit()
    # Older pre-release builds created evidence_usage without plot_id in the
    # primary key, which collapsed one source shared across plots into a row.
    usage_cols={r['name'] for r in db.execute("PRAGMA table_info(evidence_usage)").fetchall()}
    if usage_cols:
        pk=[r['name'] for r in db.execute("PRAGMA table_info(evidence_usage)").fetchall() if r['pk']]
        if 'plot_id' not in pk:
            db.execute('DROP TABLE evidence_usage')
            db.commit()
    db.execute('PRAGMA foreign_keys=ON')

def connect(path):
    p=Path(path);p.parent.mkdir(parents=True,exist_ok=True)
    db=sqlite3.connect(p);db.row_factory=sqlite3.Row
    _migrate(db)
    db.execute('PRAGMA foreign_keys=ON')
    db.executescript(SCHEMA)
    db.executescript(ARCHIVE_SCHEMA)
    return db

def evidence(db,file,kind):
    raw=Path(file).read_bytes();doc=json.loads(raw)
    if doc.get('kind')!=kind or not doc.get('id'):raise ValueError('evidence_kind_or_id')
    digest=hashlib.sha256(raw).hexdigest();old=db.execute('SELECT sha,raw FROM evidence WHERE id=?',(doc['id'],)).fetchone()
    if old:
        if old['sha']!=digest:raise ValueError('evidence_id_conflict')
        # Re-import of an archived source restores the hot bytes (idempotent
        # reverse recovery) instead of silently accepting a missing raw.
        if old['raw'] is None:db.execute('UPDATE evidence SET raw=? WHERE id=?',(raw,doc['id']))
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
          'decisions':'SELECT * FROM decision_log ORDER BY id'}.items()}
