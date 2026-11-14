# Wrapped FASTQ 双端输入

manifest.json 按文件给默认 mate；路径相对 fixtures。R1.fastq 为普通文件，R2.fastq.gz 是真实 gzip。config.json 给 header 解析、质量/边界规则与暂存窗口。readA 的序列/质量为多行，质量含 @/+，不能按这些首字符错误地切成下一记录。

正常清单中有乱序合法配对、同名同 mate 重复、单侧孤儿和头部 mate 自相矛盾。truncated.fastq 独立加入坏尾运行，其最后一条质量不足。不要通过扫描下一个 @ 猜测恢复边界。

来源范围对普通文件是 FASTQ 原字节偏移，对 gzip 是解压后 FASTQ 流偏移；另保存压缩源文件 SHA256 与 compression=gzip。范围包括原头、包装行、分隔行和行尾，不是规范输出的重排位置。使用 Python gzip/SQLite 等成熟库，不要求生物学推断。
