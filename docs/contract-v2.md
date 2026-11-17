# Perishable commitment ledger v2

仓配人员在同一个 SQLite 账本上连续提交事件批次。批次尚未拣货的预留可以反复调整；已经拣走的实物是不可变事实，任何后续事件都不能重新分配。业务计算完全离线，仅使用 Python 标准库。

## 新旧模式边界

- **v1（保留、行为不变）**：`perishable_fulfillment.cli --source --output`，单日、单操作者、无状态快照。输入上限 20000 批次/订单，输出 `schema_version=1`。代码仍在 `model.py`、`fulfill.py`、`cli.py`，逐订单 FEFO 贪心。
- **v2（本账本）**：`perishable-book book-init|book-apply|book-report`，有状态、串行化、事件溯源。上限 64 批次、64 订单、每批 128 事件，数量仍为 0..10^9 整数，输出 `schema_version=2`。代码在 `ledger.py`、`planner.py`、`flow.py`、`book.py`。
- 两模式共享 `common.py` 的严格 JSON 与原子报告发布；v2 不导入 v1 分配器，v1 也不依赖 SQLite。v2 的源文件结构沿用 `as_of/lots/orders`，但订单字段扩展，二者输入不可混用。

## book-init 源契约

顶层严格三字段 `as_of`、`lots`、`orders`，拒绝未知字段、重复 JSON 键、NaN/Infinity。

- `as_of`：ISO 日期，账本固定基准日。
- `lots`：最多 64 个，唯一 `id`；字段沿用 v1 的 `id/sku/expires/quantity/held`，`0 <= held <= quantity <= 10^9`。初始化之后实物初始数量、SKU、到期日永不变更；只有 `held` 可经事件修改，`picked` 只增。
- `orders`：最多 64 个，唯一 `id`，字段为
  `id/sku/priority/quantity/ship_date/min_life/allowed_lots`。
  - `ship_date` 不早于固定 `as_of`。
  - `min_life` 为 0..3650 的整数天；适销条件 `expires >= ship_date + min_life`，日期相加溢出直接拒绝。
  - `allowed_lots`：`null` 表示无额外限制；否则是无重复的、现存且同 SKU 的批次 id 数组；空数组 `[]` 表示该订单没有任何准入批次（必然全短缺，而非报错）。
- `book-init` 只创建全新账本：目标 DB 或其 `-wal/-shm` 已存在即拒绝，符号链接拒绝。初始 `revision=0` 并计算首份承诺。

## 事件契约（book-apply）

请求体严格三字段 `request_id`、`expected_revision`、`events`。`request_id` 为非空标识；`expected_revision` 为非负整数；`events` 为 1..128 项数组，按数组顺序处理，事件对象也拒绝未知字段。

- `set_hold = {type,lot,held}`：把该批次冻结量整笔改为 `held`，要求 `held + 累计已拣 <= 初始库存`，否则整批拒绝（冻结覆盖已拣属于无解业务输入）。
- `put_order = {type,id,sku,priority,quantity,ship_date,min_life,allowed_lots}`：新建订单或整行修改既有订单。既有订单的 SKU 不可变；`quantity` 不得低于该订单累计已拣。日期、保鲜、准入校验同源契约。
- `cancel_order = {type,order}`：把需求降为该订单的累计已拣总量并保留全部事实，订单标记 `canceled`。
- `pick = {type,order,lot,quantity}`：`quantity` 为正整数。必须来自本批开始时已提交的该订单逐 lot 预留（扣除本批先前 pick 后仍足额）；新建订单或本批尚未提交的规划不能授权 pick。事件发生时还须满足同 SKU、客户准入、保鲜适销，以及 `held + 累计已拣 + 本次 <= 初始库存` 的未冻结实物余额。

每个事件后都复核两条不变量：订单 `已拣 <= quantity`；批次 `held + 累计已拣 <= 初始库存`。订单日期/保鲜/准入的修改只约束尚未拣货的需求，历史拣货不追溯撤销。批内任一事件错误都不会留下部分事实或版本。

## 串行化、重试与持久化

- 新 `request_id` 只有在 `expected_revision` 等于当前版本时才提交；SQLite `BEGIN IMMEDIATE` 串行化校验、规划与写入，成功恰好递增一次 revision。
- 相同 `request_id` 且**原始请求字节**相同（SHA256 相等）的重试优先于 `expected_revision` 检查，直接恢复那次提交保存的原报告；相同 `request_id` 但字节不同一律拒绝。
- 事实（pick）、版本、预留与不可变回执在同一事务提交。报告文件写出可能在数据库提交后失败：此时已拣事实不回滚，CLI 以退出码 3 提示；之后用相同字节重试仍恢复旧回执。即使中间已有更新版本成功，旧请求重试返回旧报告，`book-report` 仍给当前版本，二者互不覆盖数据库状态。

## 三层全局承诺规划

事件全部裁决后，以未冻结且未拣实物为容量、订单 `quantity - 已拣` 为需求，允许整数拆分，按严格优先级求解：

1. 订单按 `(priority,id)` 升序，对各订单“已拣 + 预留”组成的向量求字典序最大（前缀最大流，逐订单固定已得最优值）。
2. 在第一层最优集合中，最小化 `sum(max(old[o,l] - new[o,l], 0))`，`old` 是上一已提交预留扣除本批 pick 后的余额（含被取消或修改订单的条目）。实现为折扣弧最小费用流：每对订单-批次的前 `old` 单位费用 -1，其余 0，固定总流量下最小费用即最大保留旧承诺。
3. 在前两层最优集合中，按订单 `(priority,id)`、批次 `(expires,id)` 遍历所有订单-批次对，令新预留向量字典序最大，作为 FEFO 确定性破同分。实现为单条最小费用流，位置权重 `-B**(P-1-k)`，基数 `B = 总剩余需求 + 1`，严格大于任意后缀位置可承载的最大流量，因此高位 pair 的一单位增量在整数数值上必然压倒所有低位增量之和——这是字典序最大化的标准精确整数编码，非浮点、非未证明权重、不按库存单位展开。

三层目标逐层严格优先；库存不足正常产生短缺，冻结覆盖已拣等无解输入才拒绝。

## 报告契约（schema_version=2）

- 顶层：`schema_version`、`ledger_revision`、`request_id`（init 为空串）、`request_sha256`（init 为空串）、`source_sha256`、`movement`、`orders[]`、`lots[]`，全部稳定排序。
- 逐订单：`order/sku/priority/requested/picked/reserved/shortage/canceled/reservations[]`，`reservations` 为 `{lot,quantity}` 并按 lot 排序。
- 逐批次：`lot/sku/expires/quantity/held/picked/reserved/remaining`。
- 守恒：批次 `初始库存 = held + picked + reserved + remaining`；订单 `需求 = picked + reserved + shortage`。
- 报告经临时文件 + 原子替换发布，拒绝符号链接；输入/计算失败保留既有报告，沿用 v1 的输出安全规则。

## 存储结构与一致性代价

- SQLite 表：`meta`（revision、as_of、来源 SHA、最近请求）、`lots`（不可变实物 + held/picked）、`orders`（含 `allowed_lots`，无限制用哨兵 `*` 存储以区别空名单）、`picks`（只增的拣货事实流）、`reservations`（当前逐 pair 预留）、`commits`（request_id 唯一、请求字节 SHA256、revision、完整不可变报告 JSON）。
- WAL 日志、`synchronous=FULL`、单写者 `BEGIN IMMEDIATE`。规模有界（64×64），网络是紧凑的订单→批次二分图，容量为整体整数，不展开库存单位；最坏密集场景的规划在秒级，主要代价来自第三层大整数（最多约 4096 位十进制权重）费用流运算。
- 一致性代价：所有事件先在内存候选态裁决并整体重规划，通过后才单事务落库，因此批内失败零副作用；代价是每批都重算全量承诺，而非增量补丁，换来严格的三层最优与可证明守恒。
