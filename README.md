# 多排序索引的阈值TopK取数

## 任务

同一批对象的两个属性各有降序索引，报表要按正整数加权总分取前K名，不能先把所有对象读完才排序。用Python 3.11实现query客户端，使用给定真实HTTP索引服务的sorted scan与按ID lookup，接口和访问限制见 `fixtures/protocol.md`。服务不提供总分排序；客户端不直接读取服务端 `rows.json` 或验收 `__inspect`。

## 交付物

- `query_client.py` — 客户端库 + 单查询CLI（阈值算法实现）
- `run_queries.py` — 批量运行 `fixtures/queries.json` 并输出报告与请求计数
- `acceptance.py` — 验收脚本（独立全量排序核对、page_size扫描、并列裁决、边界、快照失效）
- 本README

## 算法（Fagin阈值算法TA的精确整数版）

1. **固定snapshot**：`GET /info` 取得snapshot与对象数，之后每个请求都携带该snapshot，每个响应都核对snapshot一致；请求被拒（409）或响应snapshot不符，本次查询立即失败，不发布结果，绝不把两个快照的分数组合。客户端不做悄悄重试——重试就是全新一次查询（重新钉snapshot、清空候选）。
2. **确定策略推进**：x/y两路索引严格轮询（round-robin），每次扫一页（page_size 1..3），各自保留游标、最后读到的得分、exhausted标志。不做成本最优调度。
3. **随机访问补齐**：某id首次在任一路扫描中出现时，用一次 `/lookup`（每请求至多4个id，一页≤3个故一批即可）补齐另一列，缓存完整 `(x, y)`；另一路再遇到它时直接跳过——不重复候选、不重复lookup。并用扫描到的该列得分与lookup结果交叉核对，不一致即失败。
4. **未见对象上界**：两路已扫描前缀之外的得分不超过各自最后得分，故完全未见对象总分 ≤ `T = wx*last_x + wy*last_y`（精确整数）。
5. **停止条件**：已有≥K个完整候选且第K名总分**严格大于**T才停止；第K名与T同分时，未见对象可能同分且ID更小，保守继续读（报告中记录每次tie-continue）。某一路耗尽（或两路前缀已覆盖全部对象）时所有对象都已是完整候选，排名即最终，直接停止。某路尚未读到首项时T未定义（**不填0**），不能停止；空数据首次扫描即耗尽，干净停止。K大于对象数时返回全部（只能等耗尽/全覆盖停止）。
6. **排序**：总分降序，同分按ID的UTF-8字节序升序（与服务端同一键）。

每次停止的报告包含：已见集合、两路前沿（游标/最后得分/是否耗尽）、阈值T、第K名依据（id、总分、与T的比较）。每个命中输出两列原分数与总分；请求日志记录每次scan/lookup及其返回项。

## 运行

```bash
# 启动服务
python3 index_server.py --rows fixtures/rows.json --ready /tmp/index-ready.json
BASE=$(python3 -c "import json;print(json.load(open('/tmp/index-ready.json'))['base_url'])")

# 批量跑 fixtures/queries.json（打印命中、停止理由、请求计数）
python3 run_queries.py --base-url $BASE

# 单查询CLI
python3 query_client.py --base-url $BASE --k 2 --wx 3 --wy 1 --page-size 2

# 验收（自行起停服务，含change-after快照失效测试；仅验收读rows.json/__inspect）
python3 acceptance.py
```

## 实测请求计数（fixtures/queries.json，8个对象）

| query | k | page_size | scan请求 | lookup请求 | 接口返回项数 | 两索引全扫项数(2N) | 停止方式 | 停止时前沿 |
|-------|---|-----------|---------|-----------|-------------|-------------------|----------|-----------|
| top2  | 2 | 2 | 5 | 3 | 16（scan 10 + lookup 6） | 16 | threshold（第2名365 > T=119） | x=6/8, y=4/8 |
| top3  | 3 | 1 | 9 | 5 | 14（scan 9 + lookup 5） | 16 | threshold（第3名185 > T=105） | x=5/8, y=4/8 |
| all   | 20| 3 | 5 | 3 | 22（scan 14 + lookup 8） | 16 | index_exhausted（x耗尽，返回全部8个） | x=8/8, y=6/8 |

- top2/top3 都在**两路索引均未读完**时按阈值正确停止：top2 的索引扫描项 10 < 16（两路全扫），减少的是接口实际返回的扫描项数（请求日志逐条来自真实HTTP响应，非估算）。
- top2 过程中三次出现第K名与上界同分（365 == T=365），均保守继续读取，直到 x 前沿降到 8 使 T=119 才停止——同分时不忽略潜在更小ID。
- 并列裁决：b/c/d 同分365（权重3/1）按ID字节序出 b,c,d；all 中 e/f 同分32（权重1/2）e 在 f 前。
- all（k=20 > 8个对象）返回全部，只能等 x 路耗尽后停止。

## 验收结果（acceptance.py，全部通过）

1. 三个fixture查询的命中与独立读取 rows.json 的全量排序完全一致；
2. 每个查询在 page_size=1/2/3 下命中完全相同；
3. 同一查询跑两遍，命中与请求日志逐字节一致（确定策略）；
4. top2 以 threshold 停止且两路均未读完、扫描项 10 < 16；
5. b/c/d、e/f 同分均按ID字节序裁决；k>对象数返回全部；
6. 边界：空数据（0次扫描即停，返回空）、单对象（k>1返回该对象）；
7. `--change-after 2` 服务：第3次数据访问被拒（409），查询失败、**不发布成功结果**，服务端确认快照已切换且仅记录了2次成功数据访问。

## 明确不做的

近似搜索、任意模型打分、普通全量外排序、成本最优调度、持久续跑、分布式索引。
