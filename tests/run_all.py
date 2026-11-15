"""统一运行入口：给定样例 demo + 全部自动化测试（无需第三方依赖）。

运行：python3 -m tests.run_all
"""

from __future__ import annotations

import tests.conftest  # noqa: F401  确保 pytest 垫片在测试模块导入前注册

from tests import demo, test_byte_robustness, test_extra, test_random_enum
from tests._pytest_shim import run_module


def main() -> int:
    print("#" * 70)
    print("# 1/3 给定样例端到端核对（全顺序 + 四个前缀查询 + 重开）")
    print("#" * 70)
    demo.main()

    failures = 0
    for title, module in (
        ("2/4 边界值/desc 文本/非法输入测试", test_extra),
        ("3/4 字节级健壮性测试（0x02 负载/截断拒绝/极值/完整 tuple）", test_byte_robustness),
        ("4/4 随机小数据枚举测试（无遗漏、无混入、窄前缀收窄）", test_random_enum),
    ):
        print("\n" + "#" * 70)
        print(f"# {title}")
        print("#" * 70)
        failures += run_module(module.__name__)

    print()
    if failures:
        print(f"存在 {failures} 个失败测试")
        return 1
    print("全部测试通过。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
