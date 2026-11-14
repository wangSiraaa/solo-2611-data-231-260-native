# 物理排序性质驱动的ETL执行计划

本目录仅含任务输入与可运行测试设施，不含待实现的处理器。

某些ETL算子只有输入已经按分组键连续排列时才能边读边聚合，盲信上游sorted标签会把同组拆开，而每步都排序又浪费已有顺序。请用Python 3.11实现plan和run，读取sources.json与plans.json，只支持source、稳定filter、project改名/删列、顺序concat和stream_group。输入小JSONL实际数据已提供，不解析SQL或任意表达式。
source声明一个全局排序键列表，各项含asc/desc及NULLS规则；运行读取时必须核验真实数据符合声明。filter不重排；project只能沿原顺序保留并改名，遇到被删的前导排序列不能声称后续列仍全局有序。concat按输入顺序连接，各输入内部有序还不够，需核对相邻非空输入的末/首边界在同一排序键下不倒退；不能只因两个子节点order字段相同就继承。
stream_group要求其keys按声明顺序ASC、NULLS FIRST作为输入排序前缀；键相同的行一次形成一组，输出COUNT(*)及指定整数列SUM，全NULL组SUM为NULL，NULL键可分组。规划器依据可证明的物理顺序，为未满足条件的节点插入一个明确Sort；已有充分顺序时不得无条件再排。输出各节点推导顺序、证据或失效原因，以及新增Sort的位置和键，不求全局成本最优。
执行计划时保留真正的Sort调用记录和输入/输出行数，不能先统一排序全部输入再伪造零排序计划。可用Python稳定排序实现插入节点；stream_group按连续组消费，不在内部悄悄用hash聚合掩盖错误前置顺序。分组输出继续保留可证明的键顺序，空源和全部过滤掉的输入也应正确处理。
现成案例分别覆盖已有顺序拼接、边界交错、删掉前导键和保序改名后过滤。与独立不依赖顺序的字典分组逐组核对结果，再检查前两类是否分别零Sort和实际插入Sort；篡改source行序应因虚假声明失败。交付源码、规范执行计划及记录，不做join优化、缓存、并行调度或大压测。

输入文件：
- `fixtures/sources.json`
- `fixtures/plans.json`
- `fixtures/left.jsonl`
- `fixtures/right.jsonl`
- `fixtures/overlap.jsonl`

环境：Python 3.11；全部业务数据为本地合成材料。
