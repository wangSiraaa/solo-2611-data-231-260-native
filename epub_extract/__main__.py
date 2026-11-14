"""命令行入口：python -m epub_extract book.epub [-o out.json]

把抽取结果序列化为 JSON；致命错误（未知 idref / 不支持的媒体类型等）
以非零状态退出，不发布成功语料。
"""

from __future__ import annotations

import argparse
import json
import sys

from .extractor import ExtractionError, extract_epub


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="EPUB3 正文与脚注抽取")
    parser.add_argument("epub", help="EPUB3 文件路径")
    parser.add_argument("-o", "--output", help="JSON 输出路径，缺省打印到标准输出")
    args = parser.parse_args(argv)

    try:
        result = extract_epub(args.epub)
    except ExtractionError as exc:
        print(f"拒绝发布语料: {exc}", file=sys.stderr)
        return 2

    payload = json.dumps(result.to_dict(), ensure_ascii=False, indent=2)
    if args.output:
        with open(args.output, "w", encoding="utf-8") as fh:
            fh.write(payload)
            fh.write("\n")
    else:
        print(payload)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
