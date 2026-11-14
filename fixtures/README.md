# 冻结搜索输入服务

从 C0 根目录运行：

```sh
python3 fixtures/mock_search.py --port 8231 --request-log /tmp/search-231-requests.jsonl --truth-log /tmp/search-231-truth.jsonl
```

POST /open，JSON 为 {"scenario":"normal"}（或 hotspot、saturated、expired），返回 snapshot_id、cap 和整个二维域。POST /search 使用 {"snapshot_id":"返回的身份","t_range":[0,16],"bucket_range":[0,16]}。范围均是整数左闭右开。count 精确，rows 永远最多 cap=4。相同条件反复请求不产生后页。

normal 有分散记录；hotspot 有相同 t 的密集记录；saturated 有同一 t/bucket 内超过 cap 的不同 ID；expired 允许前三次搜索，从第四次起返回 HTTP 410。服务对已打开快照保持冻结，模拟 live 列表另行追加。请求/真值日志仅用于独立验收，采集程序只能访问 /open 和 /search。服务原件 scenarios.json 也不得作为采集程序输入。

HTTP 400 表示请求不符合契约，404 表示无此快照/接口。按 Ctrl-C 关闭输入服务；测试可使用 --port 0 --ready-file /tmp/search-ready.json 取得临时端口。
