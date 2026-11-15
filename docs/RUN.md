# 运行说明

环境：Python 3.11，仅用标准库（`sqlite3`、`hashlib`、`json` 等），无第三方
依赖。SQLite 需支持 `WITHOUT ROWID`（3.8.2+，本机 3.40 已满足）。

## 目录

```
orderkey/
  codec.py       # encode/decode/compile_prefix/matches + 输入校验
  store.py       # Store.build/open/scan，SQLite BLOB 联合主键索引、meta 持久化
  reference.py   # 独立参考排序（按声明逐列 Python 比较，仅测试用）
  cli.py         # 命令行 build / scan
tests/
  demo.py                # fixtures 全顺序 + 四个前缀查询 + 重开核对
  test_extra.py          # desc 文本短/长串、边界值、非法输入、successor 边界
  test_byte_robustness.py# 0x02 负载、截断拒绝、极值、完整 tuple 单点区间
  test_random_enum.py    # 随机小数据枚举：无遗漏/无混入/窄前缀收窄
  run_all.py             # 零依赖统一入口
fixtures/                # 任务给定输入
docs/LAYOUT.md           # 编码与存储布局的完整规范
```

## 一键自检

```bash
python3 -m tests.run_all
```

内容包括：

1. 给定样例端到端核对（全顺序、四个前缀查询、重开还原、窄前缀读取量）；
2. desc 文本短串/长串（含 NULL、空串、内嵌零字节、多字节 UTF-8）；
3. 字节级健壮性与非法输入拒绝；
4. 1050 行笛卡尔积（含 5 个重复业务键）上枚举全部等值前缀与合法
   `starts_with`，与 Python 全量过滤逐行比对，并断言每个窄前缀的实际读取
   键数严格小于全表。

若环境装有 pytest，也可直接 `python3 -m pytest tests/ -q`；否则
`tests/_pytest_shim.py` 提供等价的零依赖执行器。

## 命令行

```bash
# 构建
python3 -m orderkey.cli build \
  --schema fixtures/schema.json \
  --rows   fixtures/rows.jsonl \
  --db     /tmp/demo.db

# 前 k 列等值前缀（JSON 数组；null 用 null）
python3 -m orderkey.cli scan --db /tmp/demo.db --equals '["A", 5]'
python3 -m orderkey.cli scan --db /tmp/demo.db --equals '[null]'

# 起始 text 列 starts_with（可单独使用，或接在等值前缀之后）
python3 -m orderkey.cli scan --db /tmp/demo.db --starts-with A
```

每次 scan 都会打印：实际字节界 `[lower, upper)`（`None` 表示无穷上界）、
执行的 SQL、`EXPLAIN QUERY PLAN`、读取键数与返回键数、解码后的命中行。

## 作为库使用

```python
from orderkey import Store, encode, decode, reference_sort

store = Store.build("index.db", schema, rows)   # rows 为 dict 列表，含 row_id
result = store.scan(equals_prefix=["A", 5])     # 或 store.scan([], starts_with="A")
print(result.describe_bounds(), result.keys_read, result.keys_returned)

store.close()
store = Store.open("index.db")                  # 不传 schema：从库内 meta 还原
blob = encode(schema, {"tenant": "A", "score": 5, "tag": {"hex": "00"}})
key = decode(store.schema, blob)                # 仅凭 BLOB 还原业务键
```

## 给定样例的期望结果

全顺序（逐列声明比较，row_id 决胜）：

```
r1 < r3 < r8 < r4 < r2 < r5 < r6 < r7
```

四个前缀查询：

| 查询 | 字节界（hex，半开） | 命中（按输出序） |
| --- | --- | --- |
| `tenant = "A"` | `[024100, 024101)` | r3, r8, r4, r2 |
| `tenant="A", score=5` | `[024100fd7ffffffffffffffa, 024100fd7ffffffffffffffb)` | r3, r8, r4 |
| `tenant starts_with "A"` | `[0241, 0242)` | r3, r8, r4, r2, r5, r6 |
| `tenant IS NULL` | `[00, 01)` | r1 |

注意第二个查询里 desc int64 列的前缀**不带任何字段终止符**（int 定长），
而第三个 text 前缀 `0241` 同样**不含**变长终止符 `00`；它的上界由
successor 规则给出（`0242`），不是追加一个不存在的“最大字符”。
