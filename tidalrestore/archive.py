"""Offline evidence cold archive and legal preservation.

Two mutually exclusive preservation routes, both authorized offline:

* Route A - a destruction permit (owner + independent archivist) allows the
  settled-season original JSON bytes to move from the hot SQLite store into a
  read-only content-addressed tar package. The database keeps an immutable
  index (sha/seq identity plus a cold pointer); audit stays ``matched`` while
  the cold package is online and originals can be restored by seq.
* Route B - a litigation hold keeps the covered originals hot and instantly
  readable. Archiving a held source is refused and a denial carrying the legal
  reference is recorded.

The package file, the database cold pointer and the on-disk restore index are
three separate places that must converge after a power loss. The staging phase
is destructive-free and retries/rolls back; the hot-byte deletion only happens
inside one SQLite transaction after the package and index are sealed, so the
report can never show "archived" while audit cannot read the bytes back.

Nothing here opens a network connection.
"""
import io, os, json, hashlib, tarfile, threading
from datetime import datetime, timezone
from contextlib import closing
from .store import connect

ARCHIVE_EPOCH = 0
EXPORT_LOCK = threading.Lock()

class ArchiveError(Exception):
    """Base class for archive/legal-preservation failures."""
class LegalHoldError(ArchiveError):
    """Deleting the held original was refused."""
class ArchiveVerifyError(ArchiveError):
    """A package digest or member SHA did not verify."""
class ArchiveConflict(ArchiveError):
    """Legal state changed while an archive commit was in flight."""

def _now():
    return datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')

def _sha256(data):
    return hashlib.sha256(data).hexdigest()

# ---------------------------------------------------------------------------
# Offline legal documents
# ---------------------------------------------------------------------------
def record_permit(path, file):
    """Ingest an offline destruction/archival permit document."""
    raw=__read(file); doc=json.loads(raw)
    if doc.get('doc_type')!='destruction_permit':raise ArchiveError('permit_document_type')
    for key in ('permit_id','route','scope','authorized_by','custodian','issued_at'):
        if not doc.get(key):raise ArchiveError('permit_missing_'+key)
    if doc['route'] not in ('A','B'):raise ArchiveError('permit_route_unknown')
    authors=doc['authorized_by']
    if not isinstance(authors,list) or len(authors)<2:raise ArchiveError('permit_needs_owner_and_archivist')
    with closing(connect(path)) as db,db:
        if doc.get('plot_id') and not db.execute('SELECT 1 FROM plot WHERE id=?',(doc['plot_id'],)).fetchone():
            raise ArchiveError('permit_unknown_plot')
        db.execute('''INSERT INTO archive_permit(permit_id,route,plot_id,scope,authorized_by,custodian,
                      issued_at,expires_at,status,document_sha,revoked_at,revoke_reason)
                      VALUES(?,?,?,?,?,?,?,?, 'active', ?,NULL,NULL)
                      ON CONFLICT(permit_id) DO UPDATE SET route=excluded.route,plot_id=excluded.plot_id,
                      scope=excluded.scope,authorized_by=excluded.authorized_by,custodian=excluded.custodian,
                      issued_at=excluded.issued_at,expires_at=excluded.expires_at,document_sha=excluded.document_sha''',
                   (doc['permit_id'],doc['route'],doc.get('plot_id'),doc['scope'],';'.join(authors),
                    doc['custodian'],doc['issued_at'],doc.get('expires_at'),_sha256(raw)))
    return {'permit_id':doc['permit_id'],'route':doc['route'],'status':'recorded','document_sha':_sha256(raw)}

def record_hold(path, file):
    """Ingest an offline litigation preservation order."""
    raw=__read(file); doc=json.loads(raw)
    if doc.get('doc_type')!='legal_hold':raise ArchiveError('hold_document_type')
    for key in ('hold_id','order_ref','issued_by','issued_at'):
        if not doc.get(key):raise ArchiveError('hold_missing_'+key)
    source_ids=doc.get('source_ids') or []
    if not doc.get('plot_id') and not source_ids:raise ArchiveError('hold_needs_plot_or_sources')
    with closing(connect(path)) as db,db:
        for sid in source_ids:
            if not db.execute('SELECT 1 FROM evidence WHERE id=?',(sid,)).fetchone():
                raise ArchiveError('hold_unknown_source:'+sid)
        if doc.get('plot_id') and not db.execute('SELECT 1 FROM plot WHERE id=?',(doc['plot_id'],)).fetchone():
            raise ArchiveError('hold_unknown_plot')
        db.execute('''INSERT INTO legal_hold(hold_id,plot_id,source_ids,order_ref,issued_by,issued_at,lifted_at,document_sha)
                      VALUES(?,?,?,?,?,?,NULL,?)
                      ON CONFLICT(hold_id) DO UPDATE SET plot_id=excluded.plot_id,source_ids=excluded.source_ids,
                      order_ref=excluded.order_ref,issued_by=excluded.issued_by,issued_at=excluded.issued_at,
                      document_sha=excluded.document_sha,lifted_at=NULL''',
                   (doc['hold_id'],doc.get('plot_id'),json.dumps(source_ids),doc['order_ref'],
                    doc['issued_by'],doc['issued_at'],_sha256(raw)))
    return {'hold_id':doc['hold_id'],'order_ref':doc['order_ref'],'source_ids':source_ids,
            'plot_id':doc.get('plot_id'),'status':'recorded','document_sha':_sha256(raw)}

def revoke_permit(path, permit_id, reason, at=None):
    at=at or _now()
    with closing(connect(path)) as db,db:
        row=db.execute('SELECT 1 FROM archive_permit WHERE permit_id=?',(permit_id,)).fetchone()
        if not row:raise ArchiveError('unknown_permit')
        db.execute('UPDATE archive_permit SET status=?,revoked_at=?,revoke_reason=? WHERE permit_id=?',
                   ('revoked',at,reason,permit_id))
    return {'permit_id':permit_id,'status':'revoked','at':at}

def lift_hold(path, hold_id, at=None):
    at=at or _now()
    with closing(connect(path)) as db,db:
        row=db.execute('SELECT 1 FROM legal_hold WHERE hold_id=?',(hold_id,)).fetchone()
        if not row:raise ArchiveError('unknown_hold')
        db.execute('UPDATE legal_hold SET lifted_at=? WHERE hold_id=?',(at,hold_id))
    return {'hold_id':hold_id,'status':'lifted','at':at}

def __read(file):
    from pathlib import Path
    return Path(file).read_bytes()

# ---------------------------------------------------------------------------
# Source -> plot/contract/unit dependency index
# ---------------------------------------------------------------------------
def rebuild_usage(db):
    """Refresh evidence_usage: every business use of every source id.

    Uses individual execute() calls rather than executescript(), because the
    latter force-commits any open transaction and would break the atomic
    archive commit when the eligibility recheck runs inside BEGIN IMMEDIATE.
    """
    statements=[
      'INSERT OR IGNORE INTO evidence_usage SELECT source_id,plot_id,\'baseline_transect\',id FROM baseline_transect',
      'INSERT OR IGNORE INTO evidence_usage SELECT source_id,plot_id,\'baseline_dispute\',id FROM baseline_dispute',
      'INSERT OR IGNORE INTO evidence_usage SELECT source_id,plot_id,\'survey\',id FROM survey',
      'INSERT OR IGNORE INTO evidence_usage SELECT source_id,plot_id,\'acceptance_history\',plot_id||\':\'||version FROM acceptance_history',
      '''INSERT OR IGNORE INTO evidence_usage
         SELECT we.source_id,wc.plot_id,'work_event',we.id FROM work_event we
         JOIN work_contract wc ON wc.id=we.contract_id''',
      '''INSERT OR IGNORE INTO evidence_usage
         SELECT wce.source_id,wc.plot_id,'work_cell',wce.id FROM work_cell wce
         JOIN work_contract wc ON wc.id=wce.contract_id''',
      '''INSERT OR IGNORE INTO evidence_usage
         SELECT mb.source_id,wc.plot_id,'material_batch',mb.id
         FROM material_batch mb
         JOIN work_cell wce ON wce.material_id=mb.id
         JOIN work_contract wc ON wc.id=wce.contract_id''',
      '''INSERT OR IGNORE INTO evidence_usage
         SELECT dl.evidence_id,COALESCE(p.id,wc.plot_id,wev.plot_id,wcm.plot_id),'decision_log',dl.object_id
         FROM decision_log dl
         LEFT JOIN plot p ON p.id=dl.object_id
         LEFT JOIN work_contract wc ON wc.id=dl.object_id
         LEFT JOIN (SELECT we.id AS eid,wc2.plot_id FROM work_event we
                    JOIN work_contract wc2 ON wc2.id=we.contract_id) wev ON wev.eid=dl.object_id
         LEFT JOIN (SELECT DISTINCT mb.id AS bid,wc3.plot_id FROM material_batch mb
                    JOIN work_cell wce ON wce.material_id=mb.id
                    JOIN work_contract wc3 ON wc3.id=wce.contract_id) wcm ON wcm.bid=dl.object_id''']
    db.execute('DELETE FROM evidence_usage')
    for sql in statements:db.execute(sql)

def _plots_for(db, evidence_id):
    return sorted({r[0] for r in db.execute(
        'SELECT DISTINCT plot_id FROM evidence_usage WHERE evidence_id=? AND plot_id IS NOT NULL',
        (evidence_id,)).fetchall()})

def _active_holds(db):
    out=[]
    for r in db.execute('SELECT * FROM legal_hold WHERE lifted_at IS NULL'):
        d=dict(r);d['source_id_set']=set(json.loads(d['source_ids']));out.append(d)
    return out

def _hold_ref(hold):
    return hold['order_ref']+'/'+hold['hold_id']

# ---------------------------------------------------------------------------
# Eligibility planning
# ---------------------------------------------------------------------------
def plan_archive(path, permit_id, archive_root, now=None):
    now=now or _now()
    with closing(connect(path)) as db:
        db.execute('PRAGMA busy_timeout=5000')
        return _plan_archive(db,permit_id,archive_root,now)

def _plan_archive(db, permit_id, archive_root, now):
    rebuild_usage(db)
    permit=db.execute('SELECT * FROM archive_permit WHERE permit_id=?',(permit_id,)).fetchone()
    if not permit:raise ArchiveError('unknown_permit')
    permit=dict(permit)
    holds=_active_holds(db)
    settled={r[0] for r in db.execute("SELECT plot_id FROM acceptance WHERE status='accepted'")}
    # A source shared across plots may move only once every usage plot is
    # covered by some active, unexpired destruction permit. The invoking permit
    # must itself be valid; sibling permits widen the covered-plot set.
    covered_plots=set()
    active_permits=db.execute("SELECT * FROM archive_permit WHERE status='active' AND (expires_at IS NULL OR expires_at>=?)",(now,)).fetchall()
    for ap in active_permits:
        if ap['plot_id']:covered_plots.add(ap['plot_id'])
    invoking_valid=permit['status']=='active' and (not permit['expires_at'] or permit['expires_at']>=now)
    eligible,blocked,review=[],[],[]
    hot_bytes=0
    for ev in db.execute('SELECT * FROM evidence WHERE raw IS NOT NULL ORDER BY seq'):
        ev=dict(ev);hot_bytes+=len(ev['raw'])
        plots=_plots_for(db,ev['id'])
        item={'evidence_id':ev['id'],'seq':ev['seq'],'kind':ev['kind'],'sha':ev['sha'],
              'raw_size':len(ev['raw']),'plots':plots}
        block_reasons=[];review_reasons=[]
        if not plots:
            review_reasons.append(('usage_not_indexed',''))
        for hold in holds:
            direct=ev['id'] in hold['source_id_set']
            plot_hit=bool(hold['plot_id']) and hold['plot_id'] in plots
            if direct or plot_hit:
                ref=_hold_ref(hold)
                if plot_hit and not direct and len(plots)>1:
                    review_reasons.append(('hold_scope_review_shared_source',ref))
                else:
                    block_reasons.append(('legal_hold_active',ref))
        if permit['status']=='revoked':
            block_reasons.append(('permit_revoked',permit_id))
        elif permit['expires_at'] and permit['expires_at']<now:
            block_reasons.append(('permit_expired',permit_id))
        uncovered=[p for p in plots if p not in covered_plots]
        if uncovered:
            review_reasons.append(('plot_without_active_permit',';'.join(uncovered)))
        if any(p not in settled for p in plots):
            block_reasons.append(('plot_not_settled',';'.join(p for p in plots if p not in settled)))
        if review_reasons:
            item['reason'],item['legal_ref']=review_reasons[0]
            review.append(item)
        elif block_reasons:
            item['reason'],item['legal_ref']=block_reasons[0]
            blocked.append(item)
        else:
            eligible.append(item)
    return {'permit_id':permit_id,'archive_root':str(archive_root),'at':now,
            'eligible':eligible,'blocked':blocked,'review':review,'hot_bytes':hot_bytes,
            'invoking_permit_valid':invoking_valid,'covered_plots':sorted(covered_plots)}

# ---------------------------------------------------------------------------
# Package layout, restore index and verified reads
# ---------------------------------------------------------------------------
def _root_paths(archive_root):
    from pathlib import Path
    root=Path(archive_root);pkg_dir=root/'packages';idx=root/'index.json'
    pkg_dir.mkdir(parents=True,exist_ok=True)
    return root,pkg_dir,idx

def _load_index(idx):
    if idx.exists():return json.loads(idx.read_text(encoding='utf8'))
    return {'packages':[]}

def _write_index(idx, data):
    tmp=idx.with_suffix('.json.tmp')
    tmp.write_text(json.dumps(data,ensure_ascii=False,indent=2,sort_keys=True),encoding='utf8')
    os.replace(tmp,idx);os.chmod(idx,0o444)

def _make_tar(manifest, blobs):
    buf=io.BytesIO()
    with tarfile.open(fileobj=buf,mode='w',format=tarfile.USTAR_FORMAT) as tar:
        def add(name,data,mode):
            ti=tarfile.TarInfo(name);ti.size=len(data);ti.mtime=ARCHIVE_EPOCH
            ti.mode=mode;ti.uid=ti.gid=0;ti.uname=ti.gname=''
            tar.addfile(ti,io.BytesIO(data))
        add('manifest.json',json.dumps(manifest,ensure_ascii=False,indent=2,sort_keys=True).encode('utf8'),0o444)
        for sha,blob in sorted(blobs.items()):
            add('blobs/'+sha,blob,0o444)
    return buf.getvalue()

def open_verified_package(archive_root, package_id, expected_digest):
    """Open a cold package, proving its whole-package digest first."""
    from pathlib import Path
    path=Path(archive_root)/'packages'/(package_id+'.tar')
    if not path.exists():raise ArchiveVerifyError('package_missing:'+package_id)
    data=path.read_bytes();digest=_sha256(data)
    if expected_digest and digest!=expected_digest:
        raise ArchiveVerifyError('package_tampered:'+package_id)
    with tarfile.open(fileobj=io.BytesIO(data),mode='r:') as tar:
        members={m.name:m for m in tar.getmembers()}
        mf=tar.extractfile(members['manifest.json'])
        if mf is None:raise ArchiveVerifyError('manifest_missing:'+package_id)
        manifest=json.loads(mf.read())
    return path,digest,manifest,data

def _read_member(data, manifest, evidence_id, expected_sha):
    entry=next((m for m in manifest['members'] if m['evidence_id']==evidence_id),None)
    if entry is None:raise ArchiveVerifyError('member_not_manifested:'+evidence_id)
    with tarfile.open(fileobj=io.BytesIO(data),mode='r:') as tar:
        members={m.name:m for m in tar.getmembers()}
        f=tar.extractfile(members[entry['member']])
        if f is None:raise ArchiveVerifyError('member_missing:'+evidence_id)
        blob=f.read()
    if _sha256(blob)!=expected_sha:
        raise ArchiveVerifyError('member_sha_mismatch:'+evidence_id)
    return blob

def read_cold_raw(db, archive_root, evidence_id, cache=None):
    """Locate and verify one archived original. Used by audit and restore."""
    row=db.execute('''SELECT ae.package_id,ae.member,ae.sha,ab.package_digest,ab.root
                      FROM archived_evidence ae JOIN archive_batch ab ON ab.batch_id=ae.batch_id
                      WHERE ae.evidence_id=?''',(evidence_id,)).fetchone()
    if not row:raise ArchiveError('no_cold_pointer:'+evidence_id)
    root=str(archive_root) if archive_root else row['root']
    if cache is not None and row['package_id'] in cache:
        _,data,manifest=cache[row['package_id']]
    else:
        _,_,manifest,data=open_verified_package(root,row['package_id'],row['package_digest'])
        if cache is not None:cache[row['package_id']]=(root,data,manifest)
    return _read_member(data,manifest,evidence_id,row['sha'])

# ---------------------------------------------------------------------------
# Two-phase export
# ---------------------------------------------------------------------------
def export_archive(path, permit_id, archive_root, now=None, fault=None):
    """Move eligible originals into a sealed read-only package.

    Phase 1 (retriable / rollback-able): plan, write staging tar, verify its
    digest, atomically publish it, update the restore index, mark the batch
    sealed. The hot database is untouched.
    Phase 2 (atomic): re-check eligibility, insert cold pointers, null the hot
    bytes in one transaction. A crash before commit leaves the hot bytes; a
    crash before sealing leaves only a deletable staging file.
    """
    now=now or _now()
    with EXPORT_LOCK:
        plan=plan_archive(path,permit_id,archive_root,now=now)
        root,pkg_dir,idx=_root_paths(archive_root)
        if not plan['eligible']:
            # Legal denials are recorded even when nothing can be archived.
            _record_denials(path,None,plan['blocked'])
            return {'batch_id':None,'state':'nothing_eligible','archived':[],
                    'blocked':plan['blocked'],'review':plan['review'],'member_count':0,'bytes_moved':0}
        with closing(connect(path)) as db,db:
            db.execute('PRAGMA busy_timeout=5000')
            compact=_now_compact(now)
            n=1+db.execute("SELECT COUNT(*) FROM archive_batch WHERE batch_id LIKE ?",
                           ('batch-'+compact+'-%',)).fetchone()[0]
            batch_id='batch-'+compact+'-'+permit_id+('-'+str(n) if n>1 else '')
            # Legal denials are recorded before any bytes leave the hot store.
            _record_denials(db,batch_id,plan['blocked'])
            blobs={};members=[]
            for item in plan['eligible']:
                raw=db.execute('SELECT raw FROM evidence WHERE id=?',(item['evidence_id'],)).fetchone()[0]
                if _sha256(raw)!=item['sha']:raise ArchiveVerifyError('hot_sha_mismatch:'+item['evidence_id'])
                blobs[item['sha']]=raw
                members.append({'evidence_id':item['evidence_id'],'seq':item['seq'],'kind':item['kind'],
                                'sha':item['sha'],'member':'blobs/'+item['sha'],'raw_size':len(raw)})
            db.execute('INSERT INTO archive_batch(batch_id,permit_id,package_id,root,state,created_at,member_count) VALUES(?,?,?,?,?,?,?)',
                       (batch_id,permit_id,'pending',str(root),'staging',now,len(members)))
        # ---- Phase 1: package + index, no hot bytes touched ---------------
        staging=pkg_dir/(batch_id+'.tar.tmp')
        manifest={'batch_id':batch_id,'permit_id':permit_id,'created_at':now,'members':members}
        tar_bytes=_make_tar(manifest,blobs)
        staging.write_bytes(tar_bytes)
        if fault=='staging_corrupt':
            os.chmod(staging,0o644)
            with open(staging,'r+b') as f:f.seek(0);f.write(b'X')
        # Re-read the staging file and prove the digest before publishing.
        actual=_sha256(staging.read_bytes())
        if actual!=_sha256(tar_bytes):
            staging.unlink(missing_ok=True)
            _fail_batch(path,batch_id,'staging_verify_failed')
            raise ArchiveVerifyError('staging_verify_failed:'+batch_id)
        if fault=='after_staging_file':
            raise RuntimeError('fault:after_staging_file')
        package_id='pkg-'+actual[:16]
        final=pkg_dir/(package_id+'.tar')
        os.replace(staging,final);os.chmod(final,0o444)
        index=_load_index(idx)
        if not any(p['package_id']==package_id for p in index['packages']):
            index['packages'].append({'package_id':package_id,'file':'packages/'+package_id+'.tar',
                                      'digest':actual,'batch_id':batch_id,
                                      'members':[m['evidence_id'] for m in members]})
        _write_index(idx,index)
        if fault=='after_index_write':
            raise RuntimeError('fault:after_index_write')
        with closing(connect(path)) as db,db:
            db.execute('UPDATE archive_batch SET state=?,package_id=?,package_digest=?,sealed_at=? WHERE batch_id=?',
                       ('sealed',package_id,actual,now,batch_id))
        # ---- Phase 2: atomic pointer + hot-byte swap ----------------------
        return _commit_archive(path,batch_id,permit_id,package_id,actual,root,members,now,fault)

def _commit_archive(path,batch_id,permit_id,package_id,digest,root,members,now,fault):
    with closing(connect(path)) as db:
        db.execute('PRAGMA busy_timeout=5000')
        db.execute('BEGIN IMMEDIATE')
        try:
            # Concurrent permit withdrawal / hold arrival must abort the move.
            recheck=_plan_archive(db,permit_id,root,now)
            allowed={x['evidence_id'] for x in recheck['eligible']}
            changed=[m['evidence_id'] for m in members if m['evidence_id'] not in allowed]
            if changed:
                if db.in_transaction:db.execute('ROLLBACK')
                # Keep the sealed package and the sealed state: the legal
                # change is reversible, so recovery can retry this batch.
                with closing(connect(path)) as note,note:
                    note.execute('UPDATE archive_batch SET failure_reason=? WHERE batch_id=?',
                                 ('deferred_eligibility_changed:'+','.join(changed),batch_id))
                raise ArchiveConflict('eligibility_changed:'+','.join(changed))
            for m in members:
                db.execute('''INSERT INTO archived_evidence(evidence_id,batch_id,package_id,member,sha,raw_size,archived_at)
                              VALUES(?,?,?,?,?,?,?)''',
                           (m['evidence_id'],batch_id,package_id,m['member'],m['sha'],m['raw_size'],now))
            if fault=='before_hot_delete':
                if db.in_transaction:db.execute('ROLLBACK')
                raise RuntimeError('fault:before_hot_delete')
            bytes_moved=0
            for m in members:
                row=db.execute('SELECT raw FROM evidence WHERE id=?',(m['evidence_id'],)).fetchone()
                if row['raw'] is None:continue
                bytes_moved+=len(row['raw'])
                db.execute('UPDATE evidence SET raw=NULL WHERE id=?',(m['evidence_id'],))
            if fault=='after_hot_delete':
                if db.in_transaction:db.execute('ROLLBACK')
                raise RuntimeError('fault:after_hot_delete')
            db.execute('UPDATE archive_batch SET state=?,committed_at=?,bytes_moved=? WHERE batch_id=?',
                       ('committed',now,bytes_moved,batch_id))
            db.execute('COMMIT')
        except Exception:
            # Any unexpected error rolls the pointer/raw swap back; the sealed
            # package stays on disk and is recovered or reused later.
            if db.in_transaction:db.execute('ROLLBACK')
            raise
    return {'batch_id':batch_id,'package_id':package_id,'state':'committed',
            'archived':[m['evidence_id'] for m in members],'member_count':len(members),
            'bytes_moved':bytes_moved,'blocked':recheck['blocked'],'review':recheck['review']}

def _record_denials(db_or_path,batch_id,blocked):
    own=not hasattr(db_or_path,'execute')
    db=connect(db_or_path) if own else db_or_path
    try:
        for item in blocked:
            exists=db.execute('SELECT 1 FROM archive_denial WHERE evidence_id=? AND reason=? AND legal_ref=?',
                              (item['evidence_id'],item['reason'],item['legal_ref'])).fetchone()
            if not exists:
                db.execute('INSERT INTO archive_denial(batch_id,evidence_id,reason,legal_ref,at) VALUES(?,?,?,?,?)',
                           (batch_id,item['evidence_id'],item['reason'],item['legal_ref'],_now()))
        if own:db.commit()
    finally:
        if own:db.close()

def _fail_batch(path,batch_id,reason):
    with closing(connect(path)) as db,db:
        db.execute("UPDATE archive_batch SET state='failed',failure_reason=? WHERE batch_id=?",(reason,batch_id))

def _now_compact(now):
    return now.replace('-','').replace(':','').replace('T','').replace('Z','')

# ---------------------------------------------------------------------------
# Crash recovery
# ---------------------------------------------------------------------------
def recover(path, archive_root):
    """Converge package file, restore index and DB pointers after a crash.

    staging batches never touched hot bytes: their tmp file is removed and the
    batch rolls back. sealed batches already have a verified package and index
    entry: their commit is retried idempotently; a corrupt package is
    quarantined and the batch fails rather than pointing at half evidence.
    """
    root,pkg_dir,idx=_root_paths(archive_root)
    summary={'rolled_back':[],'recommitted':[],'failed':[],'orphan_tmp_removed':[]}
    for tmp in pkg_dir.glob('*.tar.tmp'):
        tmp.unlink();summary['orphan_tmp_removed'].append(tmp.name)
    with closing(connect(path)) as db:
        pending=db.execute("SELECT * FROM archive_batch WHERE state IN ('staging','sealed')").fetchall()
    for row in pending:
        b=dict(row)
        if b['state']=='staging':
            (pkg_dir/(b['batch_id']+'.tar.tmp')).unlink(missing_ok=True)
            _fail_batch(path,b['batch_id'],'recovered_staging_rollback')
            summary['rolled_back'].append(b['batch_id']);continue
        try:
            _,digest,manifest,data=open_verified_package(root,b['package_id'],b['package_digest'])
            members=manifest['members']
            now=_now()
            out=_commit_archive(path,b['batch_id'],b['permit_id'],b['package_id'],digest,str(root),members,now,None)
            summary['recommitted'].append(b['batch_id'])
        except ArchiveVerifyError as e:
            bad=pkg_dir/(b['package_id']+'.tar')
            if bad.exists():os.replace(bad,bad.with_suffix('.corrupt'))
            index=_load_index(idx)
            index['packages']=[p for p in index['packages'] if p['package_id']!=b['package_id']]
            _write_index(idx,index)
            _fail_batch(path,b['batch_id'],'recover_verify_failed:'+str(e))
            summary['failed'].append(b['batch_id'])
    # Reconcile the restore index: a package published before a crash whose
    # batch never reached a terminal committed/restored state is orphaned and
    # must be removed together with its index entry.
    index=_load_index(idx)
    with closing(connect(path)) as live:
        live_pkgs={r[0] for r in live.execute(
            "SELECT DISTINCT package_id FROM archive_batch WHERE state IN ('committed','sealed','restored')")}
    keep=[];dropped=[]
    for entry in index['packages']:
        if entry['package_id'] in live_pkgs:
            keep.append(entry)
        else:
            dropped.append(entry['package_id'])
            pub=pkg_dir/(entry['package_id']+'.tar')
            if pub.exists():os.chmod(pub,0o644);pub.unlink()
    if dropped:
        index['packages']=keep;_write_index(idx,index)
        summary.setdefault('orphan_packages_removed',[]).extend(dropped)
    return summary

# ---------------------------------------------------------------------------
# Reverse recovery
# ---------------------------------------------------------------------------
def restore(path, archive_root=None, *, seq=None, evidence_id=None, all_=False, out=None):
    """Bring archived originals back to the hot store (or extract to files)."""
    if not any((seq is not None,evidence_id,all_)):raise ArchiveError('restore_needs_target')
    from pathlib import Path
    targets=[]
    with closing(connect(path)) as db:
        rows=db.execute('''SELECT ae.evidence_id,ae.batch_id,e.seq,e.kind,ab.package_id,ab.package_digest,ab.root
                           FROM archived_evidence ae JOIN evidence e ON e.id=ae.evidence_id
                           JOIN archive_batch ab ON ab.batch_id=ae.batch_id ORDER BY e.seq''').fetchall()
        for r in rows:
            if all_ or (seq is not None and r['seq']==seq) or (evidence_id and r['evidence_id']==evidence_id):
                targets.append(dict(r))
    if not targets:raise ArchiveError('restore_target_not_archived')
    restored=[];cache={}
    if out:
        # Extraction-only mode: write the verified originals to files while
        # leaving the cold pointers and hot/cold state completely untouched.
        out=Path(out);out.mkdir(parents=True,exist_ok=True)
        with closing(connect(path)) as db:
            for t in targets:
                blob=read_cold_raw(db,archive_root,t['evidence_id'],cache)
                (out/(str(t['seq'])+'-'+t['evidence_id']+'.json')).write_bytes(blob)
                restored.append({'evidence_id':t['evidence_id'],'seq':t['seq'],'bytes':len(blob)})
        return {'restored':restored,'extracted_to':str(out),'bytes':sum(r['bytes'] for r in restored)}
    with closing(connect(path)) as db,db:
        db.execute('PRAGMA busy_timeout=5000')
        for t in targets:
            blob=read_cold_raw(db,archive_root,t['evidence_id'],cache)
            db.execute('UPDATE evidence SET raw=? WHERE id=?',(blob,t['evidence_id']))
            db.execute('DELETE FROM archived_evidence WHERE evidence_id=?',(t['evidence_id'],))
            restored.append({'evidence_id':t['evidence_id'],'seq':t['seq'],'bytes':len(blob)})
        for batch in {t['batch_id'] for t in targets}:
            left=db.execute('SELECT COUNT(*) FROM archived_evidence WHERE batch_id=?',(batch,)).fetchone()[0]
            if left==0:db.execute("UPDATE archive_batch SET state='restored' WHERE batch_id=?",(batch,))
    return {'restored':restored,'extracted_to':None,'bytes':sum(r['bytes'] for r in restored)}

# ---------------------------------------------------------------------------
# Consistency check and status
# ---------------------------------------------------------------------------
def consistency_check(path, archive_root=None):
    errors=[];checks={'hot_rows':0,'cold_rows':0,'packages':0}
    def add_error(code,**kw):
        item={'code':code};item.update(kw)
        if item not in errors:errors.append(item)
    with closing(connect(path)) as db:
        cache={}
        for ev in db.execute('SELECT * FROM evidence'):
            ev=dict(ev)
            if ev['raw'] is not None:
                checks['hot_rows']+=1
                if _sha256(ev['raw'])!=ev['sha']:
                    add_error('hot_sha_mismatch',source_id=ev['id'])
                continue
            checks['cold_rows']+=1
            ptr=db.execute('SELECT * FROM archived_evidence WHERE evidence_id=?',(ev['id'],)).fetchone()
            if not ptr:
                add_error('missing_raw_without_pointer',source_id=ev['id']);continue
            try:
                root=archive_root or dict(db.execute('SELECT root FROM archive_batch WHERE batch_id=?',
                                                     (ptr['batch_id'],)).fetchone())['root']
                if ptr['package_id'] not in cache:
                    _,digest,manifest,data=open_verified_package(root,ptr['package_id'],
                        db.execute('SELECT package_digest FROM archive_batch WHERE package_id=?',
                                   (ptr['package_id'],)).fetchone()[0])
                    cache[ptr['package_id']]=(root,data,manifest)
                _read_member(cache[ptr['package_id']][1],cache[ptr['package_id']][2],ev['id'],ptr['sha'])
                if ptr['sha']!=ev['sha']:
                    add_error('pointer_sha_mismatch',source_id=ev['id'])
            except ArchiveVerifyError as e:
                msg=str(e)
                if msg.startswith('package_tampered') or msg.startswith('package_missing'):
                    mates=[r['evidence_id'] for r in db.execute(
                        'SELECT evidence_id FROM archived_evidence WHERE package_id=?',(ptr['package_id'],))]
                    for sid in mates:
                        add_error('package_tampered',source_id=sid,package_id=ptr['package_id'])
                else:
                    add_error('member_verify_failed',source_id=ev['id'],detail=msg)
        # Restore index must agree with the DB pointers.
        if archive_root is not None:
            from pathlib import Path
            idx=Path(archive_root)/'index.json'
            index=_load_index(idx) if idx.exists() else {'packages':[]}
            db_pkgs={r[0] for r in db.execute(
                "SELECT DISTINCT package_id FROM archived_evidence")}
            idx_pkgs={p['package_id'] for p in index['packages']}
            for pkg in db_pkgs-idx_pkgs:errors.append({'code':'index_missing_package','package_id':pkg})
            for pkg in idx_pkgs-db_pkgs:
                if not db.execute('SELECT 1 FROM archive_batch WHERE package_id=? AND state=?',(pkg,'restored')).fetchone():
                    errors.append({'code':'orphan_index_entry','package_id':pkg})
            checks['packages']=len(index['packages'])
        unrecovered=[dict(r) for r in db.execute(
            "SELECT batch_id,state FROM archive_batch WHERE state IN ('staging','sealed')")]
        for b in unrecovered:errors.append({'code':'batch_not_terminal','batch_id':b['batch_id'],'state':b['state']})
    return {'status':'consistent' if not errors else 'inconsistent','errors':errors,'checks':checks}

def archive_status(path, archive_root=None):
    with closing(connect(path)) as db:
        hot=db.execute('SELECT COUNT(*) n,COALESCE(SUM(LENGTH(raw)),0) b FROM evidence WHERE raw IS NOT NULL').fetchone()
        cold=db.execute('''SELECT COUNT(*) n,COALESCE(SUM(ae.raw_size),0) b FROM archived_evidence ae
                           JOIN evidence e ON e.id=ae.evidence_id WHERE e.raw IS NULL''').fetchone()
        batches=[dict(r) for r in db.execute('SELECT * FROM archive_batch ORDER BY created_at')]
        denials=[dict(r) for r in db.execute('SELECT * FROM archive_denial ORDER BY id')]
        holds=[dict(r) for r in db.execute('SELECT hold_id,plot_id,source_ids,order_ref,lifted_at FROM legal_hold ORDER BY hold_id')]
        permits=[dict(r) for r in db.execute('SELECT permit_id,route,plot_id,status,expires_at FROM archive_permit ORDER BY permit_id')]
    return {'hot_count':hot['n'],'hot_bytes':hot['b'],'cold_count':cold['n'],'cold_bytes':cold['b'],
            'permits':permits,'active_holds':holds,'batches':batches,'denials':denials}
