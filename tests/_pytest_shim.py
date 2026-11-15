"""零依赖 pytest 垫片：仅实现测试套件实际用到的 API。

环境中若安装了真正的 pytest，优先使用官方包；否则由本模块提供
pytest.raises 上下文管理器与 test_ 函数收集执行能力，避免额外依赖。
"""

from __future__ import annotations

import contextlib
import inspect
import sys
import traceback


@contextlib.contextmanager
def raises(exc_type):
    try:
        yield
    except exc_type:
        return
    except Exception as exc:  # 抛出了其他异常类型，也算失败
        raise AssertionError(
            f"期望抛出 {exc_type.__name__}，实际抛出 {type(exc).__name__}: {exc}"
        ) from exc
    raise AssertionError(f"期望抛出 {exc_type.__name__}，但未抛出任何异常")


def run_module(module_name: str) -> int:
    """收集并执行模块内所有 test_ 开头的无参函数，支持 tmp_path 夹具。"""
    module = sys.modules[module_name]
    tests = [
        (name, obj)
        for name, obj in inspect.getmembers(module, inspect.isfunction)
        if name.startswith("test_") and obj.__module__ == module_name
    ]
    failures = 0
    for name, fn in sorted(tests):
        params = inspect.signature(fn).parameters
        kwargs = {}
        if "tmp_path" in params:
            import tempfile
            from pathlib import Path

            kwargs["tmp_path"] = Path(tempfile.mkdtemp(prefix=f"orderkey_{name}_"))
        try:
            fn(**kwargs)
            print(f"  PASS {module_name}.{name}")
        except Exception:
            failures += 1
            print(f"  FAIL {module_name}.{name}")
            traceback.print_exc()
    print(f"{module_name}: {len(tests) - failures}/{len(tests)} 通过")
    return failures
