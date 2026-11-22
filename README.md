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

## 离线招投标采录与授标

到货之前先决定哪些供应商拿到可执行合同；中标只产生授标投影，绝不预写 `material_batch` 余额，真实到货仍须独立过 `material-import` 的本土/本地门槛。

```powershell
python -m tidalrestore tender-capture --db demo.sqlite --file fixtures/tender-bundles.json
python -m tidalrestore tender-award  --db demo.sqlite --file award-lowest.json
python -m tidalrestore tender-withdrawal --db demo.sqlite --file withdrawal.json
python -m tidalrestore tender-reopen --db demo.sqlite --file re-award.json
```

- 分页乱序、重复快照（同页多快照号或快照链不从 1 开始）、缺页或采录水位早于提交截止，一律显式标为 `capture_not_closed` 并给出原因；未下载的页不会被当作无报价，未闭合采录不得授标。
- 初审排除非本土物种或非本地来源以及撤回版本，原件与拒因（`non_native_species`、`non_local_origin`、`withdrawn_version` 等）整版保留在 `tender_bid`。
- 联包原子成交：要么同时覆盖 P1/P2 的指定数量，要么整包落选；逐地块最低单价贪心无法拆包。同一 `truck_id` 在重叠潮窗内不能被两个中标包重复占用。
- 两条事前公示规则对同一完整快照得出不同合同：`lowest_total` 取 A 联包 700 元单苗圃交付；`provenance_diversity` 要求至少两座独立苗圃，取 B+C 共 800 元。
- 授标行带版本号并冻结来源、费用、交货承诺与撤回违约责任；发布后供应商撤回只把旧中标项置为 `breached` 并开 `tender_gap` 重新采购待办，不回写旧中标者，违约包也不得再投剩余授标。联包声称只交部分地块不构成部分履约，两块地都记缺口。
- 重新采购是按当前快照价格重算的可核新授标；无可行组合时缺口保持 `open`，不按旧价补单。
- 到货凭证带 `award_id/package_id` 时会与中标原件核对物种与来源；不符则 `material_arrival.matched=0` 且批次仍 `quarantined`，`import_work` 的既有门槛不因已授标而放松。

基线必须覆盖高、中、低潮位层；同层坐标冲突冻结地块并重开旧验收。独立物料批次必须是本土来源且数量和有效期足以覆盖施工格网；施工须在低潮窗口，格网重复不能重复计量。风暴按格网记损毁，独立复测按有效格网面积与高、中、低层存活率计算阶段分数；返养须在独立修复窗口进行，再经新复测才能恢复验收。迟到的较低复测保存新版返工决定，旧验收留在历史。复测员不得是施工方，样带坐标须与基线三层一致。所有身份和材料凭据为离线合成标签，没有外部签章验证。碳汇核算、无人机航测和外来物种长期治理不在首版范围。
