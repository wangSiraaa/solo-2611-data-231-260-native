#!/usr/bin/env bash
# 一键核对：安装依赖（如使用虚拟环境请先激活），运行全部单元测试，
# 再用真实 fixtures 走一遍 CLI 并确认两个 bad 文件被整体拒绝。
set -euo pipefail

cd "$(dirname "$0")"

PYTHON="${PYTHON:-python3}"
if [ -d ".venv" ]; then
  PYTHON=".venv/bin/python"
fi

echo "== 依赖 =="
"$PYTHON" -c "import netCDF4, numpy" 2>/dev/null || \
  "$PYTHON" -m pip install -r requirements.txt

echo "== 单元测试 =="
"$PYTHON" -m unittest discover -s tests -v

echo "== CLI：正常文件转换 =="
rm -rf out
"$PYTHON" -m cf_rel.cli --mapping mapping.json --out out \
  fixtures/contiguous.nc fixtures/indexed.nc

echo "== CLI：bad 文件必须失败且不发布关系表 =="
set +e
"$PYTHON" -m cf_rel.cli --mapping mapping.json --out out \
  fixtures/bad-count.nc fixtures/bad-index.nc
rc=$?
set -e
if [ "$rc" -ne 1 ]; then
  echo "期望 bad 文件转换返回 1，实际 $rc" >&2
  exit 1
fi
if [ -e "out/bad-count" ] || [ -e "out/bad-index" ]; then
  echo "bad 文件不应发布任何输出目录" >&2
  exit 1
fi

echo "全部核对通过。"
