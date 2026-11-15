# 受限 Dremel 嵌套记录列化格式说明

本项目把 `schema.json`（根 required struct，仅 struct/list/int/string，list 最多两层、
任意节点 optional）的记录编码为**每个原始叶路径一个独立 JSONL 列流**，再仅凭 schema
与这些列流反解回原记录。它不是 Parquet/Arrow 物理格式，也不使用任何第三方列式库，
仅用 Python 3.11 标准库实现。

## 1. 目录产物

`python -m dremel encode --schema schema.json --rows fixtures/rows.jsonl --out output/`

| 文件 | 含义 |
| --- | --- |
| `<leaf>.jsonl` | 每个叶路径一列，文件名用 `__` 连接路径段，如 `batches__values.jsonl`；每行一个条目 `{r,d,value?}` |
| `_meta.json` | 内嵌 schema、每列叶路径、文件名、value 类型、最大 r/d、r 与 d 的逐阈值文字说明、条目形状约定 |
| `_record_count.json` | 单独存放记录数 `{"record_count": N}`，不与任何列流混放 |

列流里**不包含**原始 JSON、行号、数组下标或任何地址信息；嵌套边界完全由 r/d 表达。

## 2. 条目形状

每行一个 JSON 对象，只允许三个键：

```json
{"r": 1, "d": 3}
{"r": 1, "d": 3, "value": "b"}
```

- 仅当 `d == 该列 max_definition_level` 时允许且必须出现 `value`；
- 其余条目（父结构缺失、空 list、null 元素等）一律无 `value`，只有 `r,d`；
- 非法键、`r>d`、越界 r/d、value 类型不符都会在解码时被拒绝。

## 3. 定义级 d 的累计规则

沿根到叶路径累计：

- 每个 **optional 节点**贡献 1（节点缺失/显式 null 停在进入该 optional 层之前）；
- 每个 list 的 **repeated element 层**固定贡献 1；
- list 元素自身若 optional 再贡献 1。

三种关键停靠点（以 `batches` 为例，d 阈值见 `_meta.json`）：

| 情形 | d 停靠 | 含义 |
| --- | --- | --- |
| optional list 不存在 / null | 进入该 optional list 之前 | 向该 list 的所有后代叶各发一个无 value 标记 |
| 空 list `[]` | 进入 repeated 层之前（list 自身 present 级） | 同样向所有后代叶发标记 |
| null 元素 | 元素 optional 层之前（repeated 级） | 标记落在该元素的叶上 |

某个父结构缺失/为空时，**它的每个后代叶仍各收到一个标记条目**，因此一条记录在每个
叶列都至少留下一个条目，整行不会从任何列流中消失。

## 4. 重复级 r 的语义

`r` 是本条条目相对前一条「重新开始的最深重复层编号」：

- `r=0`：每条根记录该叶列的第一个条目（根记录边界）；
- `r=1`：同一外层 list 的下一个元素；
- `r=2`：同一内层 list 的下一个元素。

非重复列（无 list 的叶路径，如 `id`、`meta.name`）每记录仅一项，r 恒为 0。
list 第一个元素的 r 沿用进入该 list 时的重复级，其后元素取该 list 自身的 r_level。

## 5. 解码如何重建 struct/list 边界

- 以 `r=0` 切分根记录，先全局校验每个条目形状并核对各列根记录数一致；
- 主解码器（`dremel/decode.py`）用 Dremel 栈式状态机：按 r 弹出重复级更高的帧，
  再按 d 沿层级计划下降，optional 缺失写 null、空 list 写 `[]`、null 元素停在
  repeated 层、到叶最大定义级写 value；
- 各叶独立重建后做**严格合并**：struct 字段互补、list 逐位置且等长、null/非 null
  与叶值必须一致，任何共享重复祖先形状矛盾（如 `code` 一列两个元素而 `values` 一列
  一个）都直接拒绝，不猜测、不补值；
- 合并结果再走一次完整 schema 校验（拒绝额外字段、required null、类型错误等），
  并把缺失 optional 统一规范为 null。

`dremel/reference.py` 提供另一套独立的句柄表参考解码器，与栈式机控制流不同，
测试中要求两者在全部列上逐记录一致（交叉核对）。

## 6. 反校验拒绝项（测试覆盖）

- 篡改某叶的 r（越界/不可能）或 d（越界/落到不可能区间）；
- 在非 max_d 条目上带 value，或在 max_d 条目上删 value；
- 删除一条共享边界条目、插入多余元素制造 list 长度矛盾；
- 各列根记录数不一致、出现 schema 外的多余列；
- 输入记录含额外字段、required 字段为 null、类型不符、缺必填。

## 7. 命令入口

```bash
python -m dremel encode --schema schema.json --rows fixtures/rows.jsonl --out output/
python -m dremel decode --in output/ --out output/restored.jsonl
python -m unittest discover -s tests -v
```
