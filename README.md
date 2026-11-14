# 日志轮转下的原始记录采集与缺口说明

- `fixtures/`：输入与模拟环境（`writer.py`、场景与配置），详见 `fixtures/README.md`。全部样例为本地合成。
- `collector.py`：采集器实现，Python 3.11 单进程、单目录，子命令 `run` / `status` / `export`。
- `verify.py`：验收脚本，独立运行 writer 与采集器，用 truth 核对采集来源范围；truth 仅供验收，采集器不读取。

## 采集

```sh
python3 collector.py run --config fixtures/rename-config.json \
    --log-dir /tmp/log-232-rename --stop-file /tmp/log-232-stop --state-dir /tmp/log-232-state
python3 collector.py status --state-dir /tmp/log-232-state
python3 collector.py export --state-dir /tmp/log-232-state --out-prefix /tmp/log-232-export
```

`rotation_mode` 由配置明确指定（rename / copytruncate），不做猜测。rename 按设备号/inode 跟踪已打开文件，旧文件改名后继续排空，已知 inode 的另一个名字不重复导入；stop_file 出现后排空所有已知文件退出。copytruncate 只跟踪 active 路径，文件变短或已读前缀摘要变化时分代并报告 discontinuity 与旧确认偏移，不声称能证明截断前无丢失。导出中原始字节（`*.records.jsonl`）与规范 JSON（`*.canonical.jsonl`）分别保存，字节范围含终止 LF；坏行隔离在 `*.invalid.jsonl`。

## 验收

```sh
python3 verify.py   # 依次运行 rename / copytruncate / partial 三个场景
```
