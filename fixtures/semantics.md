# 受限表达式约定
JSON AST节点只有col、literal、add、div、to_int、eq、gt、is_null、if、coalesce、and、or、not。每个节点恰有一种operator；不接任意代码。literal为int64/text/bool/null，NULL字面量由上下文定型，无法定型则预检拒绝。add/div输入为int64；div向零截断，除零及INT64_MIN/-1为行错误，任何整数运算溢出也是行错误。to_int只接受文本词法-?(0|[1-9][0-9]*)及int64范围，NULL返回NULL。无隐式bool→int。
eq/gt要求两端同类型，比较NULL得到NULL；文本比较按UTF-8字节，gt不支持bool。IF的TRUE选then，FALSE或NULL选else，分支输出同类型；COALESCE逐项取首个非NULL，遇已求值错误立即成为该行错误，不用后续值吞掉错误。AND/OR用SQL三值表，但左FALSE的AND、左TRUE的OR不求右侧；左UNKNOWN仍需右侧。其余二元节点左至右求值，左侧错误后不求右侧。错误不是NULL，输出单独error_code/node_path；每个输出列的各行独立。
每个实际运算节点记录输入selection行号及被真正求值的行数。col读取不是运算。批大小改变可改变调用次数，但每行每表达式节点是否被求值及最终结果应相同。
