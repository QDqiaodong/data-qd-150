# 离线每日需求训练面板 v8（schema_version=8）

本模式是一次性、只读的**数据集构造器**：把历史 SKU 日销量（POS 观测）与
门店在库覆盖见证整理成可复现的滑窗训练/测试样本。它**不预测需求、不优化
促销售价、不做库存分配或履约计划**，不写任何业务账本，也不修改 v1-v6 的
输入、输出与入口行为。仅用 Python 标准库。

- **入口**：`perishable-demand-panel --source INPUT.json --output REPORT.json`
  （也可 `python3 -m perishable_fulfillment.demand_panel`）。
- **Python API**：
  `perishable_fulfillment.demand_panel.compile_report(data, source_sha256)`，
  输入为已解析的 v8 源对象与源字节 SHA-256，返回完整报告对象；
  `perishable_fulfillment.demand_panel.run(source_path)` 从文件读取并计算。
- 代码：`src/perishable_fulfillment/demand_panel.py`；样例：
  `fixtures/demand_panel_input.json`；测试：`tests/test_demand_panel.py`。
- v6 报告只按其整数 `schema_version=6` 与 `line_verdicts` 消费，不重算运输
  温度、不重新裁决到货、不产生装箱对账；v1-v6 其余字段与模式完全不参与。

## 1. 输入契约

顶层**严格恰有** 11 个字段：`schema_version、calendar_start、cutoff_date、
split_date、evidence_as_of、lookback_days、horizon_days、
censored_target_policy、skus、observations、v6_reports、shelf_witnesses`
（`schema_version` 必须是真正的整数 `8`，拒绝布尔、浮点、字符串）。拒绝
未知/缺失字段、重复 JSON 键、`NaN/Infinity`、布尔/数字字符串。JSON 字节
上限沿用仓库统一的 **8 MB**（`common.read`）。

### 1.1 日历与证据时点

- 所有门店日界线统一为 **UTC 午夜**。历史日历从 `calendar_start` 到
  `cutoff_date`，含首尾，最多 366 天；日历末日必须有可表示的下一日。
- `split_date` 必须在日历内部且严格早于 `cutoff_date`；日期一律规范
  `YYYY-MM-DD`（反序列化后再序列化必须一致）。
- `evidence_as_of` 是另行指定的 UTC 整分钟截点
  `YYYY-MM-DDTHH:MM:00Z`，允许早于或晚于日历末日；带偏移量、带秒余数、
  非整分钟的写法一律拒绝。**在截点精确可用的事实计入**。
- 一天的证据查询上界为下一天 00:00Z。POS 行在其业务日结束（次日
  00:00Z）时才可用，早于该时点查询按缺失处理；晚到证明可使过去的目标日
  取得资格，但不能回填证据到达前锚点的历史特征。

### 1.2 窗口参数与策略

- `lookback_days` 为 1..90 的真正整数；`horizon_days` 为 1..28 的真正
  整数（布尔、浮点、字符串一律拒绝）。
- `censored_target_policy` 恰为 `drop_window` 或 `mask_days`：
  - `drop_window`：目标窗含任何无效目标日时整窗排除；
  - `mask_days`：保留窗口，无效目标以 `label=null`、`valid=false` 表示，
    该策略不产生任何策略排除。

### 1.3 skus 与 observations

- `skus` 为 1..64 个唯一仓库标识（沿用 `common.ident` 规则）。
- `observations` 每行字段恰为 `{date, sku, sold_units, covered_minutes,
  promoted}`，每个日历日/SKU 至多一行；日期必须在日历内、SKU 必须在声明
  域内，重复行非法。
- `sold_units` 为 `null` 或 0..10^9 的真正整数；`covered_minutes` 为
  `null` 或 0..1440 的真正整数；`promoted` 为 `null` 或真正布尔。
  缺失销量/促销/监测分钟均为 `null`；**显式零销量保留为整数 0**，与缺
  POS 行严格区分。`covered_minutes` 只描述 POS 监测口径。

### 1.4 v6_reports（不可变报告，只消费裁决）

- 最多 64 项，每项恰为 `{report_id, report}`，`report_id` 唯一。
- 只检查并消费 `report.schema_version`（整数 6）与
  `report.line_verdicts`；报告的其他字段不参与资格校验，原样容忍。
- 每条裁决逐行索引 `package/order/lot`，要求同报告内
  `(package, order, lot)` 唯一；检查 SKU 标识、`received` 为 0..10^9 的
  真正整数、`disposition` 恰为 `release/quarantine/unknown/no_receipt`
  之一。
- `no_receipt` 当且仅当 `received=0`；其余三种裁决的 `received` 必须为
  正，违反即整份输入错误。
- 不同报告可以有相同三元组，**不能合并**；同报告内不同包裹也不能互代。
  见证的完整身份是四元组 `(report_id, package, order, lot)`。

### 1.5 shelf_witnesses（在库见证）

- 最多 20000 项；每项字段恰为 `{id, sku, start, end, report_id, package,
  order, lot, visible_at}`，`id` 唯一。
- `start、end、visible_at` 均为 UTC 整分钟时间；门店见证为半开区间
  `[start, end)`，要求 `start < end <= visible_at`，且区间与历史日历
  相交（按日历整体跨度 `[calendar_start 00:00Z, 次日 00:00Z)` 判定）。
- 见证必须按 `(report_id, package, order, lot)` 精确链接既有 v6 裁决；
  悬空引用、歧义键、见证 SKU 不在声明域或与裁决行 SKU 不符、重复 ID、
  非法时间均为整份输入错误。缺失证据时应直接省略见证，不得构造悬空
  引用。
- `visible_at` 表示货架区间与其所引用裁决**同时**可供使用的最早时点。
  见证可引用 `quarantine/unknown/no_receipt` 行：它们只说明“有见证可
  见”，不贡献合格在库分钟。已给出的报告与见证不可替换或撤销，后续截点
  只可能看到更多既有事实。

## 2. 在库分钟与目标有效性

对每个 SKU/日按查询时点计算 `in_stock_minutes`：

1. 不存在任何**已可见**且与该日相交的门店见证时为 `null`；
2. 有已可见见证但没有合格覆盖时为 `0`；
3. 否则为合格区间按 UTC 日剪裁后**并集**的整数分钟数。

合格覆盖要求见证链接的裁决同时满足 `disposition=release` 与
`received > 0`（正到货且放行）。`0` 表示已见到证据但未取得任何合格分钟，
不能解释为门店实际没有货。

- 同日重叠分钟只算一次（位掩码并集）；实际见证跨午夜时按 UTC 日分别剪
  裁，**不把某日剩余分钟结转到另一日**。
- POS 监测分钟与见证在库分钟是两个独立来源，**不能相加或推断交集**。
- 目标有效（`uncensored=true`）需要 POS 行在查询时点可用，且两个口径
  都恰为 1440；此时标签为原销量（显式 0 保留为 0），否则标签为 `null`。
- 可见状态差异只影响相应字段，不修改原始 POS 观测。

## 3. 滑窗样本与切分

对每个 SKU 和日历日，以锚点日为特征末日：

- 特征窗恰含 `lookback_days` 个日（锚点向前数），目标窗从锚点次日开始
  恰含 `horizon_days` 个日；任一窗越出日历则不生成候选，计入
  `boundary_excluded`。
- 目标窗末日不晚于 `split_date` 为 `train`；首日晚于 `split_date` 为
  `test`；否则跨切分，整候选计入 `cross_split_excluded`。测试窗可以引用
  切分前已发生的历史特征；不同窗口可重用同一天。
- **特征日查询时点** = min(锚点次日 00:00Z, `evidence_as_of`)；**目标日
  查询时点** = `evidence_as_of`（无锚点封顶）。因此晚到证明能资格化过去
  的目标日，却不能回填锚点时尚不可见的历史特征。
- `drop_window` 的整窗排除按目标窗**有无缺失 POS** 分成互斥两类：
  `dropped_missing_pos`（至少一个无效目标日在该时点没有 POS 行）与
  `dropped_observed_incomplete`（无效目标日均有 POS 行，但两个口径未
  同时达到 1440）。每个边界内候选只归入跨切分、策略排除、train、test
  四类之一。

## 4. 报告契约（schema_version=8）

成功输出顶层恰为 `{schema_version, source_sha256, policy, calendar,
summary, samples}`：

- `source_sha256`：输入文件**原始字节**的 SHA-256 小写十六进制。
- `policy`：回显三个日期、两个窗口长度、`evidence_as_of` 与删失策略。
- `calendar`：本次证据截点下逐 SKU/日视图（SKU 排序后按日期升序），每行
  含 `date、sold_units、covered_minutes、promoted、in_stock_minutes、
  observed、uncensored`；POS 不可见时前三个 POS 字段为 `null`、
  `observed=false`。
- 每条样本含 `split、sku、anchor_date、feature_days、target_days`；特征
  日使用与日历同一组字段（按该样本锚点封顶查询），目标日在同组字段外再
  给 `label` 与 `valid`。样本按 train 在 test 前、SKU、锚点升序排列；
  日数组按日期升序。
- `summary` 为每 SKU 一条：`boundary_excluded、in_bounds_candidates、
  cross_split_excluded、dropped_missing_pos、dropped_observed_incomplete、
  train_samples、test_samples`，以及按 `calendar` 去重统计的
  `uncensored_days、observed_not_complete_days、no_pos_days`（重复窗口不
  重复累计日数）。空样本合法。
- 除输入字节指纹外，合法输入数组乱序不得改变业务输出；相同输入字节产生
  字节相同的报告，报告中无近似小数、随机种子或随运行时间变化的字段。

## 5. 失败、发布与安全

- 全部输入与关系检查结束后才写出；失败退出 2 并保留已有文件，stderr 给
  出可理解错误，不产生半份报告。
- 沿用仓库严格 JSON、8 MB 输入限制与原子发布（同目录临时文件 +
  `os.replace`）。
- 输入与输出不得为同一路径或硬链接等价文件（含 `os.path.samefile`
  inode 检查）；输出为符号链接时拒绝；输出不得指向输入或其硬链接。

## 6. 演示与验收

```sh
python3 -m perishable_fulfillment.demand_panel \
  --source fixtures/demand_panel_input.json --output run/demand.json
PYTHONPATH=src python3 -m unittest discover -s tests -p 'test_demand_panel.py'
```

`fixtures/demand_panel_input.json` 是手算夹具：UTC 日历 2026-11-20..26、
split=2026-11-22、回看/目标窗各 2 日、单 SKU `milk`。23 日 POS 为显式零
销量且监测满 1440 分钟；23 日唯一在库见证为全天区间，但其
`visible_at=2026-11-27T00:00:00Z`，精确指向由固定基线
`fixtures/coldchain_input.json` 生成的报告 R1 的放行行
`R1/P3/O1/L1`（同报告 `R1/P1/O1/L1` 为 milk 正到货但 quarantine）。

- 截点 `2026-11-25T00:00:00Z`：23 日日历在库分钟为 `null`；锚点 22 的
  23-24 目标窗在 `mask_days` 下 23 日为 `null/false`，在 `drop_window`
  下整窗排除。
- 截点 `2026-11-27T00:00:00Z`：两种策略均保留该窗，23 日为 `0/true`、
  日历在库分钟 1440；同次运行中锚点 24 的 23 日**特征**仍是 POS 零销量、
  在库分钟 `null`、`uncensored=false`。
- 锚点 21（特征 20-21、目标 22-23）在两次运行、两种策略下均只计为跨切分
  排除。
- 独立变体把 23 日见证改指 quarantine 行 `R1/P1/O1/L1`，或改指独立不可变
  报告 R2 中同名三元组的隔离行：早截点仍为 `null`，晚截点为 `0` 且目标
  无效。
- 另一变体保留晚可见放行见证，另加同区间、不同 ID、在
  `2026-11-24T00:00:00Z` 可见的 quarantine 见证：早截点日历 23 日与晚运
  行中锚点 24 的 23 日特征均为在库分钟 `0`，而晚截点的 23 日目标仍是
  `0/true`。

测试另覆盖：零销量与缺 POS 区分、先有非合格证据与尚无可见证据、跨午夜
与重叠见证、部分见证的不同可见时点、精确截点等号、部分 POS 监测、仅有
unknown/no_receipt、错误 SKU/链接、重复键/非法字段/类型、乱序确定性、
空样本合法性与失败保留旧结果、符号链接/同文件/硬链接拒绝。
