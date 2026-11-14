# 选择向量驱动的短路表达式ETL

本目录仅含任务输入与可运行测试设施，不含待实现的处理器。

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
