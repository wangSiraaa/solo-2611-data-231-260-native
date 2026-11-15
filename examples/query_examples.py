"""查询示例：从已物化的双时态历史表读取（不重放修订事件）。

运行：python examples/query_examples.py
"""

from __future__ import annotations

import sys
from pathlib import Path

# 允许从仓库根目录直接运行：python examples/query_examples.py
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bitemporal.store import query, slice_at

DB = "history/bitemporal.db"


def show(title: str, entity: str, valid_at: int, known_at: int) -> None:
    result = query(entity, valid_at, known_at, DB)
    print(f"{title}")
    print(f"  query({entity!r}, valid_at={valid_at}, known_at={known_at})")
    print(f"  -> found={result['found']}, value={result['value']!r}")
    if result["row"]:
        row = result["row"]
        print(
            "     命中历史段: valid=[{vf},{vt}) sys=[{sf},{st})".format(
                vf=row["valid_from"],
                vt=row["valid_to"] if row["valid_to"] is not None else "∞",
                sf=row["sys_from"],
                st=row["sys_to"] if row["sys_to"] is not None else "∞",
            )
        )
    print()


def main() -> None:
    show("迟到有效期覆写：K=1 只知道 north", "c1", 4, 1)
    show("同一有效点在 K=2 被 south 覆写", "c1", 4, 2)
    show("覆盖后局部撤回：K=3 的 valid_at=6 已无事实", "c1", 6, 3)
    show("撤回区间再填入：K=4 的 valid_at=7 为 east", "c1", 7, 4)
    show("知识时点恰等提交时刻：K=5 立即可见开放尾", "c1", 100, 5)
    show("开放尾之外没有事实（负时间查询 c1）", "c1", -1, 5)
    show("assert null：存在事实且值为 null", "c2", 0, 6)
    show("retract 与 assert null 的区别：c1 撤回点 not found", "c1", 6, 3)

    print("知识切片 slice_at('c1', K=3) 的全部可见段：")
    for row in slice_at("c1", 3, DB):
        print(
            "  valid=[{vf},{vt}) value={val!r} (sys_from={sf})".format(
                vf=row["valid_from"],
                vt=row["valid_to"] if row["valid_to"] is not None else "∞",
                val=row["value"],
                sf=row["sys_from"],
            )
        )


if __name__ == "__main__":
    main()
