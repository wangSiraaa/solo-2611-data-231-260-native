"""WebDataset 风格 tar 分片的流式读取、有界洗牌与可恢复续跑。

仅依赖 Python 3.11 标准库；不包含任何模型训练逻辑。
"""

from .errors import (
    CheckpointError,
    ConfigError,
    DataAssemblyError,
    WDSStreamError,
)
from .manifest import Config, Manifest, load_config, load_manifest, worker_seed
from .tarstream import Sample, SampleStream
from .shuffle import WorkerRunner

__all__ = [
    "CheckpointError",
    "ConfigError",
    "DataAssemblyError",
    "WDSStreamError",
    "Config",
    "Manifest",
    "load_config",
    "load_manifest",
    "worker_seed",
    "Sample",
    "SampleStream",
    "WorkerRunner",
]
