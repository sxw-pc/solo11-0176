"""A small downstream access decision based on the logger summary."""
from .salinity import summary


def access_report(path):
    return [{'station': r['station'], 'access': 'closed' if r['status']=='alert' else 'open',
             'basis_peak': r['peak']} for r in summary(path)]
