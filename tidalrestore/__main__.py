import argparse,json
from pathlib import Path
from .baseline import import_baseline
from .works import import_contract,import_work
from .survey import import_survey
from .material import import_material
from .review import import_recheck
from .store import report
from .audit import audit

def main():
    p=argparse.ArgumentParser();s=p.add_subparsers(dest='cmd',required=True)
    funcs={'baseline-import':import_baseline,'material-import':import_material,
           'contract-import':import_contract,'work-import':import_work,
           'survey-import':import_survey,'recheck-import':import_recheck,
           'works-import':import_work}
    for name in funcs:
        x=s.add_parser(name);x.add_argument('--db',required=True);x.add_argument('--file',required=True)
    x=s.add_parser('report');x.add_argument('--db',required=True)
    x=s.add_parser('audit');x.add_argument('--db',required=True)
    x=s.add_parser('demo');x.add_argument('--db',required=True)
    a=p.parse_args()
    if a.cmd=='demo':
        fixtures=Path(__file__).resolve().parent.parent/'fixtures'
        for fn,name in [(import_baseline,'baseline-ok.json'),(import_material,'material-ok.json'),
                        (import_contract,'contract-ok.json'),(import_work,'work-ok.json'),
                        (import_survey,'survey-ok.json')]:
            fn(a.db,fixtures/name)
        result=report(a.db)
    elif a.cmd in funcs:result=funcs[a.cmd](a.db,a.file)
    elif a.cmd=='audit':result=audit(a.db)
    else:result=report(a.db)
    print(json.dumps(result,ensure_ascii=False,indent=2))
if __name__=='__main__':main()
