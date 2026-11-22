"""Independent native planting batch provenance and usable quantity."""
from contextlib import closing
from .store import connect, evidence, log

def import_material(path, file):
    with closing(connect(path)) as db, db:
        doc, fresh = evidence(db, file, 'material')
        if not fresh:
            return {'id': doc['id'], 'replayed': True}
        if not doc['batch_id'] or doc['quantity'] <= 0 or not doc['origin'] or not doc['valid_until']:
            raise ValueError('invalid_material_batch')
        status = 'approved' if doc['species'] in ('native-grass', 'native-shell') and doc['origin'] == 'local' else 'quarantined'
        db.execute('INSERT INTO material_batch VALUES(?,?,?,?,?,?,?)',
                   (doc['batch_id'], doc['species'], doc['origin'], doc['quantity'], doc['valid_until'], status, doc['id']))
        # An arrival may reference an awarded package, but the award never
        # overrides independent screening: a mismatched arrival is linked as
        # unmatched and remains quarantined, so import_work still rejects it.
        mismatch = None
        if doc.get('award_id'):
            item = db.execute(
                'SELECT * FROM tender_award_item WHERE award_id=? AND package_id=?',
                (doc['award_id'], doc.get('package_id', ''))).fetchone()
            if not item:
                raise ValueError('unknown_awarded_package')
            bid = db.execute(
                'SELECT species,origin FROM tender_bid WHERE bid_id=? AND version=?',
                (item['bid_id'], item['version'])).fetchone()
            reasons = []
            if bid['species'] != doc['species']:
                reasons.append('species_mismatch')
            if bid['origin'] != doc['origin']:
                reasons.append('origin_mismatch')
            mismatch = '+'.join(reasons) if reasons else None
        db.execute('INSERT INTO material_arrival VALUES(?,?,?,?,?,?,?,?,?)',
                   (doc['batch_id'], doc.get('award_id'), doc.get('package_id'),
                    doc['species'], doc['origin'], doc['quantity'],
                    1 if doc.get('award_id') and mismatch is None and status == 'approved' else 0,
                    mismatch, doc['id']))
        log(db, doc['batch_id'], status, 'native_origin' if status == 'approved' else 'non_native_origin', doc['id'])
        return {'batch_id': doc['batch_id'], 'status': status,
                'award_matched': bool(doc.get('award_id')) and mismatch is None and status == 'approved'}
