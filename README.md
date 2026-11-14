# 结果封顶搜索接口的完整分区抽取

实现：`extract.py`（Python 3.11，仅标准库）。验收：`qa/run_scenarios.py`、`qa/test_adversarial.py`。

## 运行

```sh
# 1) 启动冻结快照输入服务（fixtures/README.md 的命令）
python3 fixtures/mock_search.py --port 8231 \
  --request-log /tmp/search-231-requests.jsonl \
  --truth-log /tmp/search-231-truth.jsonl

# 2) 打开、抽取、复核（每个场景一个输出目录）
python3 extract.py open    --base-url http://127.0.0.1:8231 --scenario normal --out-dir out/normal
python3 extract.py collect --base-url http://127.0.0.1:8231 --out-dir out/normal
python3 extract.py inspect --out-dir out/normal

# 也可以用任务配置（命令行参数优先）：
python3 extract.py open    --config task.json
python3 extract.py collect --config task.json
python3 extract.py inspect --config task.json
```

`task.json` 可含 `base_url`、`scenario`、`out_dir`、以及可选的 `cap`/`t_range`/`bucket_range` 期望值；与 `/open` 回显不一致会直接失败，不会被忽略。也可用 fixtures/config.json 的形状。

```sh
python3 qa/run_scenarios.py     # normal / hotspot / saturated / expired 端到端验收
python3 qa/test_adversarial.py  # 服务撒谎、漂移、超时等故障注入
```

退出码：`0` 完整；`2` 未完整（saturated、失效、错误、或一致性损坏）；`64` 用法错误。

## 采集规则

* 工具只调用 `/open` 与 `/search`，从不读取 `scenarios.json`、真值日志或请求日志。
* 从 `/open` 返回的整个二维域开始，维护一棵拆分树。存在拆分规则：
  * **时间优先的整数中点二分**：`t` 区间多于一个整数时按中点劈开 `t`；`t` 已成单值后才按中点劈开 `bucket`。
  * 每个父节点恰好有两个子节点，两个子区间**不相交且并集恰好等于父区间**（`tree.json` 中保存区间、请求、回显与子指针，可回查）。
  * 一个格子（单 `t`、单 `bucket`）是唯一允许“有行但不能再分”的终态。
* `count <= cap` 才接受为叶，此时服务返回的行即该叶的完整内容。接受前逐项校验：
  * 回显的 `snapshot_id`、`t_range`、`bucket_range`、`cap` 与请求一致；
  * `count` 为非负整数；`count <= cap` 时行数必须等于 `count`，`count > cap` 时行数不超过 `cap`；
  * 每行字段集合与类型（`record_id:str`、`t:int`、`bucket:int`、`payload:str`）精确匹配，无多余字段；
  * 每行坐标落在本节点区间内；叶内 `record_id` 不重复；**同一坐标允许多个不同 `record_id`，绝不去重**；
  * 跨叶 `record_id` 全局唯一，接受叶行总数 = 叶 `count` 之和 = 根 `count`。
* 父 `count` 必须等于两个子 `count` 之和；每个接受叶都有对应实际响应作为依据（`queries.jsonl` 留存原始请求/响应）。
* 单格 `count > cap` 标为 **saturated**，记录 `count`、可见行数与未取全条数；整次结果 `complete=false`，可见行只进入 `records.jsonl.partial`，不会伪装成全量成功。
* 任何致命错误（连接失败、HTTP 4xx/410、回显不一致、字段/行位/唯一性违规、计数不守恒）都中止该快照并保留已得叶与诊断，`complete=false`。**不会**只报成功。
* 所有 `/search` 只带同一个 `snapshot_id`；`snapshot` 失效（410）即整次不可完整发布，不会重新打开并把新旧两半拼在一起。需要时可显式重开新任务（重新 `open` 到新目录），不做断点续采。

## 产物（每个输出目录）

| 文件 | 内容 |
| --- | --- |
| `open.json` | `/open` 的 snapshot 身份、cap、整个二维域、打开时间 |
| `tree.json` | 完整拆分树：每节点区间、状态（accepted/saturated/internal/failed/pending）、`count`、可见 `rows`、子节点、父指针、错误 |
| `queries.jsonl` | 每次 `/search` 的原始请求、HTTP 状态与原始响应，可从任一接受叶回查 |
| `coverage.json` | 覆盖报告：`complete`、根计数、接受叶数/行数、saturated 叶与未取全条数、查询数、错误、fatal |
| `records.jsonl` | **仅当 `complete=true`** 时发布：按 `record_id` 排序、UTF-8 JSONL、保持原始字段 |
| `records.jsonl.partial` | 未完整时的可见行（按 `record_id` 排序），不覆盖正式发布名 |

`saturated`/失效/出错时**不存在** `records.jsonl`，因此不会被误当完整结果。

## 场景结果（`qa/run_scenarios.py`，与服务端独立冻结真值核对）

| 场景 | 结果 | 覆盖与核对 |
| --- | --- | --- |
| normal | complete=true，20 行 | 叶格集合 = 整个域且不重叠；ID 多重集合 = 真值 |
| hotspot | complete=true，32 行 | `t=7` 收窄成单值后继续按 `bucket` 细分（树中可见第二维拆分）；12 条热点全部取到 |
| saturated | complete=false | `(2,2)` 单格 `count=7 > cap=4`，取得 19 行 + 可见 4 行 ≤ cap + 未取全 3 条 + 已取 19 = 26 = 根 `count`；有且仅有该 saturated 叶；无 `records.jsonl` |
| expired | complete=false | 第 4 次 `/search` 起 410；3 次成功请求的叶保留，`fatal.kind=snapshot_expired`，无 `records.jsonl` |

`inspect` 只读产物、不联网，独立复核：父子计数、两个子区间对父区间的无缝覆盖、叶两两不重叠且（完整时）覆盖整个域、接受叶行数/位置/唯一性、JSONL 行数与 `record_id` 排序、未完整时不得有正式 `records.jsonl`、coverage 计数与树一致。篡改任一产物会被报为 `ok=false`（退出码 2）。

## 故意不做

* 不做最少查询次数优化、并发平台、跨快照拼接、网页发现；仅本机小数据、两个整数维度。hotspot 的第二维细分是**证明递归能力**所需，不是性能目标。
