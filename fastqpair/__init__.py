"""乱序 FASTQ 双端原件配对工具（仅数据接入，不做任何生物信息推断）。"""

from .engine import RunResult, run_pairing
from .model import Config, FatalTail, QuarantineEntry, SourceSpec, ValidRecord

__all__ = [
    "Config",
    "FatalTail",
    "QuarantineEntry",
    "RunResult",
    "SourceSpec",
    "ValidRecord",
    "run_pairing",
]
