# 双时态维度表（bitemporal）

用 Python 3.11 标准库实现的**可回查历史认知的双时态维度表**。修订按
`known_at`（知识时刻/系统时间）严格递增到达，作用于实体的有效时间轴
（半开区间 `[from, to)`）；系统把修订真正物化为一张双时态历史关系表，
之后的任何查询都只读取历史表，不重放事件。

- **有效时间（valid time）**：事实在业务世界中生效的整数时间轴，
  `valid_to = null` 表示正无穷，允许负时间。
- **系统时间（system time）**：数据库在哪个知识时刻知道该事实，
  `sys_to = null` 表示该行当前仍开放。两个维度各自独立。
- 知识切片 `K` 的可见行满足 `sys_from ≤ K < sys_to`，它们在有效时间上
  互不重叠，相邻且值相等的区间已合并。

## 目录结构

```
bitemporal/
  model.py          纯函数：有效时间轴规范化分段（拆分/撤回/合并/值相等规则）
  store.py          SQLite 历史表物化、点查询 query()、切片 slice_at()
  importer.py       导入入口：全量校验 -> 事务物化 -> 导出 history.jsonl（含 CLI）
  query_cli.py      查询 CLI
fixtures/
  revisions.jsonl       主修订数据（c1/c2，含 assert null、撤回、无变化断言）
  queries.jsonl         主 fixture 的查询材料
  revisions_extra.jsonl 扩展修订（1 与 1.0、布尔 vs 数值、填回合并等）
  queries_extra.jsonl
  invalid.jsonl         非法区间（from == to）
  invalid_order.jsonl   知识时间非严格递增
  invalid_dup.jsonl     重复修订 id
  invalid_value.jsonl   非有限标量值（NaN）
history/
  bitemporal.db     交付的 SQLite 历史库（可独立用 SQL 查询）
  history.jsonl     交付的 JSONL 双时态关系（可脱离 SQLite 查询）
  bitemporal_extra.db / history_extra.jsonl  扩展 fixture 的同样产物
tests/
  reference_oracle.py   独立参考器：朴素逐点重放，不共享生产代码
  test_bitemporal.py    31 个测试（含有限整数网格交叉验证）
examples/
  query_examples.py     查询示例
```

## 快速开始

```bash
# 1. 物化修订（全量重建 history/bitemporal.db 与 history/history.jsonl）
python3 -m bitemporal.importer fixtures/revisions.jsonl

# 2. 点查询：query(entity, valid_at, known_at)
python3 -m bitemporal.query_cli c1 4 2
# {"found": true, "value": "south", "row": {...}}

# 3. 同时查看知识切片 K 的全部可见段
python3 -m bitemporal.query_cli c1 7 3 --slice

# 4. 运行全部测试（含参考器网格交叉验证）
python3 -m unittest tests.test_bitemporal -v

# 5. 查询示例
python3 examples/query_examples.py
```

## 导入入口与编程接口

```python
from bitemporal.importer import import_revisions
from bitemporal.store import query, slice_at

# 全量校验通过后在单事务中物化，并导出 history.jsonl
import_revisions(
    "fixtures/revisions.jsonl",
    db_path="history/bitemporal.db",
    history_path="history/history.jsonl",
)

# 点查询：直接读已物化的历史表，不重放事件
result = query("c1", valid_at=7, known_at=4, db_path="history/bitemporal.db")
# {"found": True, "value": "east",
#  "row": {"entity": "c1", "valid_from": 6, "valid_to": 8,
#          "sys_from": 4, "sys_to": None, "value": "east"}}

# 某知识切片下实体的全部可见段
rows = slice_at("c1", known_at=3, db_path="history/bitemporal.db")
```

`query()` 返回 `found / value / row`：没有事实时 `found=False`（例如被
retract 的点、或从未有事实的点）；`found=True, value=None` 则表示该点
存在一条 `assert value=null` 的事实。两者严格区分。

## 历史行物化规则（唯一性保证）

每条修订先计算该 entity 规范化后的**新完整有效区间列表**，再与旧列表做
身份比对（身份 = 边界 `[lo, hi)` 完全相同且值相等）：

1. 新旧列表中身份相同的段：保留原 `sys_from`，开放行继续开放；
2. 旧列表中消失或边界变化的段：其开放行在本次 `known_at` 关闭
   （写入 `sys_to = known_at`）；
3. 新列表中出现的新段：插入一条 `sys_from = known_at`、`sys_to = null`
   的新开放行。

因此部分覆盖会关闭并拆开旧段；而**纯无变化断言**（规范化结果与旧列表
逐段相同）不关闭任何行、不插入任何行，不产生冗余历史。

只保存存在事实的段：retract 只是把区间从规范列表中删除，**绝不**用一条
`value=null` 的行替代“不存在”。

## 值相等规则

- 数值 `1` 与 `1.0` 相等（合并相邻段时视为同值）；
- 布尔与数值永不相等（`true != 1`，`false != 0`）；
- 字符串按 Unicode 码点比较；
- `null` 只与 `null` 相等；
- 仅允许有限 JSON 数值、字符串、布尔、null；NaN/Infinity、数组、对象拒绝。

## 校验与失败语义

下列情况在**变更任何状态之前**抛出 `RevisionError`，已建库不保留半成品
（解析阶段全部通过后才开启事务；物化阶段失败则回滚，导出只在提交成功后
进行）：

- 非法有效期：`from`/`to` 非整数，或 `from >= to`（`to=null` 除外）；
- `known_at` 非严格递增；
- 修订 `id` 重复；
- `kind` 非 `assert`/`retract`，retract 携带 value，assert 缺少 value；
- value 不是允许的有限 JSON 标量。

## 独立产物如何查询

`history/history.jsonl` 每行一个双时态段，字段为
`entity, valid_from, valid_to, sys_from, sys_to, value`，两个 `to` 字段的
`null` 分别代表各自维度的正无穷。脱离 SQLite 时，点查询等价于筛选：

```text
entity 相等
且 sys_from <= K 且 (sys_to 为 null 或 sys_to > K)
且 valid_from <= t 且 (valid_to 为 null 或 valid_to > t)
```

SQLite 库可直接用标准 SQL 查询（不依赖任何时态 SQL 语法）：

```sql
SELECT * FROM history
WHERE entity = 'c1'
  AND sys_from <= 3 AND (sys_to IS NULL OR sys_to > 3)
  AND valid_from <= 7 AND (valid_to IS NULL OR valid_to > 7);
```

## 验证方式

`tests/test_bitemporal.py`（31 个测试）覆盖：

- fixtures 全部手工查询（主 fixture + 扩展 fixture）；
- **每个输入知识时刻**抽取历史切片，核对无重叠、半开区间合法、
  相邻段值不同（即相邻同值已合并）；
- 旧知识切片在后续修订到达后逐字节保持（可回查，不只验证最终当前表）；
- **独立有限整数网格参考器**（`tests/reference_oracle.py`，朴素逐点重放、
  与生产代码零共享）在 entity × 有效时间网格 × 知识时刻网格上逐点交叉
  验证，两个 fixture 合计上千个网格点；
- `history.jsonl` 脱离 SQLite 独立查询并与库结果一致；
- 四类非法输入在状态变更前失败、事务回滚不留半成品；
- 纯无变化断言不产生新历史行；开放尾段可被后续修订正确关闭。

需求点名的场景在 fixture 中的对应：

| 场景 | 位置 |
| --- | --- |
| 迟到有效期覆写 | a2 覆写 a1；查询 q1/q2 对比 |
| 覆盖后局部撤回 | a3；q3 |
| 撤回区间再填入 | a4；q4 |
| 相邻同值合并 | a7（同值重述合并）；扩展 b2（1 与 1.0）、b6 |
| 开放尾区间 | a5 `to=null`；q6 |
| 负时间 | a6 `from=-5`；c2 查询 |
| 知识时点恰等提交时刻 | 全部 q 系列，参考器网格含每个提交时刻 |
| assert null 与 retract 区别 | a6（assert null，q7 found=true）对比 a3（retract，q3 found=false） |

## 范围说明

仅用于小规模数据历史生产：不做统计预测、维度自动推断，不提供 Docker，
不承诺多进程并发与崩溃恢复，也不使用任意 SQL 时态语法（仅标准 SQL）。
