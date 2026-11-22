# 潮间带修复验收采证台

离线保存修复地块三层潮位基线、合同与本土种苗施工、独立生境复测，裁定阶段验收与返养。全部数据均为合成 fixture，不接真实传感器或监管系统。

```powershell
python -m pip install . --no-deps --no-build-isolation
python -m tidalrestore baseline-import --db demo.sqlite --file fixtures/baseline-ok.json
python -m tidalrestore material-import --db demo.sqlite --file fixtures/material-ok.json
python -m tidalrestore contract-import --db demo.sqlite --file fixtures/contract-ok.json
python -m tidalrestore work-import --db demo.sqlite --file fixtures/work-ok.json
python -m tidalrestore survey-import --db demo.sqlite --file fixtures/survey-ok.json
python -m tidalrestore audit --db demo.sqlite
python -m pytest -q
```

单命令演示：`python -m tidalrestore demo --db fresh.sqlite`。各进口都保留原文件字节和 SHA；`audit` 在新数据库按证据顺序重建地块、材料、施工格网、复测与版本化验收账。

## 环境 DNA 泳道联合归属

固定副本 `edna-import` 只采集 `site、batch_id、barcode、read_count、blank_reads`，从不把读数当成物种定居结论。`lane-import` 导入整条测序泳道（两个地块样本、提取空白、PCR 空白、阳性对照、近缘条码与实测串读上界矩阵），每次样本加入/退出或参考序列更正都重新求解**联合可行分配集合**，而不是逐行改标签：

```powershell
python -m tidalrestore lane-import --db demo.sqlite --file fixtures/edna-lane.json
python -m tidalrestore conservative-plan --db demo.sqlite
python -m tidalrestore probabilistic-plan --db demo.sqlite
python -m tidalrestore naive-plan --db demo.sqlite
```

归属用有界整数流（纯标准库 Dinic）保证读数守恒：每条读数都能回到原始样本、条码、泳道与参考序列版本；同一来源的串读总量在所有目的孔和所有条码间共享一个上界，空白对照联合约束背景，不能对每个样本独立减固定空白数。不可辨识的近缘条码读数留在候选物种集合，计为未归属读数，绝不以最高匹配分宣称入侵种。

- **保守确证途径**只发布在所有可行分配中恒为真的结论；本批数据 P1 的 36 条入侵条码可全部由串读/背景解释，给“未确证、P2 优先核查”，36 不等于阳性，并列出必须复测的泳道。
- **概率巡查途径**保留整个可行集合，按经独立阳性对照校准的概率把 P1 列为待验证热点（概率严格小于 1，永不写成确证），P1、P2 同时进入巡查。
- `naive-plan` 是刻意保留的错误对照：逐地块减固定空白会让两个地块都显示确证阳性，违背单泳道串读守恒。

两途径输入与硬约束完全相同，现场出勤地点数、复测泳道和对外确定性不同；任何途径都不会把 P2 的读数同时静默算作 P1 的本地来源。原始读数或归属投影被改写后，`audit` 重放会定位到具体分歧表。

基线必须覆盖高、中、低潮位层；同层坐标冲突冻结地块并重开旧验收。独立物料批次必须是本土来源且数量和有效期足以覆盖施工格网；施工须在低潮窗口，格网重复不能重复计量。风暴按格网记损毁，独立复测按有效格网面积与高、中、低层存活率计算阶段分数；返养须在独立修复窗口进行，再经新复测才能恢复验收。迟到的较低复测保存新版返工决定，旧验收留在历史。复测员不得是施工方，样带坐标须与基线三层一致。所有身份和材料凭据为离线合成标签，没有外部签章验证。碳汇核算、无人机航测和外来物种长期治理不在首版范围。
