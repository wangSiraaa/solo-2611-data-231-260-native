# SQLite 只读表抽取器

直接从 `.sqlite` 文件的真实页读取 `table.json` 指定的普通 rowid 表，导出
`records.jsonl`（每行一条记录，含逻辑列值与逐行物理来源）。核心抽取
（`extractor.py`）只使用 Python 3.11 标准库，**不**调用 `sqlite3` 模块、
现成 SQLite 解析器或外部 sqlite 命令；`sqlite3` 仅在测试中作为独立验证
oracle 使用。

## 材料说明（随仓库提供）

`fixtures/source.sqlite` 为标准 sqlite3 创建的 UTF-8 数据库，page_size=512，
`records` 为普通 rowid 表。包含负/零/大 rowid、NULL/空字符串/空 BLOB、
Unicode 和跨 overflow 页的 1500 字节 BLOB；行数足以形成内部表页。
`table.json` 给出已确认的 rootpage 与列序，无需查询 sqlite_schema。
`fixtures/truncated.sqlite` 与 `bad-table.json` 分别用于验证对不完整页和
越界根页的拒绝。禁止修改输入。

## 文件清单

| 文件 | 说明 |
| --- | --- |
| `extractor.py` | 核心抽取器 + CLI（仅标准库，不 import sqlite3） |
| `test_extractor.py` | 自动测试（unittest；用 sqlite3 独立比对全部行与 BLOB 字节） |
| `table.json` / `bad-table.json` | 表描述 / 越界根页的反例 |
| `fixtures/source.sqlite` / `fixtures/truncated.sqlite` | 正常 / 截断的输入 |
| `records.jsonl` | 已发布的抽取结果（87 行，由下述命令生成） |

## 运行

```sh
python3 extractor.py fixtures/source.sqlite table.json -o records.jsonl
```

成功时退出码 0，stderr 打印行数、叶页与 overflow 页清单；输入损坏或超出
支持范围时退出码 2，stderr 给出带页号/偏移的定位诊断，例如：

```
error: 文件大小 5944 不是页大小 512 的整数倍，最后一页不完整（疑似截断）
error: rootpage 99999 越界：数据库共 12 页
```

## 测试

```sh
python3 -m unittest -v
```

测试覆盖：varint（含 9 字节负 rowid）、minLocal/maxLocal 公式、record 解析
（全部 serial type、IPK 规则、header/载荷不一致拒绝）、数据库头校验
（魔数/页大小/保留字节/WAL/UTF-16/截断/越界 rootpage）、整表抽取并与
sqlite3 独立查询**完整比对所有行和 BLOB 字节**（`lower(hex(payload))`）、
材料确实触发内部页与 overflow 页、负 rowid/Unicode/NULL/空串/空 BLOB、
CLI 行为，以及核心模块不 import sqlite3。

## 输出格式（records.jsonl）

每行一个 JSON 对象，按 rowid 升序：

```json
{
  "table": "records",
  "rowid": 128,
  "values": {"id": 128, "label": "row-128", "amount": null,
             "payload": "00ab…(小写hex)", "note": ""},
  "source": {"leaf_page": 6, "cell_index": 3, "rowid": 128,
             "payload_bytes": 1514, "overflow_pages": [3, 4, 5]}
}
```

* `values`：逻辑列值，键序同 `table.json` 的 `columns`；BLOB 输出小写 hex
  字符串，数值/字符串/null 保持 JSON 原类型。
* `source.leaf_page`：cell 所在叶页页号（1 起）。
* `source.cell_index`：cell 在该叶页 **cell 指针数组** 中的下标（0 起），
  不是页内物理偏移顺序。
* `source.payload_bytes`：记录载荷总字节数 P（含 overflow 部分）。
* `source.overflow_pages`：overflow 页链页号，按链顺序；无溢出为空数组。

## SQLite 格式要点（本工具实现的子集）

以下多字节整数均为**大端**；页号从 1 开始；偏移量除特别说明外均为
"页内偏移"（相对该页起始字节）。

### 数据库头（第 1 页前 100 字节）

| 偏移 | 大小 | 含义 |
| --- | --- | --- |
| 0 | 16 | 魔数 `SQLite format 3\0` |
| 16 | 2 | 页大小；值为 1 表示 65536，否则须为 512..32768 的 2 的幂 |
| 18 | 1 | 文件格式写版本：1=rollback，2=WAL（本工具拒绝 2） |
| 19 | 1 | 文件格式读版本，同上 |
| 20 | 1 | 每页末尾保留字节数 R；可用空间 U = 页大小 − R，须 ≥ 480 |
| 24 | 4 | 文件 change counter |
| 28 | 4 | 头内数据库页数（仅当非零且偏移 24 == 偏移 92 时有效） |
| 56 | 4 | 文本编码：1=UTF-8（仅支持），2=UTF-16le，3=UTF-16be |
| 92 | 4 | version-valid-for 编号 |

文件大小必须是页大小的整数倍，否则视为截断；头内页数有效时须与实际页数
一致。第 1 页的 b-tree 头从偏移 100 开始，其余页从偏移 0 开始。

### b-tree 页头（叶页 8 字节，内部页 12 字节）

| 偏移 | 大小 | 含义 |
| --- | --- | --- |
| 0 | 1 | 页类型：0x05=表内部页，0x0d=表叶页（0x02/0x0a 为索引页，不支持） |
| 1 | 2 | 首个 freeblock 偏移（本工具不恢复已删数据，忽略） |
| 3 | 2 | cell 个数 N |
| 5 | 2 | cell 内容区起点（0 表示 65536） |
| 7 | 1 | 碎片空闲字节数 |
| 8 | 4 | 仅内部页：最右孩子页号 |

页头之后是 N 个 2 字节大端偏移组成的 **cell 指针数组**，指向页内真实
cell；逻辑（rowid）顺序由指针数组与 b-tree 遍历决定，**不能**把页内物理
顺序当作 rowid 顺序。

### varint（变长整数）

大端 base-128，最多 9 字节：前 8 字节各贡献低 7 位，最高位为续位（1 表示
还有后续）；若前 8 字节续位均置位，第 9 字节贡献完整 8 位，合计 64 位。
rowid 按二进制补码解释为有符号 64 位整数（负 rowid 必编码为 9 字节）。

### 表内部页 cell 与遍历

内部页 cell = 4 字节大端左孩子页号 + rowid 键 varint。遍历：从 rootpage
出发，对每个内部页依次递归各 cell 的左孩子，最后递归最右孩子；叶页即得
升序序列。页号越界、重复访问（环）均报错。

### 表叶页 cell 与 overflow

叶 cell = payload 长度 P（varint）+ rowid（varint）+ 页内载荷
+（可选）4 字节大端 overflow 首页页号。页内载荷字节数（仅表叶，U 为可用
空间）：

```
maxLocal = U - 35
minLocal = ((U - 12) * 32 // 255) - 23
P <= maxLocal            -> 全部在页内
否则 K = minLocal + (P - minLocal) % (U - 4)
     K <= maxLocal       -> 页内 K 字节
     否则                -> 页内 minLocal 字节
```

剩余 `P − 页内` 字节沿 overflow 页链拼接：每个 overflow 页首 4 字节为大端
下一页页号（0 表示链尾），其后 `U − 4` 字节为载荷。链过短（页号 0 时仍
缺字节）、成环、页号越界、取齐后链未终止，均明确报错。

### record（行载荷）

```
header 长度 varint（含自身）| 各列 serial type varint | 数据体
```

header 字节数 + 各字段字节数之和必须恰好等于 P，否则报错。serial type：

| 值 | 含义 | 字节数 |
| --- | --- | --- |
| 0 | NULL | 0 |
| 1..6 | 有符号整数（大端补码） | 1/2/3/4/6/8 |
| 7 | IEEE 754 双精度浮点（大端） | 8 |
| 8 / 9 | 常量整数 0 / 1 | 0 |
| 10, 11 | 保留（遇到即报错） | — |
| 偶数 ≥ 12 | BLOB，(N−12)/2 字节 | |
| 奇数 ≥ 13 | TEXT（UTF-8），(N−13)/2 字节 | |

`INTEGER PRIMARY KEY` 列在 record 中存 NULL，逻辑值取自 rowid（本工具按
`table.json` 的 `integer_primary_key_index` 还原）。record 列数少于表列数
时尾部补 NULL（对应 `ALTER TABLE ADD COLUMN` 的旧行）；多于表列数报错。

## 限制

仅支持 SQLite3 UTF-8、表 b-tree（0x05/0x0d）、普通 rowid 表；不支持索引
树、WITHOUT ROWID、WAL、已删数据恢复、 freelist 校验。规模目标为几页到
几十页，整库读入内存。不实现写入、索引、数据库服务器或 Docker。
