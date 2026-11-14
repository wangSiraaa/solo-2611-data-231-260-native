# 主从数据读取的会话书签与因果可见性

本目录仅含任务输入与可运行测试设施，不含待实现的处理器。

数据报表优先从延迟同步的副本读取，但刚写入后同一客户不能读到自己已确认提交之前的状态。请用Python 3.11和SQLite实现持久化会话书签及读取路由组件，提供observe(client_id,receipt)、read_report(client_id,wait_ms,fallback)和命令行入口。已有replica_fixture.py可创建真实primary/replica数据库、在主库提交转账、手动同步副本和重置单节点代际；它只模拟数据源，不替你完成路由。先init，再用cluster.json作为预期cluster_id/generation。
写入回执含cluster_id、generation、commit_seq。observe只接受本集群当前代际和非负整数序号；同一客户书签取最大值，重复或乱序回执不能倒退，客户间状态隔离，重开仍保留。没有回执的客户从序号0开始。错误集群或代际必须报错，不悄悄清零书签，也不把别的客户的最高序号当本客户要求。
read_report先尝试副本，仅当读取快照内meta的applied_seq达到该客户书签，且cluster/generation一致，才能接受其数据。身份检查和applied_seq检查必须在真正执行报表的同一BEGIN读事务快照中完成：不能先独立查进度，再开启另一个事务取业务数据。报表由read-report.json中的accounts查询和history汇总查询共同组成，二者及meta来自同一快照；两次查询之间恰有同步或主库写入，也不得拼出混合时刻。
副本进度不足时，用单调时钟在wait_ms界内重试，每次先结束旧快照再开启新快照，避免一直等在看不见新进度的旧事务上。到时fallback=primary则对主库重新开启并验证满足条件的读快照；fallback=none返回明确not_ready，不能悄悄返回旧数据。节点身份不符属于明确identity_mismatch错误，不视作普通延迟进行隐式回退。输出选中节点、所需书签、实际快照meta、两查询结果及尝试次数；不承诺全局强一致或一定读到最新数据。
运行给定场景：主库提交两次、副本未同步，客户A观察新回执而B没有；证明A等待/回退、B可以读旧副本，不能互相污染。同步后A应回到副本。加测试钩子或可控制的线程屏障，分别在选定节点但开始事务之前重置代际，以及同一读事务两查询之间执行写入/同步，验证身份在真实快照内检查且报表未混合。测试设施不得绕过正式read_report。交付源码、持久书签、实际运行记录和README。范围仅两个本地SQLite节点、两个客户、两个固定查询；不实现复制协议、数据库故障选主、网络代理或分布式服务。

输入文件：
- `replica_fixture.py`
- `fixtures/scenarios.json`
- `fixtures/read-report.json`

环境：Python 3.11；全部业务数据为本地合成材料。
