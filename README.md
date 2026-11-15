# CF trajectoryProfile 不等长观测关系化导入

把 CF-1.10 `featureType=trajectoryProfile` 的 NetCDF 文件，逐个转换为
trajectory / profile / observation 三张关系表（JSONL）加一份变量元数据
（`variables.json`）。只支持 **trajectory → profile → observation 两层**
结构，不做插值、轨迹匹配或任何科学推断。

## 背景语义（全部以文件为准核对）

`mapping.json` 只给出变量**名称**；以下语义均从每个 NetCDF 文件实际读取并核对：

- 维度名（`trajectory` / `profile` / `obs`）及其长度；
- `trajectory_id`、`profile_id` 变量上的 `cf_role`；
- `trajectoryIndex` / `profileIndex` 上的 `instance_dimension`；
- `rowSize` 上的 `sample_dimension`；
- `_FillValue` / `missing_value`、`scale_factor` / `add_offset`、
  `units`、`calendar` 等属性。

观测层两种布局**互斥**，由文件中实际存在哪个变量决定：

- **contiguous**：profile 上的 `rowSize` 对 obs 维度连续分段，允许 0，
  必须非负且总和等于 obs 维度长度；
- **indexed**：obs 上的 `profileIndex` 显式给出每条观测所属剖面，
  观测可以交错；索引必须非缺失、非负且在 profile 维度界内。

`trajectoryIndex`（profile 上，0 基）把剖面关联到轨迹，同样校验非缺失、
在界内。所有归属只走索引链，**不按表行号对齐、不按 ID 排序猜测**。

## 安装

```bash
python3.11 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

依赖见 `requirements.txt`：`netCDF4==1.7.2`、`numpy>=1.26,<3`。

## 使用

逐文件独立转换，每个 nc 输出到 `out/<文件名去后缀>/`，绝不合并多个文件：

```bash
.venv/bin/python -m cf_rel.cli --mapping mapping.json --out out \
    fixtures/contiguous.nc fixtures/indexed.nc
```

任一文件校验失败时：该文件**不发布任何最终关系表**，CLI 打印含
文件 / 变量 / 索引的诊断并以非零码退出，其他文件的结果不受影响。

也可作为库调用：

```python
from cf_rel import convert_file
result = convert_file("fixtures/indexed.nc", mapping, "out")
```

## 输出结构

```
out/<stem>/
├── trajectory.jsonl    # 每行一条轨迹
├── profile.jsonl       # 每行一个剖面（含零观测剖面）
├── observation.jsonl   # 每行一条观测，含完整继承视图
└── variables.json      # 全量变量盘点与元数据
```

### trajectory.jsonl

```json
{"trajectory_index": 0, "trajectory_id": "T-a",
 "variables": {"campaign": "alpha"}}
```

### profile.jsonl

保留原始 profile 索引与 `profile_id`，并通过 `trajectory_index` /
`trajectory_id` 关联轨迹；`obs_count` 为该剖面实际观测数
（零观测剖面照常入表）。contiguous 布局另带原始 `row_size`。

```json
{"profile_index": 1, "profile_id": "P-1", "trajectory_index": 0,
 "trajectory_id": "T-a", "obs_count": 0,
 "variables": {"time": 20, "time_utc": "2026-01-01T00:00:20Z",
               "latitude": 41.0}}
```

### observation.jsonl

保留源文件名与原 obs 索引，正确关联 profile 与 trajectory；
`inherited` 是沿索引链广播的上级属性完整视图（scalar / profile /
trajectory 三层），在每个 profile 分段内同源取值，不会复制错位。

```json
{"source_file": "indexed.nc", "observation_index": 1,
 "profile_index": 0, "profile_id": "P-0",
 "trajectory_index": 1, "trajectory_id": "T-b",
 "variables": {"depth": 2.0, "quality": 1},
 "inherited": {
   "scalar": {"station_note": "synthetic"},
   "profile": {"time": 10, "latitude": 40.0,
               "time_utc": "2026-01-01T00:00:10Z"},
   "trajectory": {"campaign": "beta"}}}
```

### variables.json

记录布局、维度长度、各表变量清单，以及每个变量的 `dimensions`、原始
`dtype`、`units`、`fill_value`、`missing_value`、`scale_factor`、
`add_offset`、`calendar` 和全部原始属性；结构变量（ID 与索引变量）
标记 `structural`。不满足“标量或单维且维度属于三层结构”的变量出现在
`unsupported` 中并注明原因，不进入任何关系表。

## 数值与缺测规则

- 读取时关闭 netCDF4 的自动 mask / 自动 scale；
- 先拿**存储值**与变量自身的 `_FillValue`、`missing_value`（按存储
  dtype）比对，命中即输出 JSON `null`；未命中再应用
  `scale_factor` / `add_offset`；
- 数值 `0` 与空字符串 `""` 都不是缺测，原样保留；字符串变量原样保存；
- 时间仅支持 `standard`/`gregorian` 日历与 `seconds since <参考点>`，
  输出 UTC ISO8601（`Z` 后缀），原始数值同时保留；其他日历 / 时间单位
  显式报错，不做猜测转换；
- JSON 禁用 NaN/Infinity（`allow_nan=False`）。

## 校验规则（发布前失败，诊断含文件 / 变量 / 索引）

- 结构维度缺失、`featureType` 不是 trajectoryProfile；
- 结构变量缺失、维度不符或 `cf_role` / `instance_dimension` /
  `sample_dimension` 属性不符；
- rowSize 与 profileIndex 同时存在（互斥）或都不存在；
- `rowSize` 为负，或总和不等于 obs 维度（bad-count）；
- `profileIndex` / `trajectoryIndex` 缺失或越界（bad-index）；
- 轨迹 / 剖面业务 ID 不唯一；
- mapping 选中的变量在文件中不存在，或选中了结构变量；
- 不支持的日历 / 时间单位、多维或非数值字符串 dtype 变量。

写出采用“临时目录 + 整目录原子替换”，校验全部通过后才发布，
失败时不留半成品，也不会破坏已有的成功输出。

## 自动核对

```bash
./check.sh
# 或直接：
.venv/bin/python -m unittest discover -s tests -v
```

`tests/test_converter.py` 独立打开源 NetCDF、关闭自动 mask/scale，
自行按索引变量推导每条观测的父键与继承值后逐行比对（不复用转换器
内部解码，也不展平 zip）。覆盖：

- contiguous 的 rowSize 分段（含零观测 P-1）与 indexed 的交错观测；
- 逆序轨迹归属（`trajectoryIndex=[1,0,1]`）；
- packed int16 填充值先于 scale/offset 识别、int8 合法 0 与缺测区分；
- 标量广播、空字符串、missing_value、时间 ISO8601；
- bad-count / bad-index 在发布前失败且无输出目录；
- 越界 / 缺失索引、负 rowSize、双布局并存、重复 ID、多维变量
  unsupported、两文件不合并等合成场景；
- CLI 子进程端到端退出码与诊断。

## 目录

```
cf_rel/            转换包（converter.py 核心、cli.py 入口、errors.py）
tests/             unittest 自动化核对
fixtures/          真实 NetCDF：contiguous / indexed / bad-count / bad-index
mapping.json       仅声明变量名称与布局变量名
check.sh           一键核对
requirements.txt   依赖
```
