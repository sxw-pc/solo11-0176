import argparse,json
from .baseline import import_baseline
from .works import import_contract,import_work
from .survey import import_survey
from .store import report
from .audit import audit
def main():
    p=argparse.ArgumentParser();s=p.add_subparsers(dest='cmd',required=True)
    funcs={'baseline-import':import_baseline,'contract-import':import_contract,'work-import':import_work,'survey-import':import_survey}
    for name in funcs:
        x=s.add_parser(name);x.add_argument('--db',required=True);x.add_argument('--file',required=True)
    x=s.add_parser('report');x.add_argument('--db',required=True)
    x=s.add_parser('audit');x.add_argument('--db',required=True)
    a=p.parse_args();print(json.dumps(funcs[a.cmd](a.db,a.file) if a.cmd in funcs else (audit(a.db) if a.cmd=='audit' else report(a.db)),ensure_ascii=False,indent=2))
if __name__=='__main__':main()
