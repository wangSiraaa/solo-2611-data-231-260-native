"""显式失败类型：装配错误与 checkpoint 绑定错误分开，便于 CLI 区分退出原因。"""


class WDSStreamError(Exception):
    """本包所有自定义错误的基类。"""


class ConfigError(WDSStreamError):
    """config / manifest 内容非法或互相矛盾。"""


class DataAssemblyError(WDSStreamError):
    """tar 流装配样本失败：缺件、重复部件、非法路径、超大小等。

    失败是确定性的：worker 立即中止，绝不使用下一个 key 的部件补救当前样本。
    """


class CheckpointError(WDSStreamError):
    """checkpoint 自身损坏，或其绑定的 config / manifest / 源摘要不再匹配。"""
