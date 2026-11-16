# Perishable lot fulfillment v1

仓配分析人员将单日需求分配到可售批次。as_of为ISO日；lots最多20000个唯一lot id、sku、expires、quantity、held，held不得大于库存。orders最多20000个唯一id、sku、priority(小优先)、quantity。库存和需求均为0..10^9整数，不存在负库存或重复订单事件。有效期含到期日：expires>=as_of才可分配；早于as_of的过期库存不参与。
先按priority、order id排序；同优先组按order id确定顺序，不做比例分配。每单按FEFO选择同SKU可用批次（expires升序、lot id同日打破），held库存先排除；输出订单逐lot分配、短缺、批次剩余可售量、过期与冻结量。可用库存守恒为初始未过期未冻结=分配+剩余。
样例L1可售6、L2可售10、L0过期；O1取L1的6和L2的3，O2取L2余7、履约7并短缺1。
不做多日未来需求、温度暴露、替代SKU、拣货路由、订单取消或真实库存写入。到期日解释、冻结标记与优先政策应由调用方确认；输出是单次计划快照。
