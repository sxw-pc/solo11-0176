import argparse,json
from pathlib import Path
from .baseline import import_baseline
from .works import import_contract,import_work
from .survey import import_survey
from .material import import_material
from .store import report
from .audit import audit
from . import archive as ar

def main():
    p=argparse.ArgumentParser();s=p.add_subparsers(dest='cmd',required=True)
    funcs={'baseline-import':import_baseline,'material-import':import_material,'contract-import':import_contract,'work-import':import_work,'survey-import':import_survey}
    for name in funcs:
        x=s.add_parser(name);x.add_argument('--db',required=True);x.add_argument('--file',required=True)
    x=s.add_parser('report');x.add_argument('--db',required=True)
    x=s.add_parser('audit');x.add_argument('--db',required=True);x.add_argument('--archive-root')
    x=s.add_parser('demo');x.add_argument('--db',required=True)
    x=s.add_parser('works-import');x.add_argument('--db',required=True);x.add_argument('--file',required=True)
    # Offline legal preservation and cold archive.
    x=s.add_parser('permit-record');x.add_argument('--db',required=True);x.add_argument('--file',required=True)
    x=s.add_parser('permit-revoke');x.add_argument('--db',required=True);x.add_argument('--permit-id',required=True);x.add_argument('--reason',required=True)
    x=s.add_parser('hold-record');x.add_argument('--db',required=True);x.add_argument('--file',required=True)
    x=s.add_parser('hold-lift');x.add_argument('--db',required=True);x.add_argument('--hold-id',required=True)
    x=s.add_parser('archive-plan');x.add_argument('--db',required=True);x.add_argument('--permit-id',required=True);x.add_argument('--archive-root',required=True)
    x=s.add_parser('archive-export');x.add_argument('--db',required=True);x.add_argument('--permit-id',required=True);x.add_argument('--archive-root',required=True);x.add_argument('--fault')
    x=s.add_parser('archive-recover');x.add_argument('--db',required=True);x.add_argument('--archive-root',required=True)
    x=s.add_parser('archive-restore');x.add_argument('--db',required=True);x.add_argument('--archive-root');x.add_argument('--seq',type=int);x.add_argument('--evidence-id');x.add_argument('--all',dest='all_',action='store_true');x.add_argument('--out')
    x=s.add_parser('archive-check');x.add_argument('--db',required=True);x.add_argument('--archive-root')
    x=s.add_parser('archive-status');x.add_argument('--db',required=True);x.add_argument('--archive-root')
    a=p.parse_args()
    if a.cmd=='demo':
        fixtures=Path(__file__).resolve().parent.parent/'fixtures'
        for fn,name in [(import_baseline,'baseline-ok.json'),(import_material,'material-ok.json'),
                        (import_contract,'contract-ok.json'),(import_work,'work-ok.json'),(import_survey,'survey-ok.json')]:
            fn(a.db,fixtures/name)
        result=report(a.db)
    elif a.cmd=='works-import':result=import_work(a.db,a.file)
    elif a.cmd in funcs:result=funcs[a.cmd](a.db,a.file)
    elif a.cmd=='audit':result=audit(a.db,a.archive_root)
    elif a.cmd=='permit-record':result=ar.record_permit(a.db,a.file)
    elif a.cmd=='permit-revoke':result=ar.revoke_permit(a.db,a.permit_id,a.reason)
    elif a.cmd=='hold-record':result=ar.record_hold(a.db,a.file)
    elif a.cmd=='hold-lift':result=ar.lift_hold(a.db,a.hold_id)
    elif a.cmd=='archive-plan':result=ar.plan_archive(a.db,a.permit_id,a.archive_root)
    elif a.cmd=='archive-export':result=ar.export_archive(a.db,a.permit_id,a.archive_root,fault=a.fault)
    elif a.cmd=='archive-recover':result=ar.recover(a.db,a.archive_root)
    elif a.cmd=='archive-restore':result=ar.restore(a.db,a.archive_root,seq=a.seq,evidence_id=a.evidence_id,all_=a.all_,out=a.out)
    elif a.cmd=='archive-check':result=ar.consistency_check(a.db,a.archive_root)
    elif a.cmd=='archive-status':result=ar.archive_status(a.db,a.archive_root)
    else:result=report(a.db)
    print(json.dumps(result,ensure_ascii=False,indent=2))
if __name__=='__main__':main()
