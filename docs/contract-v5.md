# 离线批次召回影响分析 v5（schema_version=5）

- **入口**：`perishable-recall --source JSON --output JSON`（也可
  `python3 -m perishable_fulfillment.recall`）。
- **Python API**：`perishable_fulfillment.recall.compile_report(data, source_sha256)`，
  输入为已解析的 v5 源对象与源字节 SHA-256；`run(source_path)` 从文件读取并
  计算摘要。仅 Python 标准库，全整数运算，不按件展开、不使用浮点。
- 代码：`src/perishable_fulfillment/recall.py`；样例：
  `fixtures/recall_input.json`（最小混装案例）、
  `fixtures/recall_scale.json`（96 节点的代表性规模案例）；测试：
  `tests/test_recall.py`，其中 `OracleValidation` 使用完全独立的小整数全枚举
  oracle，不调用任何生产网络构造或见证规范化代码。

## 1. 业务模型与输入事实

一件是不可分的包装追溯单位。拆分、混装只搬运这些件，件数守恒；SKU 可因
包装变化而不同，追溯单位件数仍守恒。召回人员掌握来源批次中确定的污染件数，
以及生产拆分、混装记录；每次混装后污染件具体进入哪个去向不可知。

顶层严格为四个字段 `schema_version`、`fulfillment`、`trace`、`queries`，
拒绝未知/缺失字段、重复 JSON 键、`NaN/Infinity`、布尔/浮点/数字字符串，所有
标识符沿用 `common.ident`，日期沿用 model 的合法日期语义。

### 1.1 fulfillment（原样沿用 v1 as_of/lots/orders 结构、类型和边界）

`fulfillment.lots` 与 `fulfillment.orders` 最多各 32 项，件数为
`0..10^9` 的真正整数。v1 分配由未改动的 `model.analyze` 固定，本模式不在
v1 之外重新分配订单，也不把召回基数建立在 `available_remaining` 上。

### 1.2 trace（物料有向无环图）

- `nodes` 最多 96 个唯一节点 ID；`sources` 最多 32 项，每项
  `node/quantity/tainted`，满足 `0 <= tainted <= quantity <= 10^9`。
- `transfers` 最多 256 条边，边 ID 唯一、端点存在、无自环、允许同端点平行
  边；每条边 `quantity/lower/upper` 为整数且
  `0 <= lower <= upper <= quantity <= 10^9`。
- `lot_nodes` 与 32 个 fulfillment lot 一一对应，每个 lot 一个唯一终端
  节点；`waste_nodes` 最多 32 个唯一废弃终端。来源、lot 终端、废弃终端
  三类节点互斥；其余为中间节点。
- 结构守恒（非法输入，退出 2）：来源节点入边为 0 且出边总件数等于来源
  `quantity`；lot 终端出边为 0 且入边件数等于对应 `lot.quantity`；废弃
  终端出边为 0，其入边件数就是其物理件数；每个中间节点入件数等于出件数；
  全图无有向环（Kahn 校验）。没有边的零量来源、零量批次或孤立中间节点允许
  存在，但有节点却没有任何边的图无法到达终端，按非法输入拒绝。
- 空图（`nodes=[]`）只能配空 `sources/lot_nodes/waste_nodes/transfers/lots`，
  仍可有零履约订单。

### 1.3 queries（联合查询，最多 16 项）

每项 `id` 唯一，`bounds` 为空或最多覆盖完整对象数、每项恰有
`kind/id/lower/upper`，`kind ∈ {order, held, expired, available, waste}`，
整数 `0 <= lower <= upper <= 10^18`（超过对象实物数时合法但不可行）。同一
查询内对象键不重复，且必须引用存在的对象；对象 ID 可跨 kind 复用，不会发生
字符串键碰撞。每个查询独立叠加在同一原始事实上，不继承前一查询。

## 2. 固定履约去向（v1 数量语义）

每个 lot 保留三类分段并按 `(kind, id, lot)` 字符串元组升序排列，零量分段也
完整输出；另对每个正数 v1 allocation 建一个 `kind='order'` 分段，
`id/lot` 为订单与批次：

- `held`：数量等于原 lot `held`；
- `expired`：`expires < as_of` 时为 `quantity - held`，到期当日
  （`expires == as_of`）仍未过期，该段为 0；
- `available`：v1 的 `available_remaining`；
- `order`：v1 订单逐 lot 的分配量；一个订单跨多个 lot 时产生多个 order 段，
  汇总到同一个订单对象。

每个 lot 所有分段数量之和严格等于原 lot `quantity`。缺货不是实物去向。

## 3. 紧确污染范围与联合见证

### 3.1 有界整数流

污染解释建模为多源有界整数流：固定 `origin -> source` 弧注入来源确定的
tainted 总量；物料边携带观测区间 `[lower, upper]`；lot 终端的污染在其分段
间整数分配，每段 `0 <= 污染 <= 段实物量` 且分段污染之和等于终端流入污染；
订单段先汇入订单节点再进入统一 sink，废弃节点直接入 sink；一条
`sink -> origin` 回流弧以总污染量闭合环流。可行性是标准超源/超汇节点平衡
变换，由自研整数 Dinic 求解；容量保持 Python 大整数，`10^9` 件也只做批量
容量运算，不存在按件枚举。

- 物料**实物**总量守恒失败（结构校验）是输入错误，退出 2；
- 结构合法但不存在满足全部来源、边界、节点平衡、分段约束的整数污染解释时，
  报告 `status='inconsistent'`（合法事实矛盾，不是输入错误）。

### 3.2 紧确 min/max

每个收货对象 `(kind,id)` 的污染总量为其全部相关弧/段污染之和。对每个对象，
通过在新一轮可行性检查上施加单边界并做整数二分，分别求出可达到的最小与最大
污染；两个极值都由至少一份完整解释达到。不同对象的极值分别计算，因此对象
单独区间相交并不代表联合可行。

状态判定：`max=0 -> clear`；`min>0 -> certain`；否则 `possible`，不输出概率。

### 3.3 规范化字典序见证

见证向量先按边 ID 升序排列全部物料边污染，再按 `(kind,id,lot)` 升序排列全
部分段污染，含零量段，不含派生的对象汇总量。按该顺序逐个变量，在已固定前缀
下用可行性二分求该变量的最小可取值并永久固定，得到全局字典序最小的完整解释；
这不是逐弧贪心，每一步都以可行性为约束。普通字符串与普通 Python 字符串字典
序比较，整数为精确 JSON 整数。

基准见证（空 bounds）相当于一次空查询的最小解释。每个可行查询独立返回满足其
bounds 的规范最小解释；不可行查询 `feasible=false`、`witness=null`。报告发布
前用 `verify_witness` 从输入与见证重放来源、边界、节点平衡、终端分段和查询
bounds，任一不符即触发内部错误而不发布。

## 4. 报告契约与失败边界

成功报告顶层恰为：

```
schema_version, source_sha256, fulfillment, status, recipients,
base_witness, queries
```

- `fulfillment` 是完整原 v1 分析结果；`source_sha256` 是原始输入字节摘要，
  合法数组重排时它与 v1 顺序部分允许不同，但召回对象、范围与规范解释必须相同。
- `recipients` 按 `(kind,id)` 升序，每项恰有
  `kind/id/quantity/min_tainted/max_tainted/status`；`quantity` 是实物数量，
  与污染范围分列。完整对象集合为全部订单（含零履约）、每个源 lot 的
  held/expired/available 三对象、每个 waste 节点对象。
- `base_witness` 恰有 `transfers`（按边 ID，`{edge,tainted}` 覆盖全部物料
  边）与 `segments`（按元组，`{kind,id,lot,quantity,tainted}` 覆盖全部分段，
  含零量段）。
- `queries` 按查询 ID 升序，每项 `{id,feasible,witness}`；inconsistent 时
  `recipients=[]`、`base_witness=null`，所有查询均 `feasible=false`。
- 所有数量与范围都是精确 JSON 整数。

参数、读写、JSON、引用、映射、总量、拓扑、非法字段、NaN/Infinity 等错误退出
2 并保留既有报告；污染约束矛盾是成功输出的 `inconsistent`。输出走
`common.save` 的同目录临时文件 + `os.replace` 原子发布；输出为符号链接、与源
同一文件（含硬链接同 inode）一律拒绝。失败清理临时文件，源字节始终不变；相同
原始字节重跑产生字节相同的报告。

## 5. 规模与算法说明

公开上限：96 节点、256 边、32 来源、32 lot、32 订单、16 查询，件数
`0..10^9`、查询界 `0..10^18`。每次可行性是一次小图 Dinic（约百级节点、数百
弧，亚毫秒级）；极值对每个对象做约 30 次二分可行性，规范见证对约数百个变量
顺序固定，典型输入亚秒级、满规模数秒级完成，全程整数、无浮点、无按件展开。
