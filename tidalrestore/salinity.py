"""Salinity logger capture: signed chunked manifests, staging and atomic publish.

A logger session is one *group*.  The device signs a manifest that names the
device, the logger sequence range, the ordered piece set, each piece's byte
digest and a total digest over the whole group.  Pieces may arrive out of
order, be retransmitted, or be handed over between collectors; **only** when
every declared piece is present, every digest matches, the manifest and each
piece verify under the pinned device key, the calibration piece covers the
sample timestamps, and pieces all belong to the manifest's device, does the
group become a single committed source.  Until then it stays *staging* and the
public summary says nothing about that logger.

The legacy whole-file import is kept as one committed file source
(``file:<id>``) so old callers, the CLI demo and the audit replay keep working.
It still commits atomically and still refuses an ``id`` whose bytes changed.

Nothing public is deleted or rewritten.  A signed manifest revision that
supersedes an earlier one closes the older publication interval
(``effective_to``) and marks the old source ``superseded``; its samples and raw
bytes remain queryable forever, and ``summary(as_of=...)`` can reproduce the
old public answer.  Same-piece-index/different-bytes packets are sealed in
``salinity_conflict`` and never committed.

Crash recovery: receipt of a packet and publication of a group are *separate*
transactions, so a crash between them leaves a fully durable staging state.
Every publication step is one SQLite transaction that either commits fully or
not at all, and re-running the same packet (or ``recover``) is idempotent and
converges to the same rows as an uninterrupted import.
"""
import hashlib
import json
import time
from pathlib import Path
from contextlib import closing, contextmanager
from .store import connect
from .signing import SignatureError, verify_manifest, verify_signature

FAULT_POINTS = ('after_manifest', 'after_piece', 'before_commit', 'after_commit')
DEFAULT_LOG_COLLECTOR = 'logger'


class InjectedCrash(RuntimeError):
    """Raised by the fault harness to simulate power loss at a named seam."""


def canonical_bytes(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode('utf8')


def _sha(raw):
    return hashlib.sha256(raw).hexdigest()


def _now(stamp=None):
    if stamp:
        return stamp
    now = time.time()
    base = time.strftime('%Y-%m-%dT%H:%M:%S', time.gmtime(now))
    return f'{base}.{int((now % 1) * 1_000_000):06d}Z'


def _fault(fault, point):
    if fault and point in fault:
        raise InjectedCrash('injected_crash:' + point)


def _parse(raw):
    try:
        doc = json.loads(raw)
    except (ValueError, UnicodeDecodeError):
        raise ValueError('invalid_packet_json')
    if not isinstance(doc, dict):
        raise ValueError('invalid_packet_json')
    return doc


@contextmanager
def _write_session(path):
    """One serialized write transaction (BEGIN IMMEDIATE).

    IMMEDIATE makes concurrent collectors queue at the start of the
    transaction instead of failing on a read-to-write upgrade, which is what
    lets exactly one of them publish a group.
    """
    with closing(connect(path)) as db:
        db.execute('BEGIN IMMEDIATE')
        try:
            yield db
        except BaseException:
            db.rollback()
            raise
        else:
            db.commit()


# ---------------------------------------------------------------------------
# Packet classification
# ---------------------------------------------------------------------------

def _is_manifest(doc):
    return doc.get('kind') in ('salinity_logger_manifest', 'salinity_manifest')


def _is_piece(doc):
    return doc.get('kind') in ('salinity_logger_piece', 'salinity_piece')


def import_logger(path, file, *, collector=DEFAULT_LOG_COLLECTOR, fault=None, at=None):
    """Import one capture packet: signed manifest, one piece, or legacy file.

    Returns a JSON-serialisable status.  A piece/manifest that is valid but
    not yet sufficient returns ``status='staging'`` with a blocking reason and
    never exposes a partial peak.
    """
    raw = Path(file).read_bytes()
    doc = _parse(raw)
    if _is_manifest(doc):
        return receive_manifest(path, raw, collector=collector, fault=fault, at=at)
    if _is_piece(doc):
        return receive_piece(path, raw, collector=collector, fault=fault, at=at)
    return _import_complete_file(path, raw, doc, collector=collector, fault=fault, at=at)


def _import_complete_file(path, raw, doc, *, collector, fault, at):
    """Legacy single-document import, expressed as a committed file source."""
    if doc.get('kind') != 'salinity_logger' or not doc.get('id') or not doc.get('samples'):
        raise ValueError('invalid_logger_file')
    samples = [_validated_sample(s) for s in doc['samples']]
    digest = _sha(raw)
    source_key = 'file:' + doc['id']
    stamp = _now(at)
    error = None
    with _write_session(path) as db:
        old = db.execute('SELECT sha FROM salinity_source WHERE source_key=?', (source_key,)).fetchone()
        if old:
            if old['sha'] != digest:
                _seal_conflict(db, 'manifest', source_key, None, old['sha'], digest, raw,
                               'logger_id_conflict', stamp)
                error = 'logger_id_conflict'
            else:
                return {'id': doc['id'], 'replayed': True, 'status': 'committed'}
        else:
            db.execute('INSERT INTO salinity_source(source_key,source_id,kind,status,sha,raw,group_key,'
                       'manifest_key,revision,device_id,seq_start,seq_end,committed_at,created_at) '
                       "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                       (source_key, doc['id'], 'file', 'committed', digest, raw, source_key, None,
                        1, doc.get('device_id'), None, None, stamp, stamp))
            for seq, item in enumerate(samples, 1):
                db.execute('INSERT INTO salinity_sample(source_key,source_id,seq,station,at,'
                           'conductivity,tide) VALUES(?,?,?,?,?,?,?)',
                           (source_key, doc['id'], seq, item['station'], item['at'],
                            item['conductivity'], item['tide']))
            db.execute('INSERT INTO salinity_publication(group_key,source_key,manifest_key,revision,'
                       'effective_from,effective_to,status) VALUES(?,?,?,?,?,?,?)',
                       (source_key, source_key, None, 1, stamp, None, 'active'))
            _event(db, source_key, None, 'committed', 'complete_file', collector, stamp)
            _receipt(db, 'file', digest, None, None, collector, stamp, raw)
    if error:
        raise ValueError(error)
    _fault(fault, 'after_commit')
    return {'id': doc['id'], 'status': 'committed', 'samples': len(samples)}


# ---------------------------------------------------------------------------
# Manifest receipt
# ---------------------------------------------------------------------------

def receive_manifest(path, raw, *, collector=DEFAULT_LOG_COLLECTOR, fault=None, at=None):
    doc = _parse(raw)
    if not _is_manifest(doc):
        raise ValueError('not_a_manifest')
    if not doc.get('manifest_id') or not doc.get('device_id'):
        raise ValueError('invalid_manifest')
    if not isinstance(doc.get('revision'), int) or doc['revision'] < 1:
        raise ValueError('invalid_manifest')
    if not isinstance(doc.get('pieces'), list) or not doc['pieces']:
        raise ValueError('invalid_manifest')
    manifest_id = doc['manifest_id']
    revision = doc['revision']
    manifest_key = f'{manifest_id}@{revision}'
    digest = _sha(raw)
    stamp = _now(at)
    error = None
    replayed = False

    with _write_session(path) as db:
        old = db.execute('SELECT sha FROM salinity_manifest WHERE manifest_key=?', (manifest_key,)).fetchone()
        if old:
            if old['sha'] != digest:
                _seal_conflict(db, 'manifest', manifest_key, None, old['sha'], digest, raw,
                               'manifest_id_conflict', stamp)
                error = 'manifest_id_conflict'
            else:
                replayed = True
                _event(db, manifest_id, manifest_key, 'manifest_replayed', 'same_bytes', collector, stamp)
        if old is None:
            supersedes_key = doc.get('supersedes')
            signature_ok = _verify_manifest(doc)
            revocation_ok = 1
            reason = None
            if supersedes_key:
                prior = db.execute('SELECT * FROM salinity_manifest WHERE manifest_key=?',
                                   (supersedes_key,)).fetchone()
                if prior is None:
                    revocation_ok = 0
                    reason = 'superseded_manifest_unknown'
                elif prior['sha'] != doc.get('supersedes_sha'):
                    revocation_ok = 0
                    reason = 'supersedes_sha_mismatch'
                elif not _verify_revocation(doc):
                    revocation_ok = 0
                    reason = 'revocation_signature_invalid'
                elif prior['status'] == 'revoked':
                    revocation_ok = 0
                    reason = 'already_revoked'
            total_sha = doc.get('total_sha')
            if not total_sha or not isinstance(total_sha, str):
                reason = reason or 'missing_total_digest'
            indices = [p.get('piece_index') for p in doc['pieces']]
            if indices != list(range(1, len(doc['pieces']) + 1)):
                reason = reason or 'invalid_piece_order'
            seq_start, seq_end = doc.get('seq_start'), doc.get('seq_end')
            if not isinstance(seq_start, int) or not isinstance(seq_end, int) or seq_start > seq_end:
                reason = reason or 'invalid_sequence_range'
            calibration = doc.get('calibration')
            calibration_piece = calibration_from = calibration_to = None
            if calibration:
                calibration_piece = calibration.get('piece_index')
                calibration_from, calibration_to = calibration.get('from'), calibration.get('to')
                if calibration_piece not in indices or not calibration_from or not calibration_to \
                        or calibration_from > calibration_to:
                    reason = reason or 'invalid_calibration_declaration'
            db.execute('INSERT INTO salinity_manifest(manifest_key,manifest_id,revision,device_id,sha,raw,'
                       'seq_start,seq_end,total_sha,calibration_piece,calibration_from,calibration_to,'
                       'supersedes_key,supersedes_sha,signature_ok,revocation_ok,status,reason,received_at) '
                       'VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                       (manifest_key, manifest_id, revision, doc['device_id'], digest, raw, seq_start,
                        seq_end, total_sha, calibration_piece, calibration_from, calibration_to,
                        supersedes_key, doc.get('supersedes_sha'), 1 if signature_ok else 0,
                        revocation_ok, 'staging', reason, stamp))
            _receipt(db, 'manifest', digest, manifest_key, None, collector, stamp, raw)
            _event(db, manifest_id, manifest_key, 'manifest_staged',
                   reason or 'awaiting_pieces', collector, stamp)
            _adopt_orphans(db, manifest_key, doc, stamp)

    if error:
        raise ValueError(error)
    _fault(fault, 'after_manifest')
    # A manifest may complete a group whose pieces arrived before it.
    return _finish(path, manifest_key, collector=collector, fault=fault, at=stamp,
                   replayed=replayed, strict=False)


def _verify_manifest(doc):
    try:
        verify_manifest(doc)
        return True
    except SignatureError:
        return False


def _verify_revocation(doc):
    """The superseding revision must carry a device-signed revocation of the
    *old* manifest digest, independent of the new manifest's own signature."""
    revocation = doc.get('revocation')
    if not isinstance(revocation, dict):
        return False
    statement = canonical_bytes({'manifest_id': doc['manifest_id'],
                                 'revision': doc['revision'],
                                 'supersedes': doc.get('supersedes'),
                                 'supersedes_sha': doc.get('supersedes_sha'),
                                 'revoked': True})
    try:
        verify_signature(statement, revocation.get('signature'), doc['device_id'])
        return True
    except SignatureError:
        return False


# ---------------------------------------------------------------------------
# Piece receipt (orphans included)
# ---------------------------------------------------------------------------

def receive_piece(path, raw, *, collector=DEFAULT_LOG_COLLECTOR, fault=None, at=None):
    doc = _parse(raw)
    if not _is_piece(doc):
        raise ValueError('not_a_piece')
    for field in ('manifest_id', 'revision', 'piece_index', 'device_id'):
        if doc.get(field) in (None, ''):
            raise ValueError('invalid_piece')
    if not isinstance(doc.get('revision'), int) or not isinstance(doc.get('piece_index'), int):
        raise ValueError('invalid_piece')
    if doc['revision'] < 1 or doc['piece_index'] < 1:
        raise ValueError('invalid_piece')
    digest = _sha(raw)
    manifest_key = f"{doc['manifest_id']}@{doc['revision']}"
    piece_index = doc['piece_index']
    stamp = _now(at)
    error = None
    replayed = False

    with _write_session(path) as db:
        manifest = db.execute('SELECT * FROM salinity_manifest WHERE manifest_key=?',
                              (manifest_key,)).fetchone()
        if manifest is None:
            # Out-of-order handover: keep the packet durably as an orphan; the
            # manifest will adopt or reject it when it arrives.
            duplicates = db.execute('SELECT sha FROM salinity_piece WHERE manifest_key=? AND piece_index=?',
                                    (manifest_key, piece_index)).fetchall()
            if any(row['sha'] == digest for row in duplicates):
                replayed = True
            else:
                db.execute('INSERT INTO salinity_piece(manifest_key,manifest_id,revision,piece_index,sha,raw,'
                           'device_id,signature_ok,status,received_at) VALUES(?,?,?,?,?,?,?,?,?,?)',
                           (manifest_key, doc['manifest_id'], doc['revision'], piece_index, digest, raw,
                            doc['device_id'], 1 if _verify_piece_signature(doc) else 0, 'orphan', stamp))
                _receipt(db, 'piece', digest, manifest_key, piece_index, collector, stamp, raw)
            _event(db, doc['manifest_id'], manifest_key, 'piece_orphaned',
                   f'piece={piece_index}', collector, stamp)
        else:
            declared = [p for p in json.loads(manifest['raw'])['pieces']
                        if p.get('piece_index') == piece_index]
            if not declared:
                _seal_conflict(db, 'piece', manifest_key, piece_index, None, digest, raw,
                               'piece_not_declared', stamp)
                error = 'piece_not_declared'
            elif doc['device_id'] != manifest['device_id']:
                _seal_conflict(db, 'piece', manifest_key, piece_index, None, digest, raw,
                               'piece_device_mismatch', stamp)
                error = 'piece_device_mismatch'
            else:
                accepted = db.execute('SELECT sha FROM salinity_piece WHERE manifest_key=? AND piece_index=? '
                                      "AND status IN ('received','orphan')",
                                      (manifest_key, piece_index)).fetchall()
                if any(row['sha'] == digest for row in accepted):
                    replayed = True
                    if not db.execute("SELECT 1 FROM salinity_piece WHERE manifest_key=? AND piece_index=? "
                                      "AND sha=? AND status='received'",
                                      (manifest_key, piece_index, digest)).fetchone() and len(accepted) == 1:
                        _adopt_piece_row(db, manifest, doc, digest, raw, stamp)
                    elif len(accepted) > 1:
                        _seal_conflict(db, 'piece', manifest_key, piece_index, None, digest, raw,
                                       'piece_index_ambiguous', stamp)
                elif accepted:
                    # The same piece index already holds different bytes.
                    _seal_conflict(db, 'piece', manifest_key, piece_index, accepted[0]['sha'], digest,
                                   raw, 'piece_index_conflict', stamp)
                    error = 'piece_index_conflict'
                elif not any(p.get('sha') == digest for p in declared):
                    _seal_conflict(db, 'piece', manifest_key, piece_index, None, digest, raw,
                                   'piece_digest_mismatch', stamp)
                    error = 'piece_digest_mismatch'
                else:
                    db.execute('INSERT INTO salinity_piece(manifest_key,manifest_id,revision,piece_index,sha,raw,'
                               'device_id,signature_ok,status,received_at) VALUES(?,?,?,?,?,?,?,?,?,?)',
                               (manifest_key, doc['manifest_id'], doc['revision'], piece_index, digest, raw,
                                doc['device_id'], 1 if _verify_piece_signature(doc) else 0, 'received', stamp))
                    _receipt(db, 'piece', digest, manifest_key, piece_index, collector, stamp, raw)
                    _event(db, doc['manifest_id'], manifest_key, 'piece_received',
                           f'piece={piece_index}', collector, stamp)

    if error:
        raise ValueError(error)
    _fault(fault, 'after_piece')
    return _finish(path, manifest_key, collector=collector, fault=fault, at=stamp,
                   replayed=replayed, strict=False)


def _verify_piece_signature(doc):
    try:
        verify_signature(canonical_bytes({k: v for k, v in doc.items() if k != 'signature'}),
                         doc.get('signature'), doc['device_id'])
        return True
    except SignatureError:
        return False


def _adopt_orphans(db, manifest_key, manifest_doc, stamp):
    """Validate pieces that arrived before their manifest.

    The manifest's declared (piece_index, sha) set is the authority.  Bytes
    that do not match the declaration are sealed, as are two different byte
    streams claiming the same declared index.
    """
    declared = {p.get('piece_index'): p.get('sha') for p in manifest_doc['pieces']}
    orphans = db.execute("SELECT * FROM salinity_piece WHERE manifest_key=? AND status='orphan' "
                         'ORDER BY received_at,rowid', (manifest_key,)).fetchall()
    seen = {}
    for row in orphans:
        expected = declared.get(row['piece_index'])
        if expected is None:
            _seal_conflict(db, 'piece', manifest_key, row['piece_index'], None, row['sha'],
                           row['raw'], 'piece_not_declared', stamp)
            continue
        if row['device_id'] != manifest_doc['device_id']:
            _seal_conflict(db, 'piece', manifest_key, row['piece_index'], None, row['sha'],
                           row['raw'], 'piece_device_mismatch', stamp)
            continue
        if row['sha'] != expected:
            _seal_conflict(db, 'piece', manifest_key, row['piece_index'], None, row['sha'],
                           row['raw'], 'piece_digest_mismatch', stamp)
            continue
        if row['piece_index'] in seen:
            _seal_conflict(db, 'piece', manifest_key, row['piece_index'], seen[row['piece_index']],
                           row['sha'], row['raw'], 'piece_index_ambiguous', stamp)
            continue
        seen[row['piece_index']] = row['sha']
        db.execute("UPDATE salinity_piece SET status='received' WHERE manifest_key=? AND piece_index=? AND sha=?",
                   (manifest_key, row['piece_index'], row['sha']))


def _adopt_piece_row(db, manifest, doc, digest, raw, stamp):
    entry = {p['piece_index']: p for p in json.loads(manifest['raw'])['pieces']}.get(doc['piece_index'])
    if entry is None or digest != entry.get('sha') or doc['device_id'] != manifest['device_id']:
        _seal_conflict(db, 'piece', manifest['manifest_key'], doc['piece_index'], None, digest, raw,
                       'piece_conflict_during_adoption', stamp)
        return
    db.execute("UPDATE salinity_piece SET status='received' WHERE manifest_key=? AND piece_index=? AND sha=?",
               (manifest['manifest_key'], doc['piece_index'], digest))


# ---------------------------------------------------------------------------
# Group verification + atomic publication
# ---------------------------------------------------------------------------

def _finish(path, manifest_key, *, collector, fault, at, replayed, strict):
    """Try to publish; otherwise report staging without leaking any peak."""
    try:
        return try_commit_group(path, manifest_key, collector=collector, fault=fault, at=at,
                                replayed=replayed)
    except ValueError as exc:
        reason = str(exc)
        if strict:
            raise
        with closing(connect(path)) as db:
            manifest = db.execute('SELECT 1 FROM salinity_manifest WHERE manifest_key=?',
                                  (manifest_key,)).fetchone()
            if manifest is None:
                return {'manifest_key': manifest_key, 'status': 'staging', 'reason': reason,
                        'blocking': reason, 'replayed': replayed, 'pieces_received': 0,
                        'pieces_declared': None}
            return _status(db, manifest_key, replayed=replayed, reason=reason)


def group_state(path, manifest_key):
    with closing(connect(path)) as db:
        return _status(db, manifest_key)


def try_commit_group(path, manifest_key, *, collector=DEFAULT_LOG_COLLECTOR, fault=None, at=None,
                     replayed=False):
    """Verify a fully staged group and publish it as one indivisible source.

    Safe to call repeatedly; an already-committed group is a no-op returning
    ``replayed``.  A failed precondition raises ``ValueError(reason)`` and
    leaves the group staging.
    """
    stamp = _now(at)
    fresh = False
    with _write_session(path) as db:
        manifest = db.execute('SELECT * FROM salinity_manifest WHERE manifest_key=?',
                              (manifest_key,)).fetchone()
        if manifest is None:
            raise ValueError('unknown_manifest')
        existing = db.execute('SELECT * FROM salinity_source WHERE manifest_key=? AND status=?',
                              (manifest_key, 'committed')).fetchone()
        if existing is not None:
            return _source_result(db, existing, replayed=True)
        if manifest['status'] == 'revoked':
            raise ValueError('manifest_revoked')
        reason = _group_problem(db, manifest)
        if reason:
            _event(db, manifest['manifest_id'], manifest_key, 'group_pending', reason, collector, stamp)
            raise ValueError(reason)
        ordered = sorted(json.loads(manifest['raw'])['pieces'], key=lambda p: p['piece_index'])
        pieces = [db.execute('SELECT * FROM salinity_piece WHERE manifest_key=? AND piece_index=? '
                             "AND status='received'", (manifest_key, entry['piece_index'])).fetchone()
                  for entry in ordered]
        _fault(fault, 'before_commit')
        fresh = True
        source_key = f"group:{manifest['manifest_id']}@{manifest['revision']}"
        _retire_predecessors(db, manifest, stamp, collector)
        db.execute('INSERT INTO salinity_source(source_key,source_id,kind,status,sha,raw,group_key,'
                   'manifest_key,revision,device_id,seq_start,seq_end,committed_at,created_at) '
                   "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                   (source_key, manifest['manifest_id'], 'chunked', 'committed',
                    hashlib.sha256(manifest['raw']).hexdigest(), manifest['raw'], manifest['manifest_id'],
                    manifest_key, manifest['revision'], manifest['device_id'], manifest['seq_start'],
                    manifest['seq_end'], stamp, stamp))
        offset = 0
        for piece in pieces:
            body_samples = json.loads(piece['raw']).get('samples') or []
            for sample in body_samples:
                sample = _validated_sample(sample)
                seq = manifest['seq_start'] + offset
                db.execute('INSERT INTO salinity_sample(source_key,source_id,seq,station,at,'
                           'conductivity,tide) VALUES(?,?,?,?,?,?,?)',
                           (source_key, manifest['manifest_id'], seq, sample['station'],
                            sample['at'], sample['conductivity'], sample['tide']))
                offset += 1
        db.execute("UPDATE salinity_manifest SET status='committed',reason=NULL WHERE manifest_key=?",
                   (manifest_key,))
        db.execute('INSERT INTO salinity_publication(group_key,source_key,manifest_key,revision,'
                   'effective_from,effective_to,status) VALUES(?,?,?,?,?,?,?)',
                   (manifest['manifest_id'], source_key, manifest_key, manifest['revision'],
                    stamp, None, 'active'))
        _event(db, manifest['manifest_id'], manifest_key, 'group_committed', source_key, collector, stamp)
        _receipt(db, 'commit', source_key, manifest_key, None, collector, stamp, b'')
        result = _source_result(db, db.execute('SELECT * FROM salinity_source WHERE source_key=?',
                                               (source_key,)).fetchone(), replayed=replayed)
    if fresh:
        # Outside the transaction: the commit is already durable, exactly like
        # a power loss immediately after the DB flush.
        _fault(fault, 'after_commit')
    return result


def _source_result(db, source, replayed=False):
    count = db.execute('SELECT COUNT(*) FROM salinity_sample WHERE source_key=?',
                       (source['source_key'],)).fetchone()[0]
    return {'id': source['source_id'], 'status': 'committed', 'source_key': source['source_key'],
            'samples': count, 'replayed': replayed}


def _group_problem(db, manifest):
    if not manifest['signature_ok']:
        return 'manifest_signature_invalid'
    if manifest['supersedes_key'] and not manifest['revocation_ok']:
        return manifest['reason'] or 'revocation_signature_invalid'
    if manifest['reason']:
        return manifest['reason']
    doc = json.loads(manifest['raw'])
    declared = {p['piece_index']: p for p in doc['pieces']}
    present = {}
    for piece in db.execute("SELECT * FROM salinity_piece WHERE manifest_key=? AND status='received'",
                            (manifest['manifest_key'],)):
        present.setdefault(piece['piece_index'], []).append(piece)
    total = hashlib.sha256()
    for index in sorted(declared):
        candidates = present.get(index, [])
        if not candidates:
            return f'missing_piece:{index}'
        if len(candidates) > 1:
            return f'duplicate_piece:{index}'
        piece = candidates[0]
        if not piece['signature_ok']:
            return f'piece_signature_invalid:{index}'
        if piece['device_id'] != manifest['device_id']:
            return 'device_identity_mismatch'
        total.update(bytes.fromhex(piece['sha']))
    if total.hexdigest() != manifest['total_sha']:
        return 'total_digest_mismatch'
    samples = []
    for index in sorted(declared):
        samples.extend(json.loads(present[index][0]['raw']).get('samples') or [])
    if len(samples) != manifest['seq_end'] - manifest['seq_start'] + 1:
        return 'sequence_range_mismatch'
    try:
        for sample in samples:
            _validated_sample(sample)
    except ValueError:
        return 'invalid_salinity_sample'
    calibration = doc.get('calibration')
    if calibration:
        piece = present.get(calibration['piece_index'], [None])[0]
        if piece is None:
            return f"missing_piece:{calibration['piece_index']}"
        body = json.loads(piece['raw'])
        window = body.get('calibration') or {}
        if window.get('from') != calibration['from'] or window.get('to') != calibration['to']:
            return 'calibration_manifest_mismatch'
        for sample in samples:
            if not calibration['from'] <= sample['at'] <= calibration['to']:
                return f"calibration_window_gap:{sample['at']}"
    return None


def _validated_sample(item):
    if not isinstance(item, dict):
        raise ValueError('invalid_salinity_sample')
    if item.get('conductivity') is None or not isinstance(item['conductivity'], int) \
            or item['conductivity'] < 0 or item.get('tide') not in ('flood', 'ebb'):
        raise ValueError('invalid_salinity_sample')
    if not item.get('station') or not item.get('at'):
        raise ValueError('invalid_salinity_sample')
    return item


def _retire_predecessors(db, manifest, stamp, collector):
    """Close the public intervals that this signed group replaces.

    A revision explicitly revokes its named predecessor, even if that
    predecessor never committed (the old manifest stays as a revoked version).
    Independently, a newly committed group supersedes any earlier active
    publication that reported the same station: the station's public answer is
    whichever fully verified group was published last, and the prior interval
    is preserved with its ``effective_to`` rather than rewritten or deleted.
    """
    if manifest['supersedes_key']:
        prior = db.execute('SELECT * FROM salinity_manifest WHERE manifest_key=?',
                           (manifest['supersedes_key'],)).fetchone()
        if prior is not None:
            db.execute("UPDATE salinity_publication SET effective_to=?,status='closed' "
                       "WHERE manifest_key=? AND status='active'", (stamp, prior['manifest_key']))
            db.execute("UPDATE salinity_manifest SET status='revoked' "
                       "WHERE manifest_key=? AND status!='revoked'", (prior['manifest_key'],))
            db.execute("UPDATE salinity_source SET status='superseded' "
                       "WHERE manifest_key=? AND status='committed'", (prior['manifest_key'],))
            _event(db, manifest['manifest_id'], prior['manifest_key'], 'manifest_revoked',
                   manifest['manifest_key'], collector, stamp)
    stations = set()
    for row in db.execute('SELECT raw FROM salinity_piece WHERE manifest_key=?',
                          (manifest['manifest_key'],)):
        try:
            for sample in json.loads(row['raw']).get('samples') or []:
                if sample.get('station'):
                    stations.add(sample['station'])
        except (ValueError, AttributeError, TypeError):
            continue
    if not stations:
        return
    active = db.execute(
        'SELECT p.id,p.source_key,p.manifest_key FROM salinity_publication p '
        "WHERE p.status='active' AND p.effective_to IS NULL AND p.manifest_key IS NOT ?",
        (manifest['manifest_key'],)).fetchall()
    placeholders = ','.join('?' * len(stations))
    for pub in active:
        overlap = db.execute(
            f'SELECT 1 FROM salinity_sample WHERE source_key=? AND station IN ({placeholders}) LIMIT 1',
            (pub['source_key'], *sorted(stations))).fetchone()
        if overlap is None:
            continue
        db.execute("UPDATE salinity_publication SET effective_to=?,status='closed' WHERE id=?",
                   (stamp, pub['id']))
        if pub['manifest_key']:
            db.execute("UPDATE salinity_manifest SET status='revoked' "
                       "WHERE manifest_key=? AND status!='revoked'", (pub['manifest_key'],))
        db.execute("UPDATE salinity_source SET status='superseded' WHERE source_key=? AND status='committed'",
                   (pub['source_key'],))
        _event(db, manifest['manifest_id'], pub['manifest_key'], 'publication_superseded',
               manifest['manifest_key'], collector, stamp)


# ---------------------------------------------------------------------------
# Recovery, status, conflicts
# ---------------------------------------------------------------------------

def recover(path, *, collector=DEFAULT_LOG_COLLECTOR, fault=None, at=None):
    """Re-scan staging groups after a restart and publish any that verify.

    Returns one outcome per manifest in receipt order.  Never deletes or
    rewrites committed rows; committed groups report ``already_committed``.
    """
    stamp = _now(at)
    with closing(connect(path)) as db:
        keys = [row['manifest_key'] for row in db.execute(
            'SELECT manifest_key FROM salinity_manifest ORDER BY received_at,manifest_key')]
    outcomes = []
    for key in keys:
        try:
            result = try_commit_group(path, key, collector=collector, fault=fault, at=stamp)
            outcome = 'committed' if not result['replayed'] else 'already_committed'
        except ValueError as exc:
            reason = str(exc)
            outcome = 'revoked' if reason == 'manifest_revoked' else 'staging:' + reason
        outcomes.append({'manifest_key': key, 'outcome': outcome})
        with _write_session(path) as db:
            db.execute('INSERT INTO salinity_restore(group_key,outcome,at) VALUES(?,?,?)',
                       (key, outcome, stamp))
    return {'restored': outcomes, 'at': stamp}


def pending(path):
    """Staging groups and sealed conflicts: what is not public, and why."""
    with closing(connect(path)) as db:
        groups = []
        for row in db.execute("SELECT manifest_key FROM salinity_manifest WHERE status='staging' "
                              'ORDER BY received_at,manifest_key'):
            manifest = db.execute('SELECT * FROM salinity_manifest WHERE manifest_key=?',
                                  (row['manifest_key'],)).fetchone()
            reason = _group_problem(db, manifest)
            groups.append({'manifest_key': row['manifest_key'], 'blocking': reason,
                           'pieces_received': db.execute(
                               "SELECT COUNT(*) FROM salinity_piece WHERE manifest_key=? AND status='received'",
                               (row['manifest_key'],)).fetchone()[0]})
        conflicts = [dict(r) for r in db.execute(
            'SELECT scope,object_key,piece_index,existing_sha,received_sha,reason '
            'FROM salinity_conflict ORDER BY id')]
        return {'staging': groups, 'conflicts': conflicts}


def _status(db, manifest_key, replayed=False, reason=None):
    manifest = db.execute('SELECT * FROM salinity_manifest WHERE manifest_key=?', (manifest_key,)).fetchone()
    if manifest is None:
        raise ValueError('unknown_manifest')
    source = db.execute('SELECT * FROM salinity_source WHERE manifest_key=? AND status=?',
                        (manifest_key, 'committed')).fetchone()
    received = db.execute("SELECT COUNT(*) FROM salinity_piece WHERE manifest_key=? AND status='received'",
                          (manifest_key,)).fetchone()[0]
    declared = len(json.loads(manifest['raw'])['pieces'])
    base = {'manifest_key': manifest_key, 'manifest_id': manifest['manifest_id'],
            'revision': manifest['revision'], 'pieces_received': received,
            'pieces_declared': declared, 'replayed': replayed}
    if source is not None:
        result = _source_result(db, source, replayed=replayed)
        result.update(manifest_key=manifest_key, revision=manifest['revision'],
                      pieces_received=received, pieces_declared=declared,
                      manifest_status=manifest['status'])
        return result
    if manifest['status'] == 'revoked':
        base.update(status='revoked', reason='superseded_by_signed_revision',
                    manifest_status='revoked')
        return base
    blocking = reason or _group_problem(db, manifest) or 'unknown'
    base.update(status='staging', reason=blocking, blocking=blocking,
                manifest_status=manifest['status'])
    return base


# ---------------------------------------------------------------------------
# Conflict sealing + receipts
# ---------------------------------------------------------------------------

def _seal_conflict(db, scope, object_key, piece_index, existing_sha, received_sha, raw, reason, at):
    if db.execute('SELECT 1 FROM salinity_conflict WHERE scope=? AND object_key=? '
                  'AND piece_index IS ? AND received_sha=?',
                  (scope, object_key, piece_index, received_sha)).fetchone():
        return
    db.execute('INSERT INTO salinity_conflict(scope,object_key,piece_index,existing_sha,received_sha,'
               'raw,reason,detected_at) VALUES(?,?,?,?,?,?,?,?)',
               (scope, object_key, piece_index, existing_sha, received_sha, raw, reason, at))


def _event(db, group_key, manifest_key, event, detail, collector, at):
    db.execute('INSERT INTO salinity_event(group_key,manifest_key,event,detail,collector,at) '
               'VALUES(?,?,?,?,?,?)', (group_key, manifest_key, event, detail, collector, at))


def _receipt(db, kind, digest, manifest_key, piece_index, collector, at, raw):
    db.execute('INSERT INTO salinity_receipt(packet_kind,packet_sha,manifest_key,piece_index,'
               'collector,received_at,raw) VALUES(?,?,?,?,?,?,?)',
               (kind, digest, manifest_key, piece_index, collector, at, raw))


# ---------------------------------------------------------------------------
# Public summary / history
# ---------------------------------------------------------------------------

def summary(path, threshold=100, as_of=None):
    """Station peak over *active* publications only.

    A staging group contributes nothing.  With ``as_of`` the summary uses the
    publication intervals that were in force at that instant, so an old report
    survives a later signed revision.
    """
    with closing(connect(path)) as db:
        if as_of is None:
            rows = db.execute(
                'SELECT station,MAX(conductivity) AS peak,COUNT(*) AS n '
                'FROM salinity_sample s JOIN salinity_publication p ON p.source_key=s.source_key '
                "WHERE p.status='active' AND p.effective_to IS NULL GROUP BY station ORDER BY station").fetchall()
        else:
            rows = db.execute(
                'SELECT station,MAX(conductivity) AS peak,COUNT(*) AS n '
                'FROM salinity_sample s JOIN salinity_publication p ON p.source_key=s.source_key '
                'WHERE p.effective_from<=? AND (p.effective_to IS NULL OR p.effective_to>?) '
                'GROUP BY station ORDER BY station', (as_of, as_of)).fetchall()
        return [{'station': r['station'], 'peak': r['peak'], 'count': r['n'],
                 'status': 'alert' if r['peak'] > threshold else 'normal'} for r in rows]


def history(path):
    """Publication intervals with their per-station public peaks.

    Old intervals are closed rather than deleted, so a reader can always see
    both the previous public answer and when the new one took effect.
    """
    with closing(connect(path)) as db:
        rows = []
        for pub in db.execute('SELECT * FROM salinity_publication ORDER BY effective_from,id'):
            peaks = [dict(r) for r in db.execute(
                'SELECT station,MAX(conductivity) AS peak,COUNT(*) AS count FROM salinity_sample '
                'WHERE source_key=? GROUP BY station ORDER BY station', (pub['source_key'],))]
            entry = dict(pub)
            entry['stations'] = peaks
            rows.append(entry)
        return rows


def staging_stations(path, as_of=None):
    """Stations mentioned by live staging packets (revoked groups excluded)."""
    with closing(connect(path)) as db:
        stations = set()
        live = {row['manifest_key'] for row in db.execute(
            "SELECT manifest_key FROM salinity_manifest WHERE status='staging'")}
        known = {row['manifest_key'] for row in db.execute('SELECT manifest_key FROM salinity_manifest')}
        for row in db.execute("SELECT manifest_key,raw FROM salinity_piece "
                              "WHERE status IN ('received','orphan')"):
            # Orphans whose manifest has not arrived yet are still live capture
            # material for that session and must hold the gate.
            if row['manifest_key'] in known and row['manifest_key'] not in live:
                continue
            try:
                for sample in json.loads(row['raw']).get('samples') or []:
                    if sample.get('station'):
                        stations.add(sample['station'])
            except (ValueError, AttributeError, TypeError):
                continue
        for row in db.execute("SELECT raw FROM salinity_manifest WHERE status='staging'"):
            try:
                doc = json.loads(row['raw'])
            except (ValueError, TypeError):
                continue
            for sample in doc.get('samples') or []:
                if sample.get('station'):
                    stations.add(sample['station'])
            for piece in doc.get('pieces') or []:
                for sample in piece.get('samples') or []:
                    if isinstance(sample, dict) and sample.get('station'):
                        stations.add(sample['station'])
        return sorted(stations)
