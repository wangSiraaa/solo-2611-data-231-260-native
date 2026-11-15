"""命令行入口：逐个转换 NetCDF 文件，每个文件独立产出一套关系表。

用法：
    python -m cf_rel.cli --mapping mapping.json --out out fixtures/*.nc
"""

from __future__ import annotations

import argparse
import glob
import json
import sys

from .converter import convert_file
from .errors import ConversionError


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="CF trajectoryProfile NetCDF 关系化导入（逐文件转换）")
    parser.add_argument("inputs", nargs="+",
                        help="一个或多个 NetCDF 文件（支持 glob 模式）")
    parser.add_argument("--mapping", default="mapping.json",
                        help="变量名称映射文件（默认 mapping.json）")
    parser.add_argument("--out", default="out",
                        help="输出根目录（默认 out）")
    args = parser.parse_args(argv)

    with open(args.mapping, "r", encoding="utf-8") as fh:
        mapping = json.load(fh)

    # 展开 glob，保持逐个文件独立转换，绝不合并多个文件
    paths: list[str] = []
    for item in args.inputs:
        matched = sorted(glob.glob(item))
        paths.extend(matched if matched else [item])

    if not paths:
        print("没有匹配到任何输入文件", file=sys.stderr)
        return 2

    exit_code = 0
    for path in paths:
        try:
            result = convert_file(path, mapping, args.out)
        except ConversionError as exc:
            # 校验失败：该文件不会发布任何最终关系表
            print(f"[FAIL] {path}: {exc}", file=sys.stderr)
            exit_code = 1
            continue
        note = (f"，unsupported 变量 {len(result.unsupported)} 个"
                if result.unsupported else "")
        print(
            f"[OK] {result.source} -> {result.output_dir} "
            f"layout={result.layout} "
            f"trajectories={result.n_trajectories} "
            f"profiles={result.n_profiles} "
            f"observations={result.n_observations}{note}")
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
