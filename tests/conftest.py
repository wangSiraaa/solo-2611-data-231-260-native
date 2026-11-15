"""优先使用真实 pytest；缺失时加载零依赖垫片。"""

import sys

try:
    import pytest  # noqa: F401
except ImportError:
    from tests import _pytest_shim as pytest  # type: ignore

    sys.modules["pytest"] = pytest
