# 实现说明：物理排序性质驱动的 ETL plan/run

仅使用 Python 3.11 标准库。支持算子：`source`、稳定 `filter`、`project`（改名/删列）、
顺序 `concat`、`stream_group`，以及规划器按需注入的显式 `sort` 节点。
不解析 SQL、不支持任意表达式，不做 join、缓存、并行调度或成本优化。

## 运行方式

```bash
python -m etl all                       # 默认读取 fixtures/，产出 artifacts/
python -m etl --sources X --plans Y plan
python -m etl --sources X --plans Y --out DIR run
python -m unittest discover -s tests    # 24 个测试
```

全局选项需放在子命令之前。

## 模块划分

| 文件 | 职责 |
| --- | --- |
| `etl/ordering.py` | 排序描述符、NULL 感知比较、Python 稳定排序、前缀充分性判定、连续组顺序守卫 |
| `etl/catalog.py` | 读取 sources.json 与 JSONL，schema 类型核验 + 声明顺序逐相邻行核验 |
| `etl/ops.py` | 稳定 filter / project / concat / 连续组 stream_group 纯行运算，无哈希聚合 |
| `etl/planner.py` | 自底向上推导每个节点的可证明物理顺序，仅在证据不足时注入一个 Sort |
| `etl/runner.py` | 按规范计划真实执行，逐节点记录输入/输出行数与真实 Sort 调用 |
| `tests/` | 四个现成案例验收 + 边界/安全性质测试 + 独立字典分组对照 |

## 顺序推导规则（规划器）

1. **source**：声明的全局排序键必须经真实数据逐相邻行核验通过才采信；
   虚假声明在规划和执行两处都会失败（`OrderingError`）。
2. **filter**：稳定不过滤重排，原样继承输入描述符；输入无序则自身无序。
3. **project**：输出列严格按来源列原顺序排列（mapping 的键序不决定列序）。
   从输入描述符首键开始逐键映射改名；一旦遇到被删除的前导键立即截断，
   后续列不得再声称全局有序，并写明失效原因。
4. **concat**：不能仅凭两个子节点 order 字段相同就继承。要求所有非空输入
   描述符逐键一致，**并**核对相邻非空输入的末行/首行在同一描述符下非递减；
   空输入跳过。任一条件不满足则描述符置空并给出失效原因，顺序责任留给下游 Sort。
5. **stream_group**：要求 keys 按声明顺序 `ASC + NULLS FIRST` 构成输入排序前缀。
   用 `satisfies_prefix` 判定输入可证明次序是否覆盖该前缀（允许输入带更细的后续键）。
   满足则零 Sort；不满足则在该节点输入处插入一个显式稳定 Sort，键即分组前缀。
   分组输出天然按这些键有序，描述符继续保留。

## 执行与防伪造

- 计划中的 `sort` 节点是唯一会调用排序的地方（`sorted` + 比较器，稳定性由 Python 保证），
  每次调用在执行记录里留下 `sort_call`（键、稳定性、输入/输出行数）。
- 规划期探针物化可能对小数据做排序用于推导，但**不产生任何 Sort 记录**；
  真实排序只发生在 runner 执行注入节点时，因此“零 Sort 计划”里不可能藏排序。
- `stream_group` 只按连续相等键消费，并再次核验 ASC/NULLS FIRST 不倒退；
  同键行被拆开时直接抛 `ExecutionError`，不用哈希聚合掩盖错误前置顺序。
- `concat` 执行期只对规划器标记 `proven` 的节点复核末/首边界；
  `unproven` 节点的顺序责任已显式转移给下游 Sort。
- SUM 为 SQL 语义：NULL 忽略；整组全 NULL 时 SUM 为 NULL；COUNT 照常计数；
  NULL 键可分组，ASC/NULLS FIRST 下排在最前。

## 四个现成案例的结论

| 计划 | 场景 | 注入 Sort 数 | 关键证据 |
| --- | --- | --- | --- |
| `already_grouped` | 已有顺序拼接（A 区 → B 区边界非递减） | 0 | concat 边界证据 + 前缀匹配 |
| `crossing` | 边界交错（(A,3) 后接 (A,2)） | 1 | concat 失效原因明确，Sort 键 `region,tick` |
| `drop_leading` | project 删掉前导键 region | 1 | project 描述符截断为空，Sort 键 `t` |
| `rename` | 保序改名后稳定 filter | 0 | 改名后描述符随列保留，filter 继承 |

## 交付物（`artifacts/`）

- `plans/<id>.plan.json`：规范执行计划——各节点推导顺序、证据/失效原因、schema、
  注入 Sort 的位置与键，以及可直接执行的 `executable_plan`。
- `runs/<id>.run.json`：执行记录——逐节点输入/输出行数、真实 Sort 调用、
  边界复核明细与最终输出行。
- `plan_summary.json` / `run_summary.json`：四个计划的 Sort 数量汇总。
