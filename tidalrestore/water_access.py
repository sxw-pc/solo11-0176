"""Downstream access decision derived from committed salinity publications.

A station is only allowed to be ``open`` when it has publicly committed
samples and no staging group or sealed conflict is still waiting on that
station.  A missing piece, an unsigned revision, a pending calibration or a
sealed packet conflict therefore keeps the gate closed (``pending``) instead
of letting a partial peak look like a clean low reading.

``as_of`` reproduces a historical decision from the publication intervals that
were in force at that instant, after a signed revision has superseded an
earlier answer.
"""
import json
from contextlib import closing
from .salinity import summary, pending, staging_stations, connect  # noqa: F401


def _sealed_stations(path):
    """Stations blocked by a sealed conflict on a group that is still staging.

    A conflict recorded against a group that has since been revoked or
    committed is history, not an open question; it must not hold the gate.
    """
    stations = set()
    with closing(connect(path)) as db:
        for row in db.execute(
                'SELECT c.raw FROM salinity_conflict c JOIN salinity_manifest m '
                "ON c.object_key=m.manifest_key WHERE m.status='staging'"):
            try:
                doc = json.loads(row['raw']) if row['raw'] else {}
            except (ValueError, TypeError):
                continue
            for sample in doc.get('samples') or []:
                if isinstance(sample, dict) and sample.get('station'):
                    stations.add(sample['station'])
            for piece in doc.get('pieces') or []:
                for sample in piece.get('samples') or []:
                    if isinstance(sample, dict) and sample.get('station'):
                        stations.add(sample['station'])
    return stations


def access_report(path, threshold=100, as_of=None):
    peaks = summary(path, threshold=threshold, as_of=as_of)
    blocking = set(staging_stations(path)) if as_of is None else set()
    blocking |= _sealed_stations(path) if as_of is None else set()
    report = []
    seen = set()
    for row in peaks:
        station = row['station']
        seen.add(station)
        if row['status'] == 'alert':
            access, basis = 'closed', 'alert'
        elif station in blocking:
            access, basis = 'closed', 'pending'
        elif row['count'] == 0:
            access, basis = 'closed', 'no_publication'
        else:
            access, basis = 'open', 'normal'
        report.append({'station': station, 'access': access, 'basis_peak': row['peak'],
                       'basis': basis, 'count': row['count']})
    # A staging group for a station with no committed publication yet is an
    # explicit closed row: absence of a peak is not permission to open.
    for station in sorted(blocking - seen):
        report.append({'station': station, 'access': 'closed', 'basis_peak': None,
                       'basis': 'pending', 'count': 0})
    if not report and as_of is None and (staging_stations(path) or pending(path)['staging']):
        report.append({'station': '*', 'access': 'closed', 'basis_peak': None,
                       'basis': 'pending', 'count': 0})
    report.sort(key=lambda r: r['station'])
    return report


def access_for(path, station, threshold=100, as_of=None):
    for row in access_report(path, threshold=threshold, as_of=as_of):
        if row['station'] == station:
            return row
    # No committed row at all.  Report the gate as pending (not a clean "no
    # publication") when any live capture material — even a manifest whose
    # station is not yet known — is waiting to be verified.
    waiting = as_of is None and (staging_stations(path) or pending(path)['staging'])
    basis = 'pending' if waiting or station in _sealed_stations(path) else 'no_publication'
    return {'station': station, 'access': 'closed', 'basis_peak': None,
            'basis': basis, 'count': 0}
