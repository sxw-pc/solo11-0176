# 潮间带修复验收采证台

离线保存修复地块三层潮位基线、合同与本土种苗施工、独立生境复测，裁定阶段验收与返养。全部数据均为合成 fixture，不接真实传感器或监管系统。

```powershell
python -m pip install . --no-deps --no-build-isolation
python -m tidalrestore baseline-import --db demo.sqlite --file fixtures/baseline-ok.json
python -m tidalrestore contract-import --db demo.sqlite --file fixtures/contract-ok.json
python -m tidalrestore work-import --db demo.sqlite --file fixtures/work-ok.json
python -m tidalrestore survey-import --db demo.sqlite --file fixtures/survey-ok.json
python -m tidalrestore audit --db demo.sqlite
python -m pytest -q
```

基线必须覆盖高、中、低潮位层；施工材料必须是本土物种且在低潮施工窗口；复测员不得是施工方。验收使用三层加权存活率，风暴后损毁会把旧验收转返养并保留历史。原始证据字节、摘要与各账决定均留在 SQLite，`audit` 从原始字节重放当前状态。碳汇核算、无人机航测和外来物种长期治理不在首版范围。
