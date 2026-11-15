# 主从数据读取的会话书签与因果可见性

数据报表优先从延迟同步的副本读取，但刚写入后同一客户不能读到自己已确认提交之前的状态。本目录交付一个 Python 3.11 + SQLite 实现的持久化会话书签及读取路由组件。

**保证**（仅这一条）：按客户读己之写 —— 客户不会读到比自己 observe 过的回执更旧的快照。
**不承诺**：全局强一致、一定读到最新数据、复制协议、故障选主、网络代理、分布式服务。

## 文件

| 文件 | 说明 |
|---|---|
| `router.py` | 交付组件：`observe` / `read_report` / `get_bookmark` / CLI |
| `replica_fixture.py` | 任务给定的数据源设施（init / commit / replicate / reset-node），不做路由 |
| `run_scenarios.py` | 按 `fixtures/scenarios.json` 运行全部场景并记录证据 |
| `test_router.py` | 19 个单元测试 |
| `fixtures/read-report.json` | 报表定义：accounts 查询 + history 汇总查询 |
| `records/latest/` | 最近一次场景运行记录：`log.json`、`log.md`、`data/`（含持久书签 `bookmarks.db`） |

## 快速开始

```bash
ROOT=/tmp/demo
python3 replica_fixture.py --root $ROOT init            # 先 init，生成 cluster.json
python3 replica_fixture.py --root $ROOT commit --amount 25
# => {"cluster_id": "demo-cluster", "generation": 1, "commit_seq": 1}

python3 router.py --root $ROOT observe --client A \
    --receipt '{"cluster_id":"demo-cluster","generation":1,"commit_seq":1}'
python3 router.py --root $ROOT read --client A --wait-ms 100 --fallback none
# 副本未同步 => 重试约 100ms 后明确返回 not_ready（不带任何旧数据）
python3 router.py --root $ROOT read --client A --wait-ms 100 --fallback primary
# => "node": "primary"，读到 applied_seq=1 的快照
python3 replica_fixture.py --root $ROOT replicate
python3 router.py --root $ROOT read --client A --wait-ms 100 --fallback none
# => "node": "replica"，同步后回到副本
python3 router.py --root $ROOT bookmark --client A      # 查看持久书签
```

`observe` 的回执也可用 `--receipt-file PATH`（`-` 表示 stdin）传入。

### 退出码

| 命令 | 退出码 |
|---|---|
| `observe` / `bookmark` | 0 成功；2 校验或用法错误（`cluster_mismatch`、`generation_mismatch`、`invalid_seq` 等，书签不被改动） |
| `read` | 0：`ok` 或 `not_ready`（正常路由结果）；1：`identity_mismatch`（明确错误）；2：用法/环境错误 |

## 语义

### observe(client_id, receipt)

- 只接受 `cluster.json` 中本集群当前代际、`commit_seq` 为非负整数的回执（布尔、浮点、字符串、负数一律拒绝）。
- 书签取历史最大值：重复或乱序回执不倒退；客户间状态隔离；落盘于 `<root>/bookmarks.db`，重开保留。
- 没有回执的客户书签为 0。错误集群或代际明确报错，不清零书签，也不把别的客户的最高序号算到本客户头上。

### read_report(client_id, wait_ms, fallback)

1. 先尝试副本。**身份检查（cluster_id/generation）、applied_seq ≥ 书签检查、accounts 查询、history 汇总查询，全部在同一个 `BEGIN` 读事务快照内完成**（`router.py` 的 `_attempt`）——不存在"先独立查进度、再开另一个事务取数"的路径；两查询之间发生同步或主库写入，也不会拼出混合时刻（WAL 读快照隔离）。
2. 副本进度不足时按单调时钟（`time.monotonic`）在 `wait_ms` 内重试；每次先关闭旧快照（ROLLBACK+关闭连接）再开新快照，不会卡在看不见新进度的旧事务上。
3. 超时后 `fallback=primary`：对主库重新开启并同样验证的读快照；`fallback=none`：返回明确的 `not_ready`，不悄悄返回旧数据（结果中没有 `accounts`/`history` 字段）。
4. 任一节点身份不符立即返回 `identity_mismatch`（含实际快照 meta 与期望身份），**不重试、不隐式回退**，即使 `fallback=primary` 可用。
5. 输出：选中节点、所需书签 `required_seq`、实际快照 `snapshot_meta`、两查询结果、尝试次数 `attempts`、耗时 `elapsed_ms`。

### 测试钩子（不绕过正式 read_report）

`router.Hooks` 从 `read_report` 真实控制流中触发，用于构造确定性的交错时序：

- `after_node_selected(node)`：选定节点之后、`BEGIN` 之前（用于注入 reset-node 改代际）；
- `after_accounts()`：读事务内 accounts 查询与 history 查询之间（用于注入 commit+replicate）。

同步回调即线程屏障的确定性等价物：钩子执行时读事务真实保持打开，无需多线程。

## 运行记录

```bash
python3 run_scenarios.py     # 重建 records/latest/{log.json,log.md,data/}
python3 test_router.py -v    # 19 个单元测试
```

`records/latest` 中 26/26 检查通过，要点：

- **lag_and_clients**：主库提交 25、7（seq 1、2），副本未同步。A observe 第二张回执（书签=2，经独立进程 CLI 验证持久）；A 读等待 ≥100ms、重试 7 次后回退主库读到 a=568/b=432；B 无回执（required_seq=0）一次读旧副本 a=600/b=400；B 书签始终为 0，互不污染。`replicate` 后 A 一次命中副本。
- **observe_validation**：乱序回执不倒退（bookmark 保持 2）；错误集群/代际/负序号均 exit 2 明确报错且书签不变。
- **snapshot**：A 的读事务内、两查询之间真实执行了 commit(3)+replicate（钩子记录回执 seq=3），报表仍完整来自 seq=2 快照（meta/accounts/history 一致，满足 read-report.json 不变式）；读结束后独立查证副本确已到 seq=3，证明竞态真实发生。
- **generation**：选定副本后、`BEGIN` 前 reset 副本代际为 2；真实快照内身份校验返回 `identity_mismatch`，attempts=1、耗时 ~1ms（未当普通延迟重试），即使 `fallback=primary` 可用也不隐式回退。

## 范围

仅两个本地 SQLite 节点、两个客户、两个固定查询（来自 `read-report.json`）。不实现复制协议、数据库故障选主、网络代理或分布式服务；不承诺全局强一致或一定读到最新数据。
