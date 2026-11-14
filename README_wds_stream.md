# wds_stream：WebDataset tar 分片流式读取、有界洗牌与可恢复续跑

纯 Python 3.11 标准库实现，**不训练模型、不依赖分布式框架、不做采样权重或自动重试**。
输入是 WebDataset 风格的 USTAR tar 分片：成员连续排列、文件名为 `key.part`，
完整样本恰好包含 config 规定的三个部件（png/json/txt）。

## 目录

```
wds_stream/
  errors.py      # 显式错误类型：装配错误 / 配置错误 / checkpoint 错误
  manifest.py    # manifest、config 加载，源摘要，SHA256 seed 派生
  tarstream.py   # tarfile r| 流式组装：缺件/重复/非法路径/超大小显式失败
  shuffle.py     # B 上限的替换式流式洗牌、缓冲快照与按偏移重建
  checkpoint.py  # checkpoint 原子写、自哈希、manifest/config/源/worker 绑定
  audit.py       # 合并覆盖审计、序列逐项审计、checkpoint 绑定审计
  cli.py         # read / resume / audit 三个子命令
tests/           # 29 个 unittest 用例（含全部 N 位置续跑、故障、绑定失效）
demo.sh          # 端到端演示脚本
fixtures/        # 固定输入（只读）：3 个正常 shard + missing/duplicate 故障 tar
```

## 关键语义

### 1. worker 派发（按 manifest 顺序取模）

`shard 序号 % worker_count == worker_id`。每个 worker 只实例化自己名下的 shard，
物理上不会打开其他 worker 的 tar。样例配置 `worker_count=2`：

- worker 0 → shard `0`, `2`（8 个样本）
- worker 1 → shard `1`（4 个样本）

### 2. 样本身份

身份是 `shard_id/key`，**不是裸 key**。三个 shard 中同名的 `shared` 样本保持为
`0/shared`、`1/shared`、`2/shared` 三个独立身份，不会被合并。

### 3. 流式装配（`tarfile` 流式模式，不自实现 tar 编解码）

- 成员名必须匹配 `^[A-Za-z0-9_]+\.[A-Za-z0-9_]+$`，任何路径分隔都是非法路径；
- key 切换、shard 切换或 EOF 时强制校验当前样本，缺件立即 `DataAssemblyError`，
  **不会把下一个 key 的部件补给上一样本**；
- 重复部件、非普通文件、成员超过 `max_member_bytes` 同样显式失败；
- 内存中除“当前正在组装的一个样本”外不保留其他原始部件。

### 4. 有界洗牌（内存完整样本数 ≤ B）

严格按 config 文字实现，使用 `random.Random`（seed 由 config 的 SHA256 规则派生）：

1. 先装满最多 B 个完整样本（填充期不调用 RNG、不输出）；
2. 每来一个新样本：`randrange(len(buffer))` 选位 → 输出该位 → 用新样本替换；
3. 源结束后：反复 `randrange` 选位并 `pop`，直到缓冲清空。

既不把全部样本装入内存再 shuffle，也不做固定排序伪装随机。

### 5. checkpoint 与恢复（不从头重放）

在输出第 N 条后保存，状态包含：

- 下一源位置 `(manifest 级 shard 索引, 下一个 tar 头的绝对字节偏移)`；
- 尚未输出的洗牌缓冲（每条部件的成员头偏移、大小、sha256，不含原始字节）；
- `random.Random` 的完整内部状态；
- 已消费数量 `consumed`。

恢复时：

- 直接在保存的字节偏移上重新 `seek` 开 `r|` 流，不重放已消费成员；
- 缓冲样本按保存的成员偏移**重新打开 tar 只读自己那几个成员**，并核验成员名、
  大小与 sha256；它们重新进入缓冲而不会被再次输出；
- RNG 内部状态原样还原，因此续跑序列与不中断序列逐项完全相同。

偏移计算不使用带内部缓冲的 `fileobj.tell()`，而是
`流基准 + TarInfo.offset + 512 + ceil(size/512)*512`，保证指向真实下一头。

### 6. 绑定失效即拒绝

checkpoint 自带规范内容的 sha256（防直接篡改），并绑定：
manifest 内容哈希与路径、config 绑定子集摘要（seed/epoch/worker_count/B/parts/
上限/派生与洗牌语义）、源摘要（manifest 哈希 + 每个 shard 实际 sha256 的链式哈希）、
worker_id。任一不匹配都会报出具体字段并拒绝续跑。

## 命令用法

```bash
# 从头运行 worker 0；在输出第 3 条后存 checkpoint 并正常退出
python3 -m wds_stream read --config fixtures/config.json \
    --manifest fixtures/manifest.json --worker-id 0 \
    --output out/w0.jsonl --checkpoint out/w0.ckpt --stop-after 3

# 从 checkpoint 恢复并跑到源与缓冲全部清空（默认追加到同一输出文件）
python3 -m wds_stream resume --config fixtures/config.json \
    --manifest fixtures/manifest.json --worker-id 0 \
    --checkpoint out/w0.ckpt --output out/w0.jsonl

# 审计
python3 -m wds_stream audit merge --config fixtures/config.json \
    --manifest fixtures/manifest.json --outputs out/w0.jsonl out/w1.jsonl
python3 -m wds_stream audit sequence --full out/full.jsonl --resumed out/resumed.jsonl
python3 -m wds_stream audit checkpoint --config fixtures/config.json \
    --manifest fixtures/manifest.json --checkpoint out/w0.ckpt --worker-id 0
```

输出为 JSONL，每行含 `seq`（1-based 消费次序）、`sample_id`、`shard_id`、`key`
与每个部件的摘要（part/size/sha256/header_offset/data_offset），不含原始字节。

## 演示与测试

```bash
./demo.sh                 # 全部要求场景的端到端演示（故障与绑定演示在 output/ 副本进行）
python3 -m unittest discover -s tests -v
```

演示覆盖：B=3 / worker_count=2 派发、长短部件摘要、同 key 跨 shard 身份、
checkpoint 续跑等价、独立合并 12 个合法样本各一次、缺件 / 重复部件 / 非法路径 /
超大小失败、config 与源变动及 checkpoint 篡改的绑定拒绝。
