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

泥沙预算：`shore-bundle-import` 一次导入潮位记录、两期声呐剖面与预算请求（`fixtures/shore-two-periods.json`，需先建 P1/P2 基线）。剖面原件只存水位与点列；预算把两期剖面统一到同一潮位基准、按沿岸覆盖段配准加权，并把同一设备同一航次的系统误差当作共同区间变量传播，不逐点独立相加。共边界的沉积以单一转移区间在两侧各记一次，未测到的外海出口只列未知通量与补测位置，不为平账归零。互斥的现场核证路线在补测到达前同时保留为可行预算，`shore-verification-import` 确认其一后另一条作废，同一份缺口不能既记区内转移又记外海流失。派生的体积区间、转移归属与外购量存于 `budget_*` 表并随证据回放重建；篡改原始点列或派生预算会在 `audit` 的 `tampered_evidence`、`replay_errors` 或 `differences` 中定位。声呐派生量不覆盖基线、复测与验收的业务裁决。

```powershell
python -m tidalrestore shore-bundle-import --db demo.sqlite --file fixtures/shore-two-periods.json
python -m tidalrestore shore-verification-import --db demo.sqlite --file verification.json
```

基线必须覆盖高、中、低潮位层；同层坐标冲突冻结地块并重开旧验收。独立物料批次必须是本土来源且数量和有效期足以覆盖施工格网；施工须在低潮窗口，格网重复不能重复计量。风暴按格网记损毁，独立复测按有效格网面积与高、中、低层存活率计算阶段分数；返养须在独立修复窗口进行，再经新复测才能恢复验收。迟到的较低复测保存新版返工决定，旧验收留在历史。复测员不得是施工方，样带坐标须与基线三层一致。所有身份和材料凭据为离线合成标签，没有外部签章验证。碳汇核算、无人机航测和外来物种长期治理不在首版范围。
