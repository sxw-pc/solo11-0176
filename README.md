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

## 离线冷档与法定保全

已结算季节的原始 JSON 可在两条互斥的离线授权路线之间处置，全程不接网络：

- **路线 A（销毁许可 → 只读冷档）**：业主与独立档案员共同签发的 `destruction_permit` 允许把原件字节从热库 `evidence.raw` 迁入只读内容寻址 tar 包（`packages/pkg-<摘要>.tar`，权限 0444），数据库保留不可变的 sha/seq 身份与冷档指针，另写 `index.json` 恢复索引。`audit --archive-root` 先验整包摘要、再验各源 SHA，冷档在线时仍为 `matched`，并可按 seq 还原原件；热库缩减字节数可核。
- **路线 B（诉讼保全令 → 留在热库）**：`legal_hold` 点名的原件必须保持热库即时可读，归档时被拒绝并在 `archive_denial` 留下法定依据（法庭令号）；其余未受保全原件可正常归档。解除保全后方可迁出。

三处分发位置（包文件、数据库冷档指针、磁盘恢复索引）按两阶段提交收敛：阶段一只落包与索引、绝不动热库字节，可重试/回滚；阶段二在单个 SQLite 事务内重核资格、写指针并清空热字节。断电后 `archive-recover` 回滚 staging 批次、重试 sealed 批次、隔离损坏包，绝不把半份原件当作已归档。同一 source 跨多个地块使用时，只有所有用途均有有效许可才能移走；内容相同但 evidence id 不同的原件各保留独立 seq 与法律身份。篡改包时 `audit` 报具体 `source_id`（`package_tampered`/`member_sha_mismatch`），历史报告状态不因冷热迁移改变。

```powershell
python -m tidalrestore permit-record --db demo.sqlite --file fixtures/legal/permit-a.json
python -m tidalrestore hold-record   --db demo.sqlite --file fixtures/legal/hold-b.json
python -m tidalrestore archive-plan   --db demo.sqlite --permit-id PERMIT-A-P1 --archive-root ./vault
python -m tidalrestore archive-export --db demo.sqlite --permit-id PERMIT-A-P1 --archive-root ./vault
python -m tidalrestore audit          --db demo.sqlite --archive-root ./vault
python -m tidalrestore archive-check  --db demo.sqlite --archive-root ./vault
python -m tidalrestore archive-restore --db demo.sqlite --archive-root ./vault --all
python -m tidalrestore archive-status --db demo.sqlite
```

基线必须覆盖高、中、低潮位层；同层坐标冲突冻结地块并重开旧验收。独立物料批次必须是本土来源且数量和有效期足以覆盖施工格网；施工须在低潮窗口，格网重复不能重复计量。风暴按格网记损毁，独立复测按有效格网面积与高、中、低层存活率计算阶段分数；返养须在独立修复窗口进行，再经新复测才能恢复验收。迟到的较低复测保存新版返工决定，旧验收留在历史。复测员不得是施工方，样带坐标须与基线三层一致。所有身份和材料凭据为离线合成标签，没有外部签章验证。碳汇核算、无人机航测和外来物种长期治理不在首版范围。
