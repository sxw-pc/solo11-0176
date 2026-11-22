"""Capture offline cross-shore elevation profiles without interpreting erosion."""
import json
from contextlib import closing
from .store import connect,evidence

def import_profile(path,file):
    with closing(connect(path)) as db,db:
        doc,fresh=evidence(db,file,'shore_profile')
        if not fresh:return {'id':doc['id'],'replayed':True}
        if not db.execute('SELECT 1 FROM plot WHERE id=?',(doc['plot_id'],)).fetchone():
            raise ValueError('unknown_plot')
        pts=doc.get('points',[])
        if len(pts)<2 or any(not isinstance(p.get('distance_m'),(int,float)) or not isinstance(p.get('elevation_cm'),(int,float)) for p in pts):
            raise ValueError('profile_points_invalid')
        if any(pts[i]['distance_m']>=pts[i+1]['distance_m'] for i in range(len(pts)-1)):
            raise ValueError('profile_order_invalid')
        db.execute('INSERT INTO shore_profile VALUES(?,?,?,?,?,?,?)',
                   (doc['profile_id'],doc['plot_id'],doc['captured_at'],doc['device'],doc['water_level_cm'],json.dumps(pts,separators=(',',':')),doc['id']))
        return {'profile_id':doc['profile_id'],'status':'captured','point_count':len(pts)}
