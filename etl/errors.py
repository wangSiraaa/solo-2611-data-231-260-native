"""统一的异常类型。"""


class EtlError(Exception):
    """所有 ETL 规划/执行错误的基类。"""


class PlanError(EtlError):
    """计划结构非法或无法完成规划。"""


class OrderingError(EtlError):
    """真实数据违反声明/要求的物理顺序。"""


class ExecutionError(EtlError):
    """执行期约束被破坏（如 stream_group 输入不连续有序）。"""
