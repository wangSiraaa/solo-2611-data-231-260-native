# ttml2cues — 有限 TTML 字幕 → 规范字幕事实与时间片

把 `timing.md` 定义的**有限媒体时钟子集** TTML 转成可供语料处理的 JSONL 事实数据。
只做时钟解析、层级裁切与文本时间片切分；**不做**字幕渲染、布局、OCR、格式转换平台或任何音视频处理。

- 运行环境：Python 3.11，仅标准库（`xml.etree.ElementTree`、`decimal`、`json`、`argparse`），无第三方依赖。
- 精确算术：时间一律经 `decimal.Decimal` 换算为 `tickRate=1000` 的整数 tick，全程不累计浮点秒；
  非整数 tick 或子集外表达式（`hh:mm:ss`、frame、wallclock、动画等）直接拒绝。

## 目录

```
ttml2cues/            转换包
  timing.py           有限时间表达式解析（Decimal，s/ms/t）
  converter.py        层级时钟、祖先裁切、p 内文本时间片切分与 JSONL 输出
  __main__.py         命令行入口
verify.py             独立自动验证（自带第二套解析/评估器 + 手工时间表 + 中点判定）
fixtures/             输入样例（captions / empty-clipped / bad-time）
timing.md             支持子集的权威语义说明
output/               默认输出目录（git 忽略）
```

## 使用

```bash
python -m ttml2cues fixtures/captions.ttml -o output/captions
# 可选：--tick-rate（子集固定 1000，传入其他值只用于核对文档声明）
```

Python API：

```python
from ttml2cues import convert_file, TimingError

try:
    paths = convert_file("fixtures/captions.ttml", "output/captions")
except TimingError as exc:       # 非整数 tick / 子集外表达式 / 层级约束违反
    print("拒绝:", exc)
```

转换失败时进程退出码为 `2`，stderr 给出明确原因，且不写半成品输出。

## 时钟语义（实现要点，完整定义见 timing.md）

1. **同步基准**：`body` 以 tick 0 为基准，必须有有限 `dur`。
   - `par` 子项的基准 = 父项**未裁切开始**；
   - `seq` 直接子项的基准 = 前一兄弟的**未裁切结束**，第一个用父项未裁切开始；
   - `seq` 推进永远用未裁切值，兄弟/父裁切不回改相对时钟。
2. **begin / end / dur 区分**：`begin` 缺省为 0；未裁切 `start = 基准 + begin`；
   `end` 与 `begin` 同一同步基准；`dur` 从本项 `start` 计时；两者同时给出取更早；`end < start` 拒绝；
   `seq` 直接孩子必须显式给 `end` 或 `dur`；par 孩子无 `end/dur` 时继承父未裁切结束。
3. **先未裁切、后裁切**：每个元素先算未裁切区间，再与**全部祖先有效区间**取交。
   完全裁空是合法的不可见段，输出 `clipped_out` 诊断而不报错。
4. **p 内文本**：文本节点继承直接所属元素的有效区间；span 的 **tail 属于父元素**（span 结束后仍按父时段可见）；
   `br` 固定产生换行 `\n`；按 XML 文档序拼接当前时刻可见的 text/tail/br；
   保留 `xml:space="preserve"` 原字符，不插空格、不 trim；p 外缩进不是正文。
5. **切分与合并**：以 p 内全部有效文本节点的时界为边界切片段，在每个相邻边界的**中点**独立判可见节点；
   空字符串片段跳过但计入段诊断；相邻片段仅当**文本与参与源节点 ID 序列都相同**才合并为最大稳定片段；
   不同 p 独立保留，允许重叠，不做“挑一条”。

## 输出文件（每行一个 JSON 对象，UTF-8）

### paragraphs.jsonl — 每个 `p` 的完整时钟事实

| 字段 | 含义 |
| --- | --- |
| `p_id` | 稳定 ID：有 `xml:id` 用之，否则由 XML 路径生成 |
| `xml_path` | 原 XML 层级路径（带同名序号与 `xml:id` 注解） |
| `sync_base_kind` / `sync_base_tick` | 同步基准来源（root/par/seq）与基准 tick |
| `begin_tick` / `end_offset_tick` / `duration_tick` | 原始定时属性的精确 tick 换算（缺省为 `null`） |
| `unclipped_start_tick` / `unclipped_end_tick` | **未裁切**区间 |
| `effective_start_tick` / `effective_end_tick` | 与全部祖先取交后的有效区间；裁空为 `null` |
| `visible` / `clipped_out` | 是否可见 / 是否完全裁空的合法不可见段 |
| `empty_text_fragment_count` / `empty_text_fragments` | 跳过的空文本片段诊断（含时界与当时可见源序列） |

### cues.jsonl — 语料可直接消费的时间片

`cue_id`、`p_id`、`start_tick`、`end_tick`、`text`、`source_node_ids`（文档序的稳定源节点 ID）。
同一 p 的 cue 严格相邻、无空洞无重叠；不同 p 的 cue 可重叠。

### source_nodes.jsonl — 可回查的源节点

`source_node_id`（`xml:id#text-N` / `#tail-N`，br 用 `父id#br-N`）、`kind`（text/tail/br）、
`xml_path`、`owner_element_id`、`ordinal`、`text`（字符原文）、有效时界（裁空为 `null`）。

## captions.ttml 的手工绝对时间表（tickRate=1000）

| p | 未裁切区间 | 有效区间 | 说明 |
| --- | --- | --- | --- |
| p1 | 1500–5500 | 1500–5500 | span s1 仅 2500–4000 可见，tail `!` 常驻 |
| p2 | 3000–9000 | 3000–**7000** | end=8s 相对 par 基准 1000；被父 div（止于 7000）裁切 |
| p3 | 8000–9000 | 8000–9000 | seq 第一项 |
| p4 | 9250–10750 | 9250–10750 | 前兄弟未裁切结束 9000 + begin 250；9000–9250 为 seq 间隙 |
| p5 | 11250–**13250** | 11250–**12000** | 被 body（12s）裁切；未裁切结束仍保留供 seq 语义核对 |

## 自动验证

```bash
python verify.py
```

验证器**不复用转换器的时钟代码**，而是独立实现第二套 Decimal 解析与 par/seq 评估器，并：

- 用人工推导的绝对区间表/源节点表/期望 cue 表逐字段核对三个 fixture；
- 在每个相邻 cue 边界的**中点**独立判断可见源节点并独立拼文本，再与输出比对（而非把转换结果再序列化比较）；
- 回查原 XML 确认源节点路径与字符原文真实存在；
- 断言 p1/p2 在 t=4500 重叠且两条 cue 都保留、seq 间隙无 cue、父裁切保留未裁切时钟；
- `empty-clipped` 合法输出 `clipped_out` 且无 cue；`bad-time` 必须非零退出且无半成品；
- 另含合成 br/常驻文本材料、合并规则纯函数单元测试与时间表达式拒绝矩阵。

## 范围外（明确不做）

播放器、OCR、布局/样式渲染、SRT/VTT 等格式转换平台、Docker 编排，以及 timing.md 之外的
TTML 特性（其他时钟表达式、动画、frame/wallclock、嵌套时序以外的标签等——遇到即拒绝并指明原因）。
