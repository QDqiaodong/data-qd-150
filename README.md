# data-qd-150 · 易腐品批次履约

FEFO lot allocation with expiry, holds and shortage reconciliation。业务输入、计算和当前边界详见 [数据契约](docs/contract.md)。

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

输入JSON最多8MB，拒绝重复键、非有限数值和未定义字段；源字节SHA256随结果保留。
输出是可独立读取的JSON；成功后原子替换，输入/计算失败退出2并保留旧报告。
输入和输出不得相同，输出符号链接拒绝。单操作者批处理，不承诺并发发布或断电恢复。
同输入重跑不累加。测试使用临时目录，运行产物在忽略的run/。

src/perishable_fulfillment/model.py 为业务模型，相关算法模块独立组织；common.py 提供受限JSON及报告发布，
cli.py 衔接数据链路。fixtures 为明确自制样例，代码与样例采用MIT许可。

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

