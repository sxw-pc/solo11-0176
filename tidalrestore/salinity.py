"""Local salinity logger import. Raw packets and the current summary stay separate."""
import hashlib
import json
from pathlib import Path
from contextlib import closing
from .store import connect


def import_logger(path, file):
    raw = Path(file).read_bytes()
    doc = json.loads(raw)
    if doc.get('kind') != 'salinity_logger' or not doc.get('id') or not doc.get('samples'):
        raise ValueError('invalid_logger_file')
    with closing(connect(path)) as db, db:
        db.execute('CREATE TABLE IF NOT EXISTS salinity_source(id TEXT PRIMARY KEY,sha TEXT,raw BLOB)')
        db.execute('CREATE TABLE IF NOT EXISTS salinity_sample(source_id TEXT,station TEXT,at TEXT,conductivity INTEGER,tide TEXT)')
        sha = hashlib.sha256(raw).hexdigest()
        old = db.execute('SELECT sha FROM salinity_source WHERE id=?', (doc['id'],)).fetchone()
        if old:
            if old['sha'] != sha:
                raise ValueError('logger_id_conflict')
            return {'id': doc['id'], 'replayed': True}
        db.execute('INSERT INTO salinity_source VALUES(?,?,?)', (doc['id'], sha, raw))
        for item in doc['samples']:
            if item['conductivity'] < 0 or item['tide'] not in ('flood','ebb'):
                raise ValueError('invalid_salinity_sample')
            db.execute('INSERT INTO salinity_sample VALUES(?,?,?,?,?)',
                       (doc['id'], item['station'], item['at'], item['conductivity'], item['tide']))
        return {'id': doc['id'], 'samples': len(doc['samples'])}


def summary(path, threshold=100):
    with closing(connect(path)) as db:
        db.execute('CREATE TABLE IF NOT EXISTS salinity_sample(source_id TEXT,station TEXT,at TEXT,conductivity INTEGER,tide TEXT)')
        rows = db.execute('SELECT station,MAX(conductivity) AS peak,COUNT(*) AS n FROM salinity_sample GROUP BY station ORDER BY station')
        return [{'station': r['station'], 'peak': r['peak'], 'count': r['n'],
                 'status': 'alert' if r['peak'] > threshold else 'normal'} for r in rows]
