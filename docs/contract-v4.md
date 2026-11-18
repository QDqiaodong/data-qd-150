# 离线零售促销日程分析 v4

门店已经用未修改的 v1 分配器（`model.analyze`）完成 `as_of` 当天的履约订单。本模式是一次性、只读的离线分析：不重做订单分配，直接把 v1 结果中逐 lot 的 `available_remaining` 作为未来零售的初始库存，为之后若干天选择公开售价；不写真实库存或价格，不读取 v2 承诺账本，也不使用 v3 路网。仅用 Python 标准库。

- **入口**：`perishable-markdown --source JSON --output JSON`（也可 `python3 -m perishable_fulfillment.markdown`）。
- **可直接调用的分析函数**：`perishable_fulfillment.markdown.compile_report(data, source_sha256)`，输入为已解析的 v4 源对象与源字节 SHA-256，返回完整报告对象；`perishable_fulfillment.markdown.run(source_path)` 从文件读取并计算。
- 代码：`src/perishable_fulfillment/markdown.py`；测试：`tests/test_markdown.py`；样例：`fixtures/markdown_input.json`。
- v1/v2/v3 的输入、输出与行为保持不变，本模式只在旁新增。

## 输入契约

顶层严格为六个字段 `schema_version、fulfillment、dates、lot_costs、offers、promo_slots`，拒绝未知字段、重复 JSON 键、`NaN/Infinity`，沿用 8 MB 字节上限与 `common.ident` 标识符规则（非空、无首尾空白、无控制字符、≤128 字符）。

- `schema_version`：必须是整数 `4`（拒绝布尔、浮点、字符串）。
- `fulfillment`：原 v1 原样对象，严格只含 `as_of/lots/orders`；lot 仍只含 `id/sku/expires/quantity/held`，order 仍只含 `id/sku/priority/quantity`，v1 的类型、范围、唯一性与校验完全沿用，**不在这些对象里加单位成本**。订单仍最多 20000 条。
- 规划 SKU 集合为所有 `lot.sku` 与 `order.sku` 的并集，必须 **1..6 个**；每个 SKU 最多 **4 个源 lot**（含零量、held 或已过期 lot）。
- `dates`：1..8 个规范 `YYYY-MM-DD`，必须已升序、不重复、逐日连续，首日恰为 `as_of` 的下一自然日；日期按日历推进而非字符串拼接。不存在下一自然日的 `as_of` 非法。
- `lot_costs`：每项严格 `{lot, unit_cost}`，每个源 lot 恰好映射一次，不得悬空、重复或遗漏（含今日已过期、初始余量为零的 lot）。`unit_cost` 为每件整数分，范围 `0..10^9`。
- `offers`：每项严格 `{date, sku, options}`，每个 `dates × 规划SKU` 格子恰好一项，不得重复、遗漏或引用额外日期/SKU。`options` 为 0..4 项，每项严格 `{id, price, demand, discounted}`：
  - `id` 遵循 ident 规则且在该格子内唯一；空字符串 `""` 保留给隐式不销售动作，公开选项不得使用；不同格子可复用 ID。
  - `price`、`demand` 均为 `0..10^9` 整数（每件分、件数）。
  - `discounted` 必须是真正的 JSON 布尔，由业务输入声明是否占折扣位，**不从价格推断**。
- `promo_slots`：每项严格 `{date, slots}`，每个规划日期恰好一项；`slots` 为 `0..6` 整数。

所有整数拒绝布尔、浮点和数字字符串；所有对象拒绝多余/缺失字段，数组拒绝错误类型。ID 比较一律用 Python 字符串字典序。除 `dates` 必须已排序外，合法输入数组的排列不改变业务结果。

## 库存、销售与收益语义

- 每个 SKU 每日恰选一个公开 option，或隐式不销售（动作 ID `""`）；不允许只选部分需求或自创价位。
- 不销售输出 `action_id=""`，且 `price/demand/sold/unmet/revenue/cost/profit` 全为 0、`discounted=false`。
- 公开动作 `sold = min(demand, 当日该 SKU 全部未过期可售余量)`，`unmet = demand - sold`。
- 售出按 `(expires, lot id)` 升序依次消耗批次（FEFO），**按批次批量扣减，不按件展开**。
- 每批收入 = 售出件数 × 所选 `price`，成本 = 售出件数 × **该批自己的** `unit_cost`，毛利 = 收入 − 成本；不使用平均成本、最低成本或原始总库存。
- held、今日已分配给订单以及 v1 判定过期的量都不再出售。

期限边界（关键）：

- 每个源 lot 的 `initial_available` = 今日 v1 结果的 `available_remaining`。
- 首日之前已过期（`expires < dates[0]`，含 `expires == as_of` 的余量）记为 `expired_before_horizon`，不进入首日 `opening`；其余 lot 该项为 0。
- 首日 `opening = initial_available - expired_before_horizon`，之后每日 `opening = 前一日 closing`。
- 每天先按所选动作销售，再把 `expires` 等于当天的剩余量全部记 `expired`，`closing = opening - sold - expired`。
- 过期批次后续 opening/closing 均为 0；末日仍未到期的 closing 保留为期末存量。
- `expired` 是件数，不按成本计负毛利、不重复扣除；不计持有费、残值、折扣固定费或到期损失费用。

折扣位：折扣动作即使需求或实际销量为 0，被选中仍占当日一个名额；非折扣动作与不销售不占名额。每日期末名额约束逐日检查，不能把全期名额加总后搬移。

## 三级目标与精确算法

在整段预测期、所有 SKU、所有天上联合选择，严格按以下顺序：

1. 最大化整期总毛利（整数分）；
2. 在 1 的最优解中，最小化折扣 SKU 日动作数（整个日程中 `discounted=true` 的已选格子数，不按销量或件数计数）；
3. 在 1、2 的最优解中，取动作 ID 序列字典序最小。动作序列按日期升序、同日 SKU 升序交织，包含不销售；比较字符串元组，空字符串 `""` 小于任何公开 ID。

必须返回全局精确最优，不接受逐 SKU 独立最优、逐日贪心或启发式。实现（`markdown.py`，仅标准库、全整数、不按件展开）：

- **逐 SKU 紧凑枚举**：`enumerate_sku_schedules` 以日为层、以至多四个 lot 的批量余量元组为状态做记忆化 DFS；按折扣向量归并，只保留每个向量下毛利最高、再动作块排名最小的日程。
- **资源帕累托剪枝**：`_prune_alternatives` 删除被同 SKU 另一日程支配的选项——后者任一天折扣位都不更多、毛利不更低、块排名不更小且至少一项严格更优。这种替换在任何其它 SKU 组合下都不会破坏名额或恶化三级目标，因此剪枝精确。
- **跨 SKU 动态规划**：`optimize` 以 base-7 整数编码每日已用名额向量（逐日校验名额上限），以 base-5 块数字编码跨 `(日期, SKU)` 的动作字典序排名；每个活跃名额码维护 `(排名, 毛利)` 的帕累托前沿，并用回溯指针重建逐 SKU 选择。
- 公开上限（6 SKU、8 天、每格 4 价位、每 SKU 4 lot、`10^9` 件）下为秒级、内存约数十 MB；库存始终是批量整数，不存在按件节点。

### 19 分最小跨期案例

两天、每天 `slots=1`，今日无订单/held；A 有 1 件、第二天到期、成本 0，B 有 1 件、第一天到期、成本 0。A 第一天唯一公开折扣价 11/需求 1，第二天 10/需求 1；B 第一天 9/需求 1，第二天 options 为空。逐 SKU 各自最优或逐日先取 11 都会错过 B 的期限；联合最优为第一天卖 B、第二天卖 A，总毛利 **19**，动作 ID 序列（按日按 SKU：`A,B` 交织）为 `("", "b9", "a10", "")`。所有动作都另有不销售可选。

## 报告契约（schema_version=4）

成功输出顶层严格为 `schema_version、source_sha256、fulfillment、objective、days、lots`：

- `schema_version=4`；`source_sha256` 是输入源**原始字节**的 SHA-256 小写十六进制（不是规范化 JSON 的哈希）。
- `fulfillment` 是 `model.analyze(输入 fulfillment)` 的完整原 v1 结果，字段与顺序不变更。
- `objective` 严格为 `{profit, discount_sku_days, action_ids}`：整期总毛利、折扣动作数、按 `(日期, SKU)` 顺序的动作 ID 数组。
- `days` 按日期升序，每项严格为 `{date, promo_used, revenue, cost, profit, actions, lot_ledger}`：
  - `actions` 按 SKU 升序并完整覆盖规划 SKU，每项严格为 `{sku, action_id, price, demand, discounted, sold, unmet, revenue, cost, profit}`；公开动作的 `price/demand/discounted` 来自所选 option。
  - `promo_used` = 当日被选折扣动作数且不超过输入 `slots`；当日三项金额等于 actions 之和；整期 profit 与 objective 一致。
  - `lot_ledger` 按 `lot.id` 升序、完整覆盖所有源 lot（含全零者），每项严格为 `{lot, opening, sold, expired, closing, revenue, cost, profit}`，满足 `opening = sold + expired + closing`；按 SKU 汇总的 sold 与三项金额等于该日该 SKU 的 action。
- `lots` 按 `lot.id` 升序、完整覆盖源 lot，每项严格为 `{lot, sku, expires, unit_cost, initial_available, expired_before_horizon, sold, expired, ending, revenue, cost, profit}`；`sold/expired` 与三项金额为未来 days 的逐批合计，`ending` 为最后日 closing，且满足
  `initial_available = expired_before_horizon + sold + expired + ending`。`expired` 不再包含 `expired_before_horizon`。

所有件数与金额都是 JSON 整数；单行动作轨迹的 profit 可为负，但整期可全选不销售而至少为 0。无近似小数、内部搜索状态、随机种子或随运行时间变化的字段。相同原始输入报告字节相同；只重排合法输入时，`source_sha256` 与继承自 v1 的既有排序部分可以不同，其余零售日程与轨迹必须相同。

## 失败、发布与边界

- 源为单一只读输入；分析过程不创建/修改 v2 库、不读取 v3 路网。完整读取、校验、优化与报告构造成功后才发布。
- 成功退出 0；参数、文件读取、JSON、引用、类型、范围、规划或写出错误均退出 2，stderr 给可理解错误，不向 stdout 混入半份报告。
- 输出是符号链接，或 source/output 指向同一路径/同一文件（含符号链接解析与已有硬链接同 inode）时拒绝。
- 写出沿用同目录临时文件 + `os.replace` 原子替换；失败清理临时文件并保留已有报告，源原始字节始终不变。
- 不额外实现抵御并发恶意文件系统替换的机制，沿用仓库现有单操作者发布保证。

## 演示与测试

```sh
python3 -m perishable_fulfillment.markdown \
  --source fixtures/markdown_input.json --output run/markdown.json
PYTHONPATH=src python3 -m unittest tests.test_markdown -v
```

`fixtures/markdown_input.json` 即 19 分跨期案例，报告 `objective.profit=19`、`discount_sku_days=2`、`action_ids=["", "b9", "a10", ""]`。

测试用独立小规模**全枚举 oracle**（`brute_force`，只用 `itertools` 重放原始 lot，不调用生产优化器的候选生成/剪枝/合并）核对毛利、折扣数与字典序三级目标及选定动作；覆盖同日期共享名额竞争、早期销售改变后续批次成本与过期供给、逐 SKU 独立最优超额、负毛利不售、零销量折扣占位、空 options、零库存、`10^9` 数量不按件展开、同期限不同 lot.id、不同成本批次、`expires=as_of/首日/末日`、held 与今日订单扣量、输入重排、非法输入保留旧报告，以及公开最大 SKU/日期/价位/lot 数量的可完成性与逐 lot 逐日守恒。
