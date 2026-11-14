# WebDataset tar 输入

fixtures/shards/*.tar 是三个真实 USTAR 分片，每片四个样本，每个样本 png/json/txt 成员连续排列。manifest.json 中路径相对 fixtures，SHA256 绑定原件；同 key=shared 出现在三个 shard，身份须含 shard_id。config.json 固定派发、seed 派生与缓冲洗牌语义。

先按 manifest 派发 worker，再流式组装该 worker 自己的样本，不能全局读取后再过滤。缓冲 B=3，worker_count=2。源数据不大，但内存样本上限仍按规定。

missing.tar 与 duplicate.tar 独立作为失败输入，不属于正常 manifest。为它们建立单项临时 manifest，并保留对应 SHA。checkpoint 修改/源替换可在独立副本演示；不修改固定正常输入。只输出样本身份/部件摘要/顺序，不涉及模型训练。
