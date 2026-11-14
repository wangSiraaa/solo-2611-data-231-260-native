# 乱序FASTQ双端原件的配对数据集生产

本目录含输入模拟环境（`fixtures/`）与数据处理程序（`fastqpair/`，Python 3.11 标准库实现）。
全部样例为本地合成。规则与产物说明见 [docs/USAGE.md](docs/USAGE.md)，
端到端测试与独立重读校验见 `tests/test_pairing.py`。

运行：

```bash
python -m fastqpair --manifest fixtures/manifest.json \
                    --config fixtures/config.json --out out/
python -m unittest tests.test_pairing -v
```

程序只做数据接入：严格边界解析、身份裁定、有界暂存与配对落盘；
不做序列比对、反向互补或共识推断，不改动任何碱基/质量字符。
