# 字节序编码与存储布局（LAYOUT）

## 1. 总体方案

业务复合键被编码成**一条自定界的 BLOB**，写入 SQLite 的
`WITHOUT ROWID` 联合主键表。SQLite 只承担两件事：

1. 按 BLOB 的二进制字典序维护 B-Tree；
2. 按编译出的下界/上界做区间取数（`key_blob >= ? [AND key_blob < ?]`）。

查询路径上没有任何业务列排序、函数计算或解码过滤；残余核对只在区间候选上做
精确判断。不实现 B+ 树或自造数据库。

## 2. 表结构

```sql
CREATE TABLE kv (
    key_blob BLOB NOT NULL,          -- encode() 得到的复合业务键
    row_id   TEXT NOT NULL,          -- 键外独立身份
    payload  TEXT,                   -- 原始 JSONL 行（留存，可空）
    PRIMARY KEY (key_blob, row_id)
) WITHOUT ROWID;

CREATE TABLE kv_meta (
    name  TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
-- kv_meta 内容：
--   format_version = 1
--   schema_json    = 原始 schema 的 JSON 文本
--   schema_digest  = 规范化 JSON 的 SHA-256 摘要
```

### 为什么 row_id 不会与业务键字节混淆

`key_blob` 与 `row_id` 是联合主键的**两个独立列**，物理上分离；业务键的任何
字节都不会被解释成 row_id。相同业务键（同一 `key_blob`）可对应多条记录，
联合主键第二列保证它们按 `row_id` 的 UTF-8 字节序稳定输出，`ORDER BY
key_blob, row_id` 只是复述主键物理序，不引入业务排序。

## 3. 单字段编码（升序空间）

所有字段先编码到统一的“升序空间”，首字节为标签：

| 标签 | 含义 |
| --- | --- |
| `0x00` | NULL（单字节，无负载、无终止符） |
| `0x02` | 非空 |
| `0x01` | 保留为转义引导字节，不会作为标签出现 |

* **int64（定长）**：`0x02` + 8 字节大端。编码前对数值加 `2^63`（翻转符号
  位），于是有符号 int64 的数值序等于 uint64 字节序。总宽 9 字节。
* **text**：先取 UTF-8 字节（不做 locale 比较、不做 Unicode 归一化），再
  `0x02` + 转义负载 + 终止符 `0x00`。
* **bytes**：输入为 `{"hex": "..."}`，解码成原始字节后同 text 规则编码。

### 变长转义（无歧义可逆，内嵌零字节安全）

终止符是裸 `0x00`，因此负载中的 `0x00` 必须转义；同时转义引导字节自身
也要转义：

| 负载原字节 | 转义后 |
| --- | --- |
| `0x00` | `0x01 0x01` |
| `0x01` | `0x01 0x02` |
| 其他 | 原样 |

这样终止符 `0x00` 只可能出现在字段边界，解码时可以无歧义切字段；空串/空
bytes 编码为 `0x02 0x00`（标签后紧跟终止符），与 NULL（`0x00`）、含零字节
负载均不同。

### 区分性

负整数、`INT64_MIN`/`INT64_MAX`、空串、空 bytes、内嵌零字节、NULL 的编码
两两不同（由定长宽度、标签、终止符与转义共同保证）。

## 4. order / nulls 声明的实现

每个字段在升序空间编码后，按该列声明做**只作用于本字段字节段**的双射变换。
变换保持或整体反转该字段的字典序，且不改变字段边界位置，因此拼接后整条
BLOB 的字典序精确等价于“按 schema 逐列、按各自 order/nulls 比较”的业务序。

| order | nulls | 变换 | NULL 最终标签 | 值首字节 | 终止符最终值 |
| --- | --- | --- | --- | --- | --- |
| asc  | first | 恒等 | `0x00` | `0x02` | `0x00` |
| asc  | last  | 仅头部交换标签 `0x00<->0x02` | `0x02` | `0x00` | `0x00` |
| desc | last  | 整段按位取反 | `0xFF` | `0xFD` | `0xFF` |
| desc | first | 整段取反，再仅头部交换 `0xFD<->0xFF` | `0xFD` | `0xFF` | `0xFF` |

要点：

* **头部交换只作用于字段首字节**。负载中允许出现任意字节（含 `0x02`），
  若对整段做标签交换会破坏负载；终止符也不参与头部交换，其最终值仅由
  order 决定（asc=`0x00`，desc=`0xFF`）。
* **desc 的终止符参与次序**。取反作用于“标签+负载+终止符”整段，短串的
  终止符 `0x00` 取反为 `0xFF`。短串是长串前缀时，短串在该位置的 `0xFF`
  大于长串的任意取反负载字节，因此降序下短串正确排在长串之后，不会出现
  “只倒置有效字节”导致的短前缀错位。
* 取反与头部交换都是字节双射，字段前缀关系在变换后保持（同一列的两个段
  共享前缀当且仅当变换后仍共享前缀），这是前缀区间在 desc 列上依然成立的
  基础。

## 5. 前缀查询到 BLOB 区间的编译

支持“前 k 列等值”以及“紧接第 k+1 列（必须是 text）的 `starts_with`”。

1. 前 k 列逐列生成**完整字段段**（变长列含终止符）；
2. `starts_with` 只生成 `标签 + 转义后的前缀负载`，**绝不追加终止符**——
   它是字段前缀而非完整字段；
3. 拼接得到区间下界 `L`（包含）；
4. 上界取 `successor(L)`：从末字节向左找到第一个非 `0xFF` 字节加 1 并截断
   后缀，得到严格大于所有以 `L` 为前缀的 BLOB 的最短键（排除）；
5. 若 `L` 为空或全为 `0xFF`，不存在有限后继，上界为**无穷**——SQL 中直接
   不写上界子句，而不是虚构一个“最大字符”。

所有命中键在最终字节序空间中恰好共享同一前缀，因此无论 asc/desc、何种
NULL 位置，区间统一为半开 `[L, successor(L))`。

### 扫描与残余核对

```sql
SELECT key_blob, row_id, payload FROM kv
WHERE key_blob >= ? AND key_blob < ?      -- 上界为无穷时省略第二个条件
ORDER BY key_blob, row_id
```

`EXPLAIN QUERY PLAN` 显示 `SEARCH kv USING PRIMARY KEY (key_blob>? AND
key_blob<?)`，确认走主键 B-Tree 区间扫描，不是全表扫描。取出的候选解码后
再用 `matches()` 做一次精确残余核对（正常情况下区间紧密、零丢弃），防止
前缀边界理解错误。**不会**扫描全部键再解码筛选。

## 6. 持久化与重开

`kv_meta` 保存格式版本、完整 schema JSON 及其 SHA-256 摘要。重开时：

* 校验 `format_version == 1`；
* 从库内还原 schema（可不传外部 schema）；
* 若调用方传入 schema，则比对摘要，不一致直接报错；
* 仅凭 `key_blob` 即可 `decode` 还原完整业务键，bytes 列回到
  `{"hex": ...}` 形式。

## 7. 输入校验（拒绝即抛 ValueError）

* 未知类型、非法 order/nulls、重名列、空 schema；
* int64 非整数、布尔值、超出 `[-2^63, 2^63-1]`；
* text 非字符串、含无法编码为 UTF-8 的 lone surrogate；
* bytes 非 `{"hex": ...}` 结构、奇数位、大写或非十六进制字符（空串合法）；
* 缺列、`starts_with` 作用于非 text 列或 NULL、前缀列数超出 schema。

## 8. 明确不做

浮点/NaN 顺序、通用 collation、压缩格式、B+ 树或自造数据库。
