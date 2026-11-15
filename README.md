# pgmirror — pgoutput 离线消息 → SQLite 关系镜像

把 PostgreSQL 16 实际导出的 **pgoutput v1 逻辑解码消息**（`fixtures/changes.bin`）
离线还原成一个可查询的 SQLite 关系镜像。程序从空镜像开始，**只解释消息字节**，
不重新执行 `source.sql`、不连接数据库、不管理复制槽、不做在线 CDC。

## 数据格式

`fixtures/*.bin` 的离线存储格式（不是复制协议 wire envelope）：

```
[ uint32 大端长度（不含这 4 字节自身） ][ 一条完整 pgoutput v1 消息原字节 ]
```

`pgmirror/framing.py` 只负责按这层长度切帧，消息原字节保持 pgoutput 原样上交协议层。

## 支持范围（与 contract.json 对齐）

| 维度 | 支持 | 明确拒绝 |
| --- | --- | --- |
| 消息 | `B` Begin、`R` Relation、`I` Insert、`U` Update、`D` Delete、`C` Commit | `T` TRUNCATE、`O` origin、`Y/L/S` streaming |
| 类型 | OID `23` int4、OID `25` text（text 形态 tuple） | 其他 OID、binary(`b`) tuple、类型扩展 |
| 复本标识 | 默认 replica identity（Update/Delete 带 `K` 旧键） | REPLICA IDENTITY FULL 的 `O` full-old tuple |
| 关系演进 | 仅**尾部追加** nullable 且 default NULL 的列 | 改列名/类型/顺序、删列、改主键、改 identity |
| 事务 | 非流式普通事务 | streaming 事务 |

## 核心语义

- **提交可见性**：消息只在事务暂存层生效；Commit 时在单个 SQLite 事务里把
  schema 变更与行变更一并落库。任何错误都丢弃整个未提交事务，已提交前缀保留，
  未提交内容绝不泄漏。EOF 处仍打开的事务标记为 `incomplete`，不发布其中记录。
- **列位置不固化**：tuple 始终按**当前最新 Relation** 的列名/顺序解释，
  而不是第一次见到的位置。Relation 可在后续事务重发；追加列后镜像执行
  `ALTER TABLE ... ADD COLUMN ... DEFAULT NULL`，旧行新列为空。
- **tuple 标记**：
  - `n` = 真 NULL（会把业务字段清空）；
  - `u` = unchanged TOAST，**继承该业务行已有值**，不是 NULL 也不是字面字符；
    没有旧行可继承则失败；Insert 中的 `u` 非法；
  - `t` = uint32 长度前缀的文本值。
- **定位规则**：Update 带 `K` 旧键元组时按旧主键定位、再写新主键；没有 `K`
  时用新 tuple 的键定位。主键变化不会留下旧行。Delete 按 `K` 键删除。
  `K` 元组中非键列上的 `n` 仅为占位，**不代表清空业务字段**。

## 用法

需要 Python 3.11，仅用标准库，无第三方依赖。

```bash
# 构建镜像与两份 JSONL（输出到 output/）
python3 -m pgmirror.cli build \
  --input fixtures/changes.bin \
  --contract contract.json \
  --out output

# 在任一消息边界（含端点的 0 基消息序号）检查已提交镜像，不落盘
python3 -m pgmirror.cli inspect \
  --input fixtures/changes.bin \
  --contract contract.json \
  --through 4

# 附带只读 SQL 查询
python3 -m pgmirror.cli inspect \
  --input fixtures/changes.bin --contract contract.json \
  --through 17 --sql "SELECT id, qty, note, length(body) AS body_len FROM records"
```

成功退出码 0；遇到不支持/格式错误退出码 2，诊断带**消息序号与字节偏移**。

## 交付物

运行 `build` 后在 `--out` 目录得到：

- **`mirror.sqlite`**：业务表（如 `records`）+ 两张目录表
  （`_mirror_relations` 关系版本、`_mirror_transactions` 事务元数据）。
- **`records.jsonl`**：每条行变更一行，含 `xid`、commit LSN（整数+十六进制）、
  消息序号/帧偏移、`op`、定位键、全字段 `new_row` / 删除或更新前 `row_before`。
- **`transactions.jsonl`**：每个事务一行，含 `xid`、`status`
  （committed/aborted/incomplete）、commit LSN、消息范围 `message_from/to`、
  帧范围、insert/update/delete 计数与 schema 版本事件。

LSN 同时保留**无损整数**与 `0x...` 十六进制字符串。

## 真实材料核对结论（fixtures/changes.bin，18 条消息 / 4 个事务）

1. **Relation 重发**：4 个事务都重发 Relation，目录版本 1→4；
   第 3 个事务把列从 3 列扩到 4 列（追加 `note text`）。
2. **4096 字节 TOAST 跨更新保留**：`id=1` 的 `body` 是
   `string_agg(md5(g::text),'')`（g=1..128），第二次事务只改 `qty`、`body` 发 `u`，
   镜像中的 4096 字节内容与独立重算的 md5 串**逐字符相等**，并继续存活到最后。
3. **2→3 主键变更**：`id=2` 的 Update 带 `K=(id 2)`，新 tuple 键为 `id=3`；
   镜像中 `id=2` 不残留，`id=3` 生效。
4. **note 新增与删除**：追加 `note` 后旧行新列为 NULL，`id=1` 更新为 `'added'`；
   第 4 个事务按 `K=(id 3)` 删除，最终只剩 `id=1` 一行。

独立基准不执行 `source.sql`，而是按其语义手工推导并用 `hashlib.md5` 重算 TOAST 串。

### 负例 fixtures

- `fixtures/unknown-relation.bin`：第 1 条 Insert 的 relid 被改成 `0xfffffed8`
  （uint32 = 4294967000）。诊断报 `message_index=2, frame_offset=91, byte_offset=92`，
  该事务 aborted，业务表甚至未创建，`records.jsonl` 为空。
- `fixtures/truncated.bin`：尾部删 3 字节。诊断报
  `message_index=17, frame_offset=4772, byte_offset=4776, declared_length=26, available=23`，
  前三个已提交事务完整保留，第 4 个删除事务不发布。

## 代码结构

```
pgmirror/
  errors.py    带消息序号/字节偏移的异常体系
  framing.py   4 字节大端长度分帧（含截断诊断）
  protocol.py  pgoutput v1 纯字节解析器（无状态、可单测）
  contract.py  contract.json 加载与校验
  engine.py    事务暂存、schema 演进、TOAST 继承、原子提交
  driver.py    分帧→解析→引擎 驱动，产出三份交付物
  cli.py       build / inspect 命令行
tests/
  pgoutput_builder.py  独立 pgoutput 字节编码器（不导入被测 parser）
  test_protocol.py     分帧/协议解析单测 + 与真实字节逐字节交叉验证
  test_engine.py       TOAST 继承、K 键、schema 演进边界、列位置等语义
  test_integration.py  真实 fixtures 端到端 + 独立基准全字段比对
```

## 自动验证

```bash
python3 -m unittest discover -s tests -v
```

56 个测试覆盖：真实消息全量解析、独立编码器与真实字节逐字节相等、每个 Commit
边界的全字段快照、TOAST 继承/非法 `u`、`K` 键定位与非键 `n` 占位、追加列回填
NULL、各类不支持 schema 变更拒绝、unknown-relation 与 truncated 的定位诊断、
未提交事务的中间不可见性，以及 EOF incomplete 标记。
