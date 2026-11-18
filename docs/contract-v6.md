# 离线冷链到货放行分析 v6（schema_version=6）

仓配人员在发货后需要判断到货批次能否进入可售库存。v1 allocation 只是每个
订单-批次**可装运数量的上限**，不代表实际装箱、签收或运输温度。本模式是一次性、
只读的离线分析：把装箱行、到货数量与运输温度观测关联到**未修改的 v1 固定
allocation** 所确定的订单与批次，逐订单-批次输出已到货数量中可放行、须隔离、
证据不足三类裁决以及未装箱、短装对账。它不写库存、账本、订单或价格，不按温控
结果重新分配订单，也不读取 v2 SQLite 账本、v3 路线、v4 促销或 v5 召回图。仅用
Python 标准库。

- **入口**：`perishable-coldchain --source JSON --output JSON`（也可
  `python3 -m perishable_fulfillment.coldchain`）。
- **Python API**：`perishable_fulfillment.coldchain.compile_report(data, source_sha256)`，
  输入为已解析的 v6 源对象与源字节 SHA-256，返回完整报告对象；
  `perishable_fulfillment.coldchain.run(source_path)` 从文件读取并计算摘要。
- 代码：`src/perishable_fulfillment/coldchain.py`；样例：
  `fixtures/coldchain_input.json`；测试：`tests/test_coldchain.py`。
- v1 至 v5 的输入、输出与行为保持不变，本模式只在旁新增，通过 `model.analyze`
  取得固定 allocation，不复制或修改其优先级、FEFO、held、到期日或短缺语义。

## 1. 输入契约

顶层**严格恰有**四个字段 `schema_version、fulfillment、policies、packages`，
拒绝未知/缺失字段、重复 JSON 键、`NaN/Infinity`、布尔/数字字符串，标识符沿用
`common.ident`（非空、无首尾空白、无控制字符、≤128 字符）。JSON 字节上限沿用
仓库统一的 **8 MB**（`common.read`）。

### 1.1 规模上限

| 数组 | 上限 |
| --- | --- |
| `policies` | 128 |
| `packages` | 256 |
| 每包 `readings` | 4096 |
| 每包 `lines` | 512 |

数量整数范围 `0..10^9`；两个分钟上限为真正的非负整数，范围 `0..10^9`。

### 1.2 schema_version 与 fulfillment

- `schema_version` 必须是真正的整数 `6`（拒绝布尔、浮点、字符串）。
- `fulfillment` 原样使用现有 v1 的 `{as_of, lots, orders}` 输入及其严格校验；
  本模式直接调用 `model.analyze` 取得固定 allocation。

### 1.3 policies（按 SKU 唯一）

每项严格为 `{sku, min_c, max_c, max_gap_minutes, max_excursion_minutes}`：

- `sku` 唯一，且策略集合必须恰好覆盖 fulfillment 中每个 lot SKU 与 order SKU
  的并集——遗漏或出现无对应 SKU 的策略均为非法输入。
- `min_c、max_c` 为有限 JSON 数字（整数或浮点，拒绝布尔与非有限数），且
  `min_c < max_c`。
- `max_gap_minutes、max_excursion_minutes` 为真正的非负整数。
- 每个 SKU 使用自己的温度区间与曝光时限；多个 SKU 共箱时各自独立裁决。

### 1.4 packages（包裹、读数、装箱行）

每项严格为 `{id, transit_start, transit_end, readings, lines}`：

- `id` 在包裹间唯一。
- `transit_start、transit_end` 为**带时区**的 ISO-8601 时间戳（无时区偏移的
  字符串被拒绝），`start < end`，内部统一归一到 UTC 处理与输出。
- `readings` 每项严格 `{at, temp_c}`：`at` 为带时区 ISO-8601 时间戳，严格递增
  且位于运输窗口**闭区间**内；`temp_c` 为有限 JSON 数字。每包至少一条读数。
- `lines` 每项严格 `{order, lot, packed, received}`：包内 `(order, lot)` 唯一；
  `packed、received` 为真正非负整数且 `0 <= received <= packed <= 10^9`。
- 包裹、策略、行均拒绝缺失/未知字段、重复标识、错误类型与越界数量。

## 2. 固定 allocation 与数量对账

- 以 v1 结果中每个**正** allocation `(order, lot, quantity)` 为对账单元。
- 所有包裹行引用的 `order、lot` 必须存在，lot SKU 必须与 order SKU 相同；
  不得存在没有对应正 allocation 的装箱行（v1 零分配、缺货订单不可装箱）。
- 同一 `(order, lot)` 全部包裹行的 `packed` 之和不得超过其固定 allocation；
  超出即非法输入，退出 2，不发布报告。
- 每行 `packed - received` 为 `short_shipped`，只有 `received` 进入温控裁决；
  未到货的短装数量不受温控状态影响。
- 每个正 allocation 完整、互斥地归入五个去向，并严格满足

  `allocation = unshipped + short_shipped + received_release + received_quarantine + received_unknown`

  其中 `unshipped` 是 allocation 中从未装箱的余量。零分配与原 v1 缺货在报告的
  v1 结果块与订单摘要中保留可见。
- 同一箱内多订单或多批次共享同一条温度轨迹，但每行依其 SKU 策略单独裁决，
  数量始终回写到原 `(order, lot)` 身份，行数量与追溯身份不合并、不丢失。

## 3. 曝光计算与状态语义

### 3.1 插值、缺口与边缘规则

- 每条读数代表该时刻的测量值。相邻读数间隔 **不超过** 该 SKU
  `max_gap_minutes` 时，温度按时间**线性变化**解释；间隔超过上限的整段为
  **未观测缺口**，不做任何插值，也不得静默当作温度合格。
- 运输开始至首条读数、末条读数至运输结束两个单侧边缘区段**不做线性外推**：
  边缘长度不超过 `max_gap_minutes` 时，以边界读数温度作为整段恒定温度；否则
  整段为未观测缺口。只有一条读数的包裹按同一规则分别处理两侧边缘。
- 间隔与边缘长度按**闭区间等于上限即合格**处理（`<= max_gap_minutes` 覆盖）。
- 温度恰等 `min_c` 或 `max_c` 属于合格温区，不计超温。

### 3.2 精确有理数几何（明确采用的计算结构）

实现明确采用**结构 A：逐相邻采样段精确裁切温区并累计有理数曝光，超限缺口作为
独立未覆盖区间**。另一种可行结构 B（先把读数规范化成不重叠 UTC 时间段，再按
每个 SKU 温区扫描）在测试中以完全独立的 oracle 重新实现并与结构 A 逐例核对，
两者遵守同一插值、缺口与温区语义。

结构 A 的正确性义务：

1. **避免边界重复计时**：覆盖段只在零时长的读数瞬间相接，曝光按互不重叠的
   闭开区间累计，边缘段一律裁在运输窗口内、绝不外推到窗口之外；报告输出的
   `covered_minutes + gap_minutes = window_minutes`。
2. **缺口不插值**：超过 `max_gap_minutes` 的相邻间隔整体进入 gap 列表，不参与
   温区裁切；未观测时间既不算安全也不算确定超温。
3. **穿越时刻精确求解**：对线性段 `T(u)=t0+(t1-t0)u, u∈[0,1]`，穿越
   `min_c/max_c` 的参数 `u=(bound-t0)/(t1-t0)` 以 `fractions.Fraction` 精确
   表示，段内超温时长 = 段分钟数 × 超温参数长度；温度全程使用 `Fraction`
   （JSON 浮点取其精确二进制值），**不使用浮点时长累计，不做任何舍入后裁决**。
4. **多 SKU 不混用温限**：同一条包裹轨迹按每行 SKU 的独立策略重新评估，
   `(package, sku)` 级证据缓存只在同 SKU 行间共享。

### 3.3 行级裁决（只作用于 received 数量）

- 可证明的超温累计时长 **>= `max_excursion_minutes`**：该行全部 received 归
  `received_quarantine`，即使窗口内还存在未观测缺口（隔离优先）。
- 未达阈值但存在任何未覆盖/超限缺口：全部 received 归 `received_unknown`
  （证据不足），不得当作安全，也不得直接算作超温。
- 覆盖完整且可证明曝光小于时限：全部 received 归 `received_release`。
- `received = 0` 的行状态为 `no_receipt`，其 `packed` 全部进入
  `short_shipped`，不参与温控分类。
- 报告不输出污染概率，不把证据不足等同安全或确定超温。

## 4. 报告契约（schema_version=6）

成功输出顶层严格为 `schema_version、source_sha256、fulfillment、allocation、
policies、packages、line_verdicts、reconciliation、order_summary、totals`：

- `source_sha256`：输入源**原始字节**的 SHA-256 小写十六进制。
- `fulfillment`：`model.analyze(输入 fulfillment)` 的完整原 v1 结果。
- `allocation`：固定正 allocation 摘要 `{order, lot, quantity}`，按
  `(order, lot)` 排序，含全部正分配。
- `policies`：按 SKU 排序的策略回显（含输入温度数字与两个分钟上限）。
- `packages`：按 `id` 排序、时间归一 UTC、行按 `(order, lot)` 排序的包裹
  回显。
- `line_verdicts`：按 `(order, lot, package)` 稳定排序的逐行裁决，每行含
  `packed/received/short_shipped`、`disposition`
  （`release/quarantine/unknown/no_receipt`）、`reasons`、该行 SKU 的 `policy`
  与 `temperature_evidence`。
- `temperature_evidence`：精确曝光与覆盖/缺口可复核证据。所有时长为
  `{"numerator": 整数, "denominator": 正整数}` 的明确有理数对象（不出现浮点、
  不做十进制舍入）；`covered_segments` 逐段给出 UTC 起止、`interpolation`
  （`linear/constant_edge`）、两端温度与该段精确超温分钟；`gaps` 给出缺口 UTC
  起止与原因（`leading_edge_gap/inter_reading_gap/trailing_edge_gap`）。
- `reconciliation`：按 `(order, lot)` 排序的正 allocation 对账，五项去向满足
  上述恒等式。
- `order_summary`：覆盖全部 v1 订单（含零履约/缺货订单），保留 v1
  `requested/fulfilled/shortage` 并汇总本模式五类数量。
- `totals`：全局总量，内置两条守恒检查
  `allocation = unshipped + short_shipped + received_release + received_quarantine + received_unknown`
  与 `packed = short_shipped + 三类 received 之和`；另含包裹/行/读数计数。

除输入温度数字外，件数、分钟上限均为 JSON 整数；时长为有理数对象。无近似小数、
随机种子或随运行时间变化的字段。相同输入字节产生字节相同的报告；合法输入数组
（包裹、策略、lot、order、行）顺序变化不改变任何业务结果与稳定排序。

## 5. 失败、发布与安全

- 全部输入校验、温度分析与守恒检查成功后才发布结果；成功采用仓库现有原子发布
  （同目录临时文件 + `os.replace`）。
- 参数、JSON、引用、类型、范围、重复标识、越额装箱、守恒或写出错误均退出 2，
  stderr 给可理解错误，不产生半份报告，已存在报告保持不变。
- 输入与输出不得为同一路径或硬链接等价文件（含 `os.path.samefile` inode
  检查）；输出为符号链接时拒绝。
- 源为单一只读输入；分析过程不创建/修改任何 v2 库，不读取 v3/v4/v5 数据。
- 不额外抵御并发恶意文件系统替换，沿用仓库现有单操作者发布保证。

## 6. 演示与测试

```sh
python3 -m perishable_fulfillment.coldchain \
  --source fixtures/coldchain_input.json --output run/coldchain.json
PYTHONPATH=src python3 -m unittest tests.test_coldchain -v
```

`fixtures/coldchain_input.json` 是可手算的多包裹案例：P1 跨午夜三段累计超温
200 分钟（线性 40 + 恒定 120 + 线性 40）整箱隔离并含 1 件短装；P2 为 milk 与
yogurt 共箱、各自策略下完整覆盖放行；P3 恒定合格放行；P4 单读数且尾边缘超
gap，证据不足。

测试覆盖：跨午夜多段超温累计、曝光恰好等于阈值（隔离）与低一分钟（放行）、
温区端点恰等温限合格、超 `max_gap` 缺口优先标未知、已知曝光越阈时即使有缺口
仍隔离、运输窗口首尾边缘缺读数（含单读数包裹）、同箱多 SKU 不同温限独立裁决、
allocation 未装箱余量、短装与到货分开、部分到货、零到货行、越额装箱、坏引用、
SKU 不匹配、无正 allocation 装箱、各类非法字段/类型/数量关系、乱序确定性、
重复键与非有限数拒绝，以及输出失败保留旧报告、符号链接/同文件/硬链接拒绝。

独立 oracle 验收（`structure_b_oracle`）采用替代计算结构：先把包裹读数规范化
成不重叠 UTC 段（线性/恒定边缘/未观测缺口），再按 SKU 温区逐段以根分割中点法
扫描累计有理数曝光；它不 import 任何生产温度函数，不把生产计算再次调用当作
oracle，并与手算有理数样例（如 4→9°C / 90 分钟恰超温 18 分钟、5→-1°C / 60
分钟恰超温 30 分钟）逐值核对一致。
