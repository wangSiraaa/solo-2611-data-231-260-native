# 真实轮转日志输入器

为每次场景使用全新的空目录；truth 与 stop 必须放在目录外。先启动采集器观察日志目录，再启动 writer，或在 writer 的 start-delay 期间启动采集器：

```sh
python3 fixtures/writer.py --scenario rename --log-dir /tmp/log-232-rename --truth-file /tmp/log-232-truth.jsonl --stop-file /tmp/log-232-stop
```

另运行 copytruncate、partial，各自换新的路径。每场景对应 rename-config.json / partial-config.json / copytruncate-config.json，明确 rotation_mode；config.json 为共同参数。rename/partial 的 rotation_mode=rename；copytruncate 的 rotation_mode=copytruncate。active 文件可能在采集器开始后才创建，须等待出现。writer 默认启动延迟 0.5 秒，各阶段间隔 0.35 秒；可用 --start-delay 和 --interval 调整，采集器仍按 config 约 0.03 秒轮询，read_chunk_bytes=7。

rename 会先写半个中文 UTF-8 字符所在的行，改名后向旧句柄补齐，并交错向新 active 写行。copytruncate 复制归档后原地清空同 inode 再写；采集器不得把归档重采。partial 会把一行拆成未闭合 UTF-8、缺 LF、最终 LF 三阶段，之后有完整坏 JSON 和合法后续行。stop 出现表示 writer 已关闭全部句柄；此时才排空所有已跟踪文件退出。

truth 为每次实际 write 的字节片段、inode、代及范围，仅供验收合并原字节；不是采集器输入。日志业务 id 不用于补缺或去重。copytruncate 必须报告无法证明连续，即使当前样例被完整收集。
