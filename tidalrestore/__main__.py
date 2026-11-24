import argparse,json
from pathlib import Path
from .baseline import import_baseline
from .works import import_contract,import_work
from .survey import import_survey
from .material import import_material
from .salinity import (import_logger,summary as salinity_summary,pending as salinity_pending,
                       recover as salinity_recover,history as salinity_history,group_state)
from .water_access import access_report as salinity_access
from .store import report
from .audit import audit

def main():
    p=argparse.ArgumentParser();s=p.add_subparsers(dest='cmd',required=True)
    funcs={'baseline-import':import_baseline,'material-import':import_material,'contract-import':import_contract,'work-import':import_work,'survey-import':import_survey}
    for name in funcs:
        x=s.add_parser(name);x.add_argument('--db',required=True);x.add_argument('--file',required=True)
    x=s.add_parser('report');x.add_argument('--db',required=True)
    x=s.add_parser('audit');x.add_argument('--db',required=True)
    x=s.add_parser('demo');x.add_argument('--db',required=True)
    x=s.add_parser('works-import');x.add_argument('--db',required=True);x.add_argument('--file',required=True)

    # Salinity capture protocol: one command per packet, identical semantics to
    # receive_manifest / receive_piece.  A partial group reports status=staging.
    x=s.add_parser('salinity-import');x.add_argument('--db',required=True);x.add_argument('--file',required=True)
    x.add_argument('--collector',default=None)
    x=s.add_parser('salinity-summary');x.add_argument('--db',required=True)
    x.add_argument('--threshold',type=int,default=100);x.add_argument('--as-of',dest='as_of',default=None)
    x=s.add_parser('salinity-access');x.add_argument('--db',required=True)
    x.add_argument('--threshold',type=int,default=100);x.add_argument('--as-of',dest='as_of',default=None)
    x=s.add_parser('salinity-pending');x.add_argument('--db',required=True)
    x=s.add_parser('salinity-recover');x.add_argument('--db',required=True)
    x.add_argument('--collector',default=None)
    x=s.add_parser('salinity-history');x.add_argument('--db',required=True)
    x=s.add_parser('salinity-group');x.add_argument('--db',required=True);x.add_argument('--manifest-key',required=True)

    a=p.parse_args()
    collector=dict(collector=a.collector) if getattr(a,'collector',None) else {}
    if a.cmd=='demo':
        fixtures=Path(__file__).resolve().parent.parent/'fixtures'
        for fn,name in [(import_baseline,'baseline-ok.json'),(import_material,'material-ok.json'),
                        (import_contract,'contract-ok.json'),(import_work,'work-ok.json'),(import_survey,'survey-ok.json')]:
            fn(a.db,fixtures/name)
        result=report(a.db)
    elif a.cmd=='works-import':result=import_work(a.db,a.file)
    elif a.cmd in funcs:result=funcs[a.cmd](a.db,a.file)
    elif a.cmd=='audit':result=audit(a.db)
    elif a.cmd=='salinity-import':result=import_logger(a.db,a.file,**collector)
    elif a.cmd=='salinity-summary':result=salinity_summary(a.db,threshold=a.threshold,as_of=a.as_of)
    elif a.cmd=='salinity-access':result=salinity_access(a.db,threshold=a.threshold,as_of=a.as_of)
    elif a.cmd=='salinity-pending':result=salinity_pending(a.db)
    elif a.cmd=='salinity-recover':result=salinity_recover(a.db,**collector)
    elif a.cmd=='salinity-history':result=salinity_history(a.db)
    elif a.cmd=='salinity-group':result=group_state(a.db,a.manifest_key)
    else:result=report(a.db)
    print(json.dumps(result,ensure_ascii=False,indent=2))
if __name__=='__main__':main()
