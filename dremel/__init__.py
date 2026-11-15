"""受限 Dremel 式嵌套记录列化 / 反解（仅 struct / list / int / string 子集）。

模块划分：
- schema.py：schema 加载、结构约束校验、输入行严格校验与规范化
- planner.py：把 schema 编译成每个叶路径的 r/d 层级计划（阈值表）
- encode.py：递归编码，输出每叶独立的 {r,d,value?} JSONL 列流
- decode.py：递归阈值读取器 + 多叶合并 + 严格反解校验
- reference.py：独立的栈式状态机参考解码器，仅用于交叉核对
- cli.py / __main__.py：encode、decode 命令行入口
"""

__all__ = ["schema", "planner", "encode", "decode", "reference"]
