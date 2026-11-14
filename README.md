# decompose.py — 依赖保持的 3NF 分解小工具

读取 `schema.json`（属性顺序、**业务声明**的函数依赖）与 CSV 数据，
产出分解方案、实际关系表（SQLite）和可重建的自然连接查询。

**依赖是业务声明**：工具只用声明依赖做推理，绝不从样例数据猜测额外约束，
也不把样例中偶然成立的关系当作证明。

## 范围约定

- 最多 6 个**非空字符串**列；NULL/空串不在范围内（出现即拒绝）。
- 关系采用集合语义：CSV 中重复完整元组直接拒绝。
- 可枚举属性子集求闭包（2^6 规模）；**不**寻找全部最优分解、**不**做 BCNF 最小表数搜索。
- 不做在线迁移、兼容写视图、理论证明论文。

## 用法

```bash
python3 decompose.py fixtures/schema.json --out out     # 主例（CSV 缺省取 schema 旁的 records.csv）
python3 decompose.py fixtures/schema.json --csv fixtures/invalid.csv   # 违例 → 拒绝发布
python3 selftest.py                                     # 端到端自检（26 项）
```

- `schema.json` 可含 `relation`（关系名，缺省 `wide_relation`）、`csv`（相对路径，
  缺省为 schema 旁的 `records.csv`，也可用 `--csv` 覆盖）、`attribute_order`
  （若给出，必须是 `attributes` 的排列，作为规范属性顺序）；其余说明性键原样忽略。
- 退出码：`0` 成功；`1` 规格错误或违反声明依赖（**拒绝发布**，不产生任何输出）；
  `2` 重建核对失败。

## 处理流程

1. **输入检查**：逐条用声明依赖检查 CSV。违例时给出决定列值与两条矛盾源行
   （逻辑行号从 1 起、不计表头，附完整行内容），并拒绝发布。
   例如 `fixtures/invalid.csv` 中 `employee = e1` 的第 1、2 行 `name` 分别为
   `Ann`/`Different`，违反 `employee -> name`。
2. **最小覆盖**：右部拆成单属性 → 按属性顺序反复消除多余左部属性与冗余依赖
   直到不动点。每次消除都在 `plan.json` 的 `minimal_cover.steps` 中记录
   **闭包依据**（用哪些依赖、按什么顺序把右部推出来）。
3. **3NF 合成**：按相同左部合并属性形成关系；删除被其他关系完整包含的关系；
   若没有任何关系含原模式一个候选键，补入按（属性数、再属性顺序）最小的候选键关系。
4. **推导展示**：`dependency_derivations` 给出每条原声明依赖如何由
   **投影到各关系的依赖**（极小左部）的闭包共同推出。
5. **落库与核对**：从真实输入投影去重载入各 SQLite 表
   （`TEXT NOT NULL` 列、候选键作主键/唯一约束、稳定表名 `rel_1..rel_n`）；
   生成并**实际执行** `rebuild.sql` 自然连接查询，逐完整元组与原关系核对。

## 输出文件（`out/`）

| 文件 | 内容 |
|---|---|
| `plan.json` | 分解方案：最小覆盖及闭包依据、各关系属性与可作为键的决定集合、每条声明依赖的推导、重建核对结果 |
| `decomposed.db` | SQLite 库，只含分解后的关系表（**不**保存另一份完整宽表作为重建答案） |
| `rebuild.sql` | 自然连接重建查询（显式列清单 + 全属性 ORDER BY，结果确定） |
| `lineage.json` | 来源映射：每个投影元组对应的原逻辑行号；仅供追溯，**不参与**补造重建结果 |

## 无损性与依赖保持的说明

方案采用**依赖保持的 3NF 构造**：合成保持全部声明依赖，且某一关系包含原模式候选键，
因此对所有满足声明依赖的关系（不止当前这份数据）连接无损——这是构造性保证。
工具对当前数据执行的重建核对只是 sanity check，
**不能只因当前数据连接回来相等就宣称未来所有合法关系都无损**。

## 顺序无关性

一切决策按规范**属性顺序**进行（声明依赖重编号为 FD1..、最小覆盖编号 m1..、
关系命名 rel_1.. 均为规范顺序）。更换 CSV 行序或依赖列出顺序，
`plan.json`（除 `source` 文件名外）与数据库内容不变——由 `selftest.py` 第 3 组检验固化。

## 主例（fixtures/）

声明依赖（规范化重编号后）：

- `FD1: employee -> name, department`
- `FD2: employee, skill -> name`（给定冗余依赖：左部 `skill` 多余，化简为
  `employee -> name` 后与 FD1 拆分项重复而消除，闭包依据见 plan.json）
- `FD3: employee, skill -> rating`（复合技能键 `(employee, skill)` 即原模式候选键）
- `FD4: department -> office`（跨部门传递：`employee -> department -> office`）

分解结果：

| 表 | 属性 | 键 |
|---|---|---|
| `rel_1` | employee, name, department | (employee) |
| `rel_2` | employee, skill, rating | (employee, skill) |
| `rel_3` | department, office | (department) |

`rel_2` 含候选键 `(employee, skill)`，无需补键关系；
`rel_1 ⋈ rel_2 ⋈ rel_3` 重建出 4 个元组，与原关系逐完整元组一致。

## 小例（examples/）

- `no_nontrivial/`：只有平凡依赖 `code -> code`，最小覆盖为空，
  无任何合成关系，补入全属性候选键关系 `(code, label, note)`。
- `needs_key/`：`a -> b` 合成出 `(a, b)`；候选键 `(a, c)` 未被任何关系包含，
  补入键关系 `(a, c)`。

## 文件清单

```
decompose.py    工具本体（仅标准库，Python 3.11）
selftest.py     端到端自检（26 项断言）
fixtures/       给定输入（schema.json, records.csv, invalid.csv）
examples/       两个小例（无非平凡依赖 / 需要补键关系）
out/            主例产物（plan.json, decomposed.db, rebuild.sql, lineage.json）
```
