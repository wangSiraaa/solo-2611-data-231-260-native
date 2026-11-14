# 选择向量驱动的短路表达式ETL

## 任务

批处理表达式中常用IF或COALESCE保护非法输入，若先对整列算除法再选择分支，会把本来不该求值的除零也报错。请用Python 3.11和NumPy实现受限列式表达式ETL，读取schema、columns及expressions，输出每行各表达式的值、NULL标记和独立错误信息。输入是实际列数组，row_id绑定原记录，批大小可配置。
支持范围以semantics.md为准：int64/text/bool及NULL，少量算术、转换、比较和控制节点。先预检AST与类型，不把结构错误变成逐行NULL。运行中使用selection vector或等价布尔掩码，在当前需要的行上逐节点运算；IF、COALESCE及AND/OR分别缩小分支的有效行集合，不能先算完整左右分支再np.where掩盖异常，也不能把整个batch退化成逐行调用一个标量解释器。
NULL、FALSE和错误是三种不同状态。被选中分支除零、非法to_int或整数溢出，只使对应输出列的这一行报错，保留row_id与AST路径，不污染其他行或另一输出表达式；未被选中分支的同类错误不出现。COALESCE取到非NULL后不再求后续项，已发生错误也不被后续默认值吞掉。IF的NULL条件选else；AND/OR按给定三值与短路规则处理。
记录真正进入每个运算节点的原始行号与求值计数，由执行入口产生，证明危险分支没有被提前计算。可以用NumPy向量运算与受控掩码，只对文本解析等缺少合适内核的叶操作作局部元素转换；不要把普通业务代码作为eval执行。
用现成数据核对未选择的坏文本、NULL除数、实际除零、最大整数加一和未知逻辑值。独立编写仅用于小数据验收的标量解释器，逐单元格比较值/错误及节点求值集合；批大小1、3和全集的规范结果须一致。交付编译计划、输出和README，不做SQL解析、JIT、在线服务、拟合特征或大压测。

输入文件：
- `fixtures/columns.json`
- `fixtures/schema.json`
- `fixtures/expressions.json`
- `fixtures/semantics.md`

环境：Python 3.11；全部业务数据为本地合成材料。

---

## 实现说明

### 结构

```
etl/
  model.py      类型/AST节点/Vector/错误码（纯数据结构，两实现共享）
  loader.py     读取并校验 schema、columns、expressions
  compiler.py   预检：AST 结构校验 + 类型定型 → 编译计划（Plan）
  engine.py     向量化引擎：selection vector 驱动，逐节点按掩码运算
  scalar.py     独立标量解释器（仅验收用，不共享求值逻辑）
  runner.py     作业编排：加载 → 编译 → 分批执行 → 汇总
main.py         ETL 入口，产出编译计划/输出/trace
acceptance.py   小数据验收，产出验收报告
```

### 运行

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/python main.py --batch-size 3          # 写 out/compile_plan.json、out/output.json、out/trace.json
.venv/bin/python acceptance.py                   # 写 out/acceptance.json，失败时退出码非 0
```

### 预检（编译期，拒绝即整体失败）

- 结构校验：每节点恰含一个 operator；元数固定（add/div/eq/gt/and/or 两个子节点，to_int/not/is_null 一个，if 三个，coalesce ≥1）；col 必须引用 schema 中存在的列；literal 仅 int64/text/bool/null（int 须在 int64 范围内，float 拒绝）。
- 类型定型：add/div 要求 int64；to_int 要求 text；eq/gt 两端同类型（gt 不支持 bool）；if 条件 bool、两分支同类型；coalesce 各项同类型；and/or/not 要求 bool；无隐式 bool→int。NULL 字面量由上下文定型，无法定型（如 `eq(NULL,NULL)`、`if(c,NULL,NULL)`、`is_null(NULL)`）则预检拒绝。
- 任何结构/类型错误抛出 `CompileError`，`main.py` 退出码 2，一行数据都不会执行——结构错误不会退化为逐行 NULL。
- 编译计划落盘 `out/compile_plan.json`：每个表达式的结果类型/可空性，及每个节点的 AST 路径（根为 `$`，子节点按位置展开，如 `$/2/0`）、operator、类型、可空性。

### 运行期（向量化引擎）

- **三态模型**：每行每表达式结果是「值 / NULL / 错误」之一。错误不是 NULL：出错行记录 `error_code + node_path`，从后续 selection 剔除，不会被后续分支或 COALESCE 默认值吞掉；各表达式、各行完全独立。
- **selection vector**：每个运算节点只在传入的行号选择集上计算（NumPy  fancy indexing + 布尔掩码），结果写入 batch 长的稠密 `Vector`（values + null 掩码），父节点只读取自己 selection 内的行。绝不先算完整分支再 `np.where` 掩盖异常。
- **分支收缩**：`if` 按条件（TRUE→then，FALSE/NULL→else）把 selection 切成两个子集分别求值；`coalesce` 逐项收缩剩余 NULL 行，取到非 NULL 即停，出错行立即成为该行错误；`and`/`or` 左 FALSE/左 TRUE 短路不求右侧，左 UNKNOWN 仍需右侧，再按 SQL 三值表合并。其余二元节点左至右求值，左侧出错的行不进入右侧。
- **错误码**：`div_zero`（除数为零）、`overflow`（add 溢出、INT64_MIN/-1）、`bad_to_int`（词法不符 `-?(0|[1-9][0-9]*)` 或超 int64 范围）。
- **NumPy 内核**：add 用 int64 回绕 + 符号判溢出；div 用 `//`/`%` 修正为向零截断，先剔除零除数与 MIN/-1；eq/gt 用 `np.equal`/`np.greater`（文本序 = UTF-8 字节序 = Python str 序）；仅 to_int 的文本词法解析无合适内核，对选中行做局部元素转换。算子通过分发表调用，无 `eval`。
- **trace**：所有运算节点（col 读取与 literal 填充不算运算）经唯一执行入口 `Engine._eval` 进入并记录（表达式、节点路径、batch 号、原始行号、计数），落盘 `out/trace.json`。例如 `guarded` 的 `$/2`（div）求值集合为 `{r0,r2,r3,r5,r6,r7}`——被 IF 保护的 r1、r4 从未进入除法。

### 输出

`out/output.json`：每表达式每行 `{row_id, value, is_null, error}`；错误行 `error={code, node_path}`，`value=null`、`is_null=false`（错误不是 NULL）。

### 验收（acceptance.py，84 项全部通过）

1. **一致性**：向量化引擎在批大小 1 / 3 / 全集（8）下的规范结果（带类型的值 + NULL 标记 + 错误码/路径）逐单元格一致；节点求值集合 `(node_path, row)` 跨批大小一致（调用次数可变，求值集合不变）。
2. **独立对标**：`etl/scalar.py` 为独立编写的逐行标量解释器（不共享求值逻辑），与向量化引擎逐单元格、逐节点求值集合完全一致。
3. **现成数据关键语义点**：
   - 未选择的坏文本：`branch_cast` r1（`to_int("bad")` 在未选中的 then 分支）得 7 无错误，to_int 只求值 {r0,r3,r5}；`guarded_and` r7 左 FALSE 短路不求 `to_int("03")`；`fallback` 的 to_int 只求值 {r2}。
   - NULL 除数：`unguarded_div` r5（b=NULL）结果为 NULL 而非错误。
   - 实际除零：`unguarded_div` r1/r4 报 `div_zero@$`；`guarded` 同行被 IF 保护得 0，div 求值集合不含 r1/r4。
   - 最大整数加一：`overflow` r3（INT64_MAX+1）报 `overflow@$`，同行的 `guarded`/`fallback` 不受影响。
   - 未知逻辑值：`guarded_and`/`guarded_or` r5 左 UNKNOWN 仍求右侧并暴露 `to_int("bad")` 错误；`branch_cast` r2/r6 条件 NULL 选 else。
4. **合成小数据**：INT64_MIN/-1 溢出、负数向零截断（-7/2=-3 等）、to_int 词法边界（`+5`/` 5`/`5 `/前导零拒绝、±INT64 边界）、三值真值表、COALESCE 不吞已发生错误、NULL 字面量上下文定型。
5. **预检**：14 个结构/类型错误用例全部被拒（退出码 2），3 个合法用例通过。

### 明确不做

SQL 解析、JIT、在线服务、拟合特征、大压测；标量解释器仅用于小数据验收，不在主执行路径。
