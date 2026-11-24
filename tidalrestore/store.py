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
'''

# --- Salinity capture protocol -------------------------------------------------
# A staged packet (manifest or piece) is stored exactly as received; a public
# source and its samples are only written when the complete signed group has
# been verified.  Nothing is ever deleted or overwritten: revisions mark old
# material as superseded/revoked and close the publication interval instead.
SALINITY_SCHEMA='''
CREATE TABLE IF NOT EXISTS salinity_source(
  source_key TEXT PRIMARY KEY,          -- 'file:<id>' or 'group:<manifest>@<revision>'
  source_id TEXT NOT NULL,              -- logger id / manifest id
  kind TEXT NOT NULL,                   -- 'file' | 'chunked'
  status TEXT NOT NULL,                 -- 'committed' | 'superseded' | 'closed'
  sha TEXT NOT NULL,                    -- sha256 of the raw artifact that authorised it
  raw BLOB NOT NULL,
  group_key TEXT NOT NULL,
  manifest_key TEXT,
  revision INTEGER NOT NULL DEFAULT 1,
  device_id TEXT,
  seq_start INTEGER,
  seq_end INTEGER,
  committed_at TEXT NOT NULL,
  created_at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS idx_salinity_source_id ON salinity_source(source_id,kind);
CREATE INDEX IF NOT EXISTS idx_salinity_source_group ON salinity_source(group_key);
CREATE TABLE IF NOT EXISTS salinity_sample(
  source_key TEXT NOT NULL REFERENCES salinity_source(source_key),
  source_id TEXT NOT NULL,
  seq INTEGER,
  station TEXT NOT NULL,
  at TEXT NOT NULL,
  conductivity INTEGER NOT NULL,
  tide TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS idx_salinity_sample_source ON salinity_sample(source_key);
CREATE INDEX IF NOT EXISTS idx_salinity_sample_station ON salinity_sample(station);
CREATE TABLE IF NOT EXISTS salinity_manifest(
  manifest_key TEXT PRIMARY KEY,        -- '<manifest_id>@<revision>'
  manifest_id TEXT NOT NULL,
  revision INTEGER NOT NULL,
  device_id TEXT,
  sha TEXT NOT NULL,                    -- sha256 of raw manifest bytes
  raw BLOB NOT NULL,
  seq_start INTEGER,
  seq_end INTEGER,
  total_sha TEXT,
  calibration_piece INTEGER,
  calibration_from TEXT,
  calibration_to TEXT,
  supersedes_key TEXT,
  supersedes_sha TEXT,
  signature_ok INTEGER NOT NULL DEFAULT 0,
  revocation_ok INTEGER NOT NULL DEFAULT 0,
  status TEXT NOT NULL,                 -- 'staging' | 'committed' | 'revoked'
  reason TEXT,
  received_at TEXT NOT NULL,
  UNIQUE(manifest_id,revision));
CREATE INDEX IF NOT EXISTS idx_salinity_manifest_id ON salinity_manifest(manifest_id,revision);
CREATE TABLE IF NOT EXISTS salinity_piece(
  manifest_key TEXT NOT NULL,
  manifest_id TEXT NOT NULL,
  revision INTEGER NOT NULL,
  piece_index INTEGER NOT NULL,
  sha TEXT NOT NULL,
  raw BLOB NOT NULL,
  device_id TEXT,
  signature_ok INTEGER NOT NULL DEFAULT 0,
  status TEXT NOT NULL DEFAULT 'received',  -- 'received' | 'conflict'
  received_at TEXT NOT NULL,
  PRIMARY KEY(manifest_key,piece_index,sha));
CREATE INDEX IF NOT EXISTS idx_salinity_piece_key ON salinity_piece(manifest_key,piece_index);
CREATE TABLE IF NOT EXISTS salinity_conflict(
  id INTEGER PRIMARY KEY,
  scope TEXT NOT NULL,                  -- 'manifest' | 'piece'
  object_key TEXT NOT NULL,
  piece_index INTEGER,
  existing_sha TEXT,
  received_sha TEXT,
  raw BLOB,
  reason TEXT NOT NULL,
  detected_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS salinity_publication(
  id INTEGER PRIMARY KEY,
  group_key TEXT NOT NULL,
  source_key TEXT NOT NULL,
  manifest_key TEXT,
  revision INTEGER,
  effective_from TEXT NOT NULL,
  effective_to TEXT,
  status TEXT NOT NULL);                -- 'active' | 'closed'
CREATE INDEX IF NOT EXISTS idx_salinity_publication_group ON salinity_publication(group_key,effective_from);
CREATE INDEX IF NOT EXISTS idx_salinity_publication_source ON salinity_publication(source_key);
CREATE TABLE IF NOT EXISTS salinity_event(
  id INTEGER PRIMARY KEY,
  group_key TEXT,
  manifest_key TEXT,
  event TEXT NOT NULL,
  detail TEXT,
  collector TEXT,
  at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS salinity_receipt(
  id INTEGER PRIMARY KEY,
  packet_kind TEXT NOT NULL,
  packet_sha TEXT NOT NULL,
  manifest_key TEXT,
  piece_index INTEGER,
  collector TEXT,
  received_at TEXT NOT NULL,
  raw BLOB NOT NULL);
CREATE TABLE IF NOT EXISTS salinity_restore(
  id INTEGER PRIMARY KEY,
  group_key TEXT NOT NULL,
  fault_point TEXT,
  outcome TEXT NOT NULL,
  at TEXT NOT NULL);
'''

def connect(path):
    p=Path(path);p.parent.mkdir(parents=True,exist_ok=True)
    db=sqlite3.connect(p,timeout=30.0)
    db.row_factory=sqlite3.Row
    db.execute('PRAGMA journal_mode=WAL')
    db.executescript(SCHEMA)
    if _has_legacy_salinity(db):
        _migrate_legacy_salinity(db)
    db.executescript(SALINITY_SCHEMA)
    return db

def _columns(db,table):
    return {r['name'] for r in db.execute(f'PRAGMA table_info({table})')}

def _has_legacy_salinity(db):
    existing={r['name'] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    return 'salinity_source' in existing and 'source_key' not in _columns(db,'salinity_source')


def _migrate_legacy_salinity(db):
    """Bring the original two-column salinity tables up to the protocol shape.

    The baseline release stored ``salinity_source(id, sha, raw)`` and an
    unkeyed ``salinity_sample``.  Rows written by that release are real public
    samples, so they are preserved as committed legacy sources (never
    rewritten or deleted) and gaps in the protocol tables are filled in place.
    """
    existing={r['name'] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    db.execute('ALTER TABLE salinity_source RENAME TO salinity_source_legacy')
    legacy_sample=False
    if 'salinity_sample' in existing and 'source_key' not in _columns(db,'salinity_sample'):
        db.execute('ALTER TABLE salinity_sample RENAME TO salinity_sample_legacy')
        legacy_sample=True
    db.executescript(SALINITY_SCHEMA)
    legacy=[dict(r) for r in db.execute('SELECT * FROM salinity_source_legacy')]
    for row in legacy:
        raw=row.get('raw') or b''
        digest=row.get('sha') or hashlib.sha256(raw).hexdigest()
        key='file:'+row['id']
        db.execute('INSERT OR IGNORE INTO salinity_source(source_key,source_id,kind,status,sha,raw,group_key,manifest_key,revision,device_id,seq_start,seq_end,committed_at,created_at) '
                   "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                   (key,row['id'],'file','committed',digest,raw,key,None,1,None,None,None,
                    '1970-01-01T00:00:00Z','1970-01-01T00:00:00Z'))
        db.execute('INSERT OR IGNORE INTO salinity_publication(group_key,source_key,manifest_key,revision,'
                   "effective_from,effective_to,status) VALUES(?,?,?,?,?,?,?)",
                   (key,key,None,1,'1970-01-01T00:00:00Z',None,'active'))
    if legacy_sample:
        db.execute('INSERT INTO salinity_sample(source_key,source_id,seq,station,at,conductivity,tide) '
                   "SELECT 'file:'||source_id,source_id,NULL,station,at,conductivity,tide FROM salinity_sample_legacy")
        db.execute('DROP TABLE salinity_sample_legacy')
    db.execute('DROP TABLE salinity_source_legacy')

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
          'decisions':'SELECT * FROM decision_log ORDER BY id'}.items()}
