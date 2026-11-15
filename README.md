# 单机双输入事件时间 LEFT JOIN

按到达顺序消费 `fixtures/events.jsonl`（JSONL）中的左（`L`）/右（`R`）事件，做事件时间 LEFT JOIN。提供可逐条调用的算子 `Joiner.push(event)` / `Joiner.finish()`，每步返回该步新产生的匹配 / 未匹配事实，而不是等读完全文再统一连接。

纯标准库（Python 3.11），无网络、无 Docker、无外部依赖。

## 匹配语义

对左事件 `l` 与右事件 `r`：

```
l.key == r.key
且 l.ts + lower_ms <= r.ts < l.ts + upper_ms
```

区间左闭右开，参数来自 `config.json`（`lower_ms=-2`, `upper_ms=4`）。相同 key 允许多对多；每个合法左右对在两者都在场时恰好输出一次。

## 事件格式

```jsonc
{"op":"data","side":"L","id":"l1","key":"a","ts":10,"value":1}   // value 可为任意 JSON
{"op":"watermark","side":"R","ts":12}
{"op":"end","side":"R"}
```

- `side` 只能是 `L` / `R`；`id` 全局唯一（字符串或整数）；`ts` 为整数毫秒（拒绝 `bool`/浮点）。
- **水位语义**：某侧水位 `W` 表示该侧未来*合法*数据的 `ts >= W`。水位初始为 `-inf`，只能递增或持平（持平合法，倒退报错）。`end` 视为该侧 `+inf`。
- **迟到判定**：数据到达时，先与*本侧此刻的旧水位*比较。`ts < W` 判为迟到，写入 `late.jsonl`，**绝不参与关联**；`ts == W` 仍合法（未来合法数据为 `>= W`）。
- `EOF` 不是 `end`。只有显式 `end` 才把该侧水位提升到 `+inf`；两侧都 `end` 之前调用 `finish()` 会报 `incomplete`。

## 清退条件（左右不同阈值）

两侧缓冲的清退条件**不同**，不能套用同一个“最小水位”阈值：

- **左缓冲**：缓冲中的左 `l` 只能匹配未来合法右 `r.ts >= W_R`。其匹配区间是 `[l.ts+lower, l.ts+upper)`。一旦

  ```
  W_R >= l.ts + upper_ms
  ```

  未来右已不可能落入区间，`l` 可安全清退；若从未匹配，则**恰在此刻**输出 `right_id = null` 的最终事实（已有匹配则不输出 null 事实）。

- **右缓冲**：缓冲中的右 `r` 只能匹配未来合法左 `l.ts >= W_L`。把谓词改写为 `r.ts - upper < l.ts <= r.ts - lower`。一旦

  ```
  W_L > r.ts - lower_ms
  ```

  未来最小合法左已越过可匹配的最大左，`r` 可安全清退。注意必须**严格大于**：`l.ts == W_L == r.ts - lower` 的左仍然匹配。

即：左由**右**水位清退，右由**左**水位清退，公式分别为 `W_R >= l.ts+upper` 与 `W_L > r.ts-lower`。为及时清退，两侧各用一个按 `ts` 排序的堆，每次水位/结束变化后立即弹出可清退记录。

> 即时匹配：任一事件到达时立即与该 key 下对侧已缓冲记录求交并输出；若某左到达时其区间已被右水位封闭，则不必缓冲，直接得出“匹配 + 可能的 null 事实”的最终结果。故每个合法对只在“两者中较晚到达者”到来时输出一次。

## 结果输出

- `matches.jsonl`：`left_id` / `right_id`、`left_ts` / `right_ts`、`left_value` / `right_value`。LEFT JOIN 未匹配左的 `right_id`、`right_ts`、`right_value` 为 `null`。
- `late.jsonl`：保留原行号 `line`、`side`、`id`、`key`、`ts`、`value`，以及判定时的水位 `watermark`。
- `trace.jsonl`：每步记录 `step`（输入序号）、`line`、`input`、`watermarks_before`/`watermarks`、`ended`、`retained`（此刻保留的左右 id）、`outputs`（本步新输出）、`late`（本步判迟）。便于核查时机与清退。
- 最终导出用 `sort_facts()` 按 `left_id` 稳定排序（同左先 `right_id=null`，再按 `right_id`）。

## 用法

```bash
# 运行（默认 config.json / fixtures/events.jsonl / out/）
python -m evjoin.cli --config config.json --events fixtures/events.jsonl --out out
# 或
python -m evjoin

# 全量测试
python -m unittest discover -s tests -v
# 或（若已安装 pytest）
python -m pytest tests -q
```

失败时进程退出码 `2`，stderr 打印带行号的诊断，例如：

```
error: line 2: watermark regression on side L: 4 < 5
```

## 算子 API

```python
from evjoin import Joiner

j = Joiner.from_config("config.json")   # 或 Joiner(lower_ms=-2, upper_ms=4)
new_facts = j.push(event, line_no=1)    # event 可为 dict 或 JSON 字符串
new_facts = j.push(event, line_no=2)
...
new_facts = j.finish()                  # 两侧均已 end，否则 JoinError
j.facts; j.matches; j.late; j.trace
```

内存状态：每侧按 key 分桶的缓冲、两个按 `ts` 的堆、已见 id 集合、水位/结束标记。无 checkpoint / 崩溃恢复。

## 失败情形（明确报错）

- 非法记录：未知/缺失 `op`、非法 `side`、缺 `id`/`key`/`value`、`ts` 非整数。
- 重复 `id`（跨两侧共享唯一性）。
- 水位倒退（持平允许）。
- 某侧 `end` 后继续送来该侧 `data` / `watermark`，或重复 `end`。
- 两侧未 `end` 时调用 `finish()`（EOF 不算 `end`）。

## 测试

`tests/` 覆盖：

- 与独立暴力参考实现 `tests/reference.py`（不做清退的嵌套循环 + 到达时水位判迟）对最终事实逐条核对，含 60 组随机合法流的对拍。
- 每步输出时机：新匹配即时产生；左在 `W_R >= l.ts+upper` 时输出 null；右在 `W_L > r.ts-lower` 才清退；错用对侧水位不影响。
- 同键多对多、左事件提前到达、右端点不匹配（`r.ts == l.ts+upper`）、左端点命中、恰等水位仍合法且判迟边界、无匹配左行、单侧空侧结束。
- 迟到记录保留行号与判定水位、最终稳定排序、CLI 输出文件、各类非法输入的失败路径。
