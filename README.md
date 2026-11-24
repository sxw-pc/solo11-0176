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

基线必须覆盖高、中、低潮位层；同层坐标冲突冻结地块并重开旧验收。独立物料批次必须是本土来源且数量和有效期足以覆盖施工格网；施工须在低潮窗口，格网重复不能重复计量。风暴按格网记损毁，独立复测按有效格网面积与高、中、低层存活率计算阶段分数；返养须在独立修复窗口进行，再经新复测才能恢复验收。迟到的较低复测保存新版返工决定，旧验收留在历史。复测员不得是施工方，样带坐标须与基线三层一致。所有身份和材料凭据为离线合成标签，没有外部签章验证。碳汇核算、无人机航测和外来物种长期治理不在首版范围。

## 分片盐度采录（U 盘分段交接）

一次潮位录制是一个**组**。设备对总清单签名，清单列出设备、序号范围、片序、每片字节摘要和总摘要；片段可乱序到达、重传，或由两名采录员分别交接。只有全部片到齐、每片摘要与清单一致、清单与每片都通过仓内固定公钥（`tidalrestore/device_keys.py`，离线 mock，无外部 CA）验签、校准片时段覆盖全部样本、且所有片属于清单声明的同一设备时，该组才作为**不可拆分**的已提交来源进入公开摘要。缺片、缺总摘要、清单修订无撤销签名、跨设备片、同片序不同字节都保持组级待核（staging），**不会泄出局部峰值**。

```powershell
# 路线 A：三片到齐后 S1 峰值 120，取水口 closed
python -m tidalrestore salinity-import --db flow.sqlite --file fixtures/salinity-chunked-a/manifest.json
python -m tidalrestore salinity-import --db flow.sqlite --file fixtures/salinity-chunked-a/piece-1.json
python -m tidalrestore salinity-import --db flow.sqlite --file fixtures/salinity-chunked-a/piece-3.json
python -m tidalrestore salinity-access  --db flow.sqlite   # closed / pending
python -m tidalrestore salinity-import --db flow.sqlite --file fixtures/salinity-chunked-a/piece-2.json
python -m tidalrestore salinity-summary --db flow.sqlite   # S1 peak 120 alert
python -m tidalrestore salinity-access  --db flow.sqlite   # closed / alert

# 路线 B：第三片属于另一台设备被拒；设备端签名更正为两片后 S1 峰值 80，取水口 open
python -m tidalrestore salinity-import --db flow.sqlite --file fixtures/salinity-chunked-b-corrected/manifest.json
python -m tidalrestore salinity-import --db flow.sqlite --file fixtures/salinity-chunked-b-corrected/piece-1.json
python -m tidalrestore salinity-import --db flow.sqlite --file fixtures/salinity-chunked-b-corrected/piece-2.json
python -m tidalrestore salinity-history --db flow.sqlite   # 旧输出区间 closed + 新输出区间 active
python -m tidalrestore salinity-pending --db flow.sqlite
python -m tidalrestore salinity-recover --db flow.sqlite   # 崩溃重启后幂等收敛
python -m tidalrestore audit --db flow.sqlite
```

`salinity_source` 只有 `committed` / `superseded`（以及被外部更正后的 `closed` 区间）两种对外可见状态；staging 只存在于 `salinity_manifest` / `salinity_piece`。每次提交是一个 `BEGIN IMMEDIATE` 事务，因此并发采录员竞争同一清单时只有一个提交者。清单更正通过新的签名修订完成：旧清单、旧源字节和旧样本全部保留，旧公开区间写入 `effective_to`，`salinity-summary --as-of` 与 `salinity-access --as-of` 可回看当时的对外结果；绝不 `UPDATE` 源字节或批量 `DELETE`。同片序不同字节、跨设备片和摘要不符封存进 `salinity_conflict` 并阻止该组提交。

四个崩溃注入点（`after_manifest`、`after_piece`、`before_commit`、`after_commit`）由测试通过 `import_logger(..., fault={...})` 触发；重启后再次投递同一包或运行 `salinity-recover`，结果与一次完成导入的 source/sample/count、公开摘要和 `audit` 完全一致。审计按收到的原始包与封存冲突重放整条协议账；旧版单文件 `salinity-logger-ok.json` 仍作为 `file:<id>` 来源兼容，老库中的旧表会在连接时原位迁移且不丢样本。

