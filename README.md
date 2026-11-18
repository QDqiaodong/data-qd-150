# data-qd-150 · 易腐品批次履约

FEFO lot allocation with expiry, holds and shortage reconciliation。业务输入、计算和当前边界详见 [数据契约 v1](docs/contract.md)、[承诺账本 v2](docs/contract-v2.md)、[载重波次 v3](docs/contract-v3.md) 与 [零售促销日程 v4](docs/contract-v4.md)。

本仓包含四个边界清晰、互不依赖的模式：

- **v1 单日分配器（无状态，保留原行为）**：`perishable-fulfillment --source --output`，逐订单 FEFO 贪心，输出 `schema_version=1`。
- **v2 易腐品承诺账本（有状态、可连续使用）**：`perishable-book book-init|book-apply|book-report`，SQLite 持久化、事件溯源、三层全局承诺规划，输出 `schema_version=2`。已拣实物不可重新分配，未拣预留可随事件调整。
- **v3 载重约束的整单拣货波次（无状态，只读 v1 结果）**：`perishable-wave --source --output`，直接调用 v1 分配器，把固定结果编译成仓内波次、有向行走与逐次装载清单，输出 `schema_version=3`。不修改分配、不写库存、不读取 v2 账本。
- **v4 离线零售促销日程分析（无状态、只读 v1 余量）**：`perishable-markdown --source --output`，以 v1 逐 lot `available_remaining` 为未来零售初始库存，联合选择各 SKU 每日公开价位或不销售，在每日折扣名额下求整期毛利精确最优并追溯逐 lot 售出/过期/结余，输出 `schema_version=4`。只做一次性离线分析，不写真实库存或价格，不读取 v2 账本或 v3 路网。

## 安装与演示

Python >=3.11，运行和测试仅用标准库；先确认 `python3 --version`。
每仓建立独立环境，构建工具锁版本。首次安装无公共缓存时可能联网；业务运行不访问网络。

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements-build.lock
.venv/bin/python -m pip install --no-build-isolation --no-deps .
.venv/bin/python -m pip check
.venv/bin/perishable-fulfillment --source fixtures/input.json --output run/report.json
.venv/bin/python -m unittest discover -s tests -v
```

本地已有wheel时，安装构建工具可加 `--no-index --find-links <公共wheel缓存目录>`。
修改源码后重新安装。本仓不共享其他仓环境，不依赖Docker、外部账号或持续服务。

### v2 承诺账本 CLI（仅标准库，离线）

```sh
# 仅创建新账本：revision=0 并计算首份承诺
python3 -m perishable_fulfillment.book book-init \
  --db run/book.db --source fixtures/book_source.json

# 提交一个事件批次（pick/set_hold/put_order/cancel_order，最多128项）
python3 -m perishable_fulfillment.book book-apply \
  --db run/book.db --source fixtures/book_request_pick.json --output run/r1.json

# 后续批次：提高保鲜要求、收紧准入、冻结，未拣承诺自动迁移
python3 -m perishable_fulfillment.book book-apply \
  --db run/book.db --source fixtures/book_request_tighten.json --output run/r2.json

# 打印当前版本回执；相同 request_id + 相同字节的 apply 重试返回原报告
python3 -m perishable_fulfillment.book book-report \
  --db run/book.db --output run/current.json
```

提交后若报告文件写出失败，数据库事实不回滚，命令以退出码 3 结束；用相同请求字节重试即可恢复原回执。规划采用紧凑整数网络流（不展开 10^9 库存、无浮点），三层目标与存储/一致性代价见 [contract-v2.md](docs/contract-v2.md)。

### v3 载重约束拣货波次 CLI（仅标准库，离线）

```sh
# 把 v1 固定分配编译成波次、有向行走与逐次装载清单
python3 -m perishable_fulfillment.wave \
  --source fixtures/wave_input.json --output run/wave.json

# 静态最短回边不可承重、必须绕行的合法路线
python3 -m perishable_fulfillment.wave \
  --source fixtures/wave_detour.json --output run/detour.json

# 必须先经过某货位而不取、从别处取货后再回来整取
python3 -m perishable_fulfillment.wave \
  --source fixtures/wave_roundtrip.json --output run/roundtrip.json
```

输入顶层严格为 `{fulfillment, warehouse}`：`fulfillment` 是原 v1 输入（本模式最多 8 订单、64 批次），`warehouse` 是有向载重路网。先最小化波数、再最小化总距离，再按订单组元组与动作签名取字典序最小；路线状态包含已取货位集合，路过不自动拾取，载重含等号。某个订单单独不可达时整次命令失败并列出这些订单。完整契约、守恒与独立枚举说明见 [contract-v3.md](docs/contract-v3.md)。

### v4 离线零售促销日程 CLI（仅标准库，离线）

```sh
# 19 分跨期案例：第一天卖临期 B、第二天卖 A，联合毛利 19
python3 -m perishable_fulfillment.markdown \
  --source fixtures/markdown_input.json --output run/markdown.json
```

也可安装后直接用 `perishable-markdown --source INPUT.json --output REPORT.json`，或在 Python 中调用：

```python
from perishable_fulfillment.common import read
from perishable_fulfillment.markdown import compile_report
data, digest = read('INPUT.json')          # 严格 JSON + 源字节 SHA-256
report = compile_report(data, digest)       # 返回完整 schema_version=4 报告
```

输入顶层严格为 `{schema_version, fulfillment, dates, lot_costs, offers, promo_slots}`（`schema_version=4`）；`fulfillment` 是原 v1 对象，单位成本经额外 `lot_costs` 按 lot 映射，不改动 v1 字段。最多 6 规划 SKU、8 个连续日期、每格 4 价位（另有不销售）、每 SKU 4 lot，库存 `0..10^9` 整数且预测期无补货。每个 SKU 每日选一个公开价位或不销售，`sold=min(demand, 未过期可售余量)`，按 `(expires, lot id)` FEFO 批量扣批次，并以实际售出批次的单位成本计毛利。

三级目标严格分层：**先最大化整期总毛利，再最小化折扣 SKU 日动作数，最后按日期升序、SKU 升序的动作 ID 序列取字典序最小**（不销售 ID 固定为空字符串，小于任何公开 ID）。折扣名额逐日校验，折扣动作即使零销量也占位。求解是逐 SKU 记忆化枚举 + 资源帕累托剪枝 + 跨 SKU 名额动态规划的全局精确解，全整数、不按件展开。完整契约、期限边界、报告字段与独立枚举验收见 [contract-v4.md](docs/contract-v4.md)。

输入JSON最多8MB，拒绝重复键、非有限数值和未定义字段；源字节SHA256随结果保留。
输出是可独立读取的JSON；成功后原子替换，输入/计算失败退出2并保留旧报告。
输入和输出不得相同，输出符号链接拒绝。单操作者批处理，不承诺并发发布或断电恢复。
同输入重跑不累加。测试使用临时目录，运行产物在忽略的run/。

src/perishable_fulfillment/model.py 为业务模型，相关算法模块独立组织；common.py 提供受限JSON及报告发布，
cli.py 衔接数据链路，wave.py 为 v3 波次编译器，markdown.py 为 v4 离线零售促销日程分析（公开入口 compile_report(data, source_sha256) / run(source_path)）。fixtures 为明确自制样例，代码与样例采用MIT许可。

## Scope and sample
# Perishable lot fulfillment v1

仓配分析人员将单日需求分配到可售批次。as_of为ISO日；lots最多20000个唯一lot id、sku、expires、quantity、held，held不得大于库存。orders最多20000个唯一id、sku、priority(小优先)、quantity。库存和需求均为0..10^9整数，不存在负库存或重复订单事件。有效期含到期日：expires>=as_of才可分配；早于as_of的过期库存不参与。
先按priority、order id排序；同优先组按order id确定顺序，不做比例分配。每单按FEFO选择同SKU可用批次（expires升序、lot id同日打破），held库存先排除；输出订单逐lot分配、短缺、批次剩余可售量、过期与冻结量。可用库存守恒为初始未过期未冻结=分配+剩余。
样例L1可售6、L2可售10、L0过期；O1取L1的6和L2的3，O2取L2余7、履约7并短缺1。
不做多日未来需求、温度暴露、替代SKU、拣货路由、订单取消或真实库存写入。到期日解释、冻结标记与优先政策应由调用方确认；输出是单次计划快照。


## Scope and sample
# Perishable lot fulfillment v1

仓配分析人员将单日需求分配到可售批次。as_of为ISO日；lots最多20000个唯一lot id、sku、expires、quantity、held，held不得大于库存。orders最多20000个唯一id、sku、priority(小优先)、quantity。库存和需求均为0..10^9整数，不存在负库存或重复订单事件。有效期含到期日：expires>=as_of才可分配；早于as_of的过期库存不参与。
先按priority、order id排序；同优先组按order id确定顺序，不做比例分配。每单按FEFO选择同SKU可用批次（expires升序、lot id同日打破），held库存先排除；输出订单逐lot分配、短缺、批次剩余可售量、过期与冻结量。可用库存守恒为初始未过期未冻结=分配+剩余。
样例L1可售6、L2可售10、L0过期；O1取L1的6和L2的3，O2取L2余7、履约7并短缺1。
不做多日未来需求、温度暴露、替代SKU、拣货路由、订单取消或真实库存写入。到期日解释、冻结标记与优先政策应由调用方确认；输出是单次计划快照。


## Scope and sample
# Perishable lot fulfillment v1

仓配分析人员将单日需求分配到可售批次。as_of为ISO日；lots最多20000个唯一lot id、sku、expires、quantity、held，held不得大于库存。orders最多20000个唯一id、sku、priority(小优先)、quantity。库存和需求均为0..10^9整数，不存在负库存或重复订单事件。有效期含到期日：expires>=as_of才可分配；早于as_of的过期库存不参与。
先按priority、order id排序；同优先组按order id确定顺序，不做比例分配。每单按FEFO选择同SKU可用批次（expires升序、lot id同日打破），held库存先排除；输出订单逐lot分配、短缺、批次剩余可售量、过期与冻结量。可用库存守恒为初始未过期未冻结=分配+剩余。
样例L1可售6、L2可售10、L0过期；O1取L1的6和L2的3，O2取L2余7、履约7并短缺1。
不做多日未来需求、温度暴露、替代SKU、拣货路由、订单取消或真实库存写入。到期日解释、冻结标记与优先政策应由调用方确认；输出是单次计划快照。

