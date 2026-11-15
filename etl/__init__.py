"""物理排序性质驱动的微型 ETL 规划器/执行器。

只支持 source / 稳定 filter / project(改名删列) / 顺序 concat / stream_group，
以及规划器注入的显式 sort 节点。不解析 SQL，不做 join、缓存、并行或成本优化。
"""

__version__ = "1.0.0"
