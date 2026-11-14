# fastqpair — 乱序 FASTQ 双端配对（仅数据接入）

把记录顺序不一致的 R1/R2 FASTQ 原件生产成可靠的双端配对数据集。
**只做数据接入**：不做序列比对、反向互补或共识推断，不修改任何碱基与质量字符。

## 边界与解析规则（严格，不做 `@` 重同步）

- 支持普通 FASTQ 与 gzip FASTQ（按魔数自动识别，也可在清单里显式声明）。
- 支持序列与质量的**合法多行包装**：
  - 序列一直读到以 `+` 开头的分隔行为止；
  - 质量按**序列长度**逐字符累计，质量行即使以 `@` 或 `+` 开头也只是质量，
    绝不会被当成下一条记录。
- 序列字符限 `ACGTN`；质量为 Phred+33 可打印字符（ASCII 33–126），长度必须与序列相等。
- **文件级致命错误（无法确定后续边界）→ 停止该文件并报告未读尾部**，绝不搜索下一个 `@` 猜测修复：
  - 头/序列/分隔/质量阶段遇到 EOF（物理截断）；
  - 质量累计超过序列长度（超长质量，下一记录边界不可信）；
  - 缺失 `@` 头、结构空行等。
- **边界可确定但字段非法（非法碱基、非法质量字符、空序列）→ 隔离该记录并保留位置**，其余记录继续。
- 仅含 `@` 的头边界仍可确定，按**空身份**隔离而非致命停止。

## 身份与配对规则

- 规范 read 身份由 config 启用的方案解释：
  - `suffix`：首个空白 token 去掉结尾 `/1`、`/2`，后缀声明 mate；
  - `casava`：第二个空白 token 以 `1:` / `2:` 开头时声明 mate。
- 文件清单中的 `mate` 是文件角色，提供**缺省 mate**。
- 明确声明之间冲突、声明与文件角色不符、空身份，一律隔离，不得通过。
- 每个 `read_id` 必须**恰好**有一个合法 mate1 和一个合法 mate2 才能配对；
  任一 mate 出现多条同身份记录，整个 read 组判为 `ambiguous`，**不做多对多**。
- 单端缺失（`orphan`）与对端存在被拒记录（`mate_rejected`）分开报告；
  绝不因序列相同而合并身份。
- 所有来源读取与校验结束后才统一裁定，避免“先输出、后被重复身份推翻”。

## 有界暂存

- 乱序候选先入内存，完整未配对记录数严格不超过 `pending_record_limit`；
- 窗口满后最早进入的记录整体溢出到标准库 **SQLite**（不自行实现数据库）；
- 报告里给出 `spilled_records` 与 `max_memory_pending_records` 便于核对窗口是否触发。

## 产物（输出目录）

仅当所有来源完整（`complete=true`）时才发布配对 FASTQ：

| 文件 | 内容 |
| --- | --- |
| `paired_R1.fastq` / `paired_R2.fastq` | 规范四行 FASTQ，按 read_id 的 UTF-8 字节序排列，两份行数与顺序严格一致，多行合并为单行，原头与 seq/qual 字节不变 |
| `manifest.json` | 总清单：每来源、逻辑记录号、原头（base64）、规范身份、最终状态、字节范围、SHA256、compression |
| `orphans.json` | 单端缺失组 |
| `ambiguous.json` | 同名重复导致无法唯一配对的整组（含全部候选与位置） |
| `mate_rejected.json` | 对端存在隔离记录的组 |
| `quarantine.json` | 边界可确定但字段/身份非法的记录，保留来源位置 |
| `run_report.json` | complete 标志、分类计数、来源状态、致命尾部诊断、配对 id 顺序 |
| `.staging.sqlite` | 暂存库（可重入，重复运行会清空） |

字节范围口径：

- 普通文件：**原始 FASTQ 字节范围**（含原头、包装行、`+` 行与行尾）；
- gzip 文件：**解压后 FASTQ 流的字节偏移**（`space=decompressed_fastq_stream`），
  同时另存压缩原件 `file_sha256` 与 `compression=gzip`，不冒充 gzip 物理字节范围。

输入不完整时不发布配对 FASTQ，`manifest.complete=false`，但保留已知前缀与未读尾部诊断
（`tail_offset`、`tail_length`、base64 预览、原因、出错逻辑记录号）。

## 用法

```bash
python -m fastqpair --manifest fixtures/manifest.json \
                    --config fixtures/config.json \
                    --out out/
```

manifest（JSON 数组，路径相对该清单文件）：

```json
[
  {"path": "R1.fastq", "mate": 1},
  {"path": "R2.fastq.gz", "mate": 2}
]
```

退出码：`0` 完整成功；`2` 存在致命未读尾部（诊断已写出）；`1` 配置或 I/O 错误。

## 测试

```bash
python -m unittest tests.test_pairing -v
```

测试覆盖乱序、wrapped 序列、以 `@/+` 开头的质量行、同名重复、角色/声明冲突、
孤儿与 mate 拒绝分离、坏尾（截断/超长/空行/gzip）、UTF-8 字节序、小窗口落盘等。
其中 `IndependentVerifier` 是**独立重读校验器**：不导入生产解析器，
而是按 manifest 的字节范围自行切回来源原件，逐配对核对序列/质量，而非只比较配对总数。
规模仅几十条，窗口常设为 1–2 以强制触发落盘，不做生物分析或性能基准。
