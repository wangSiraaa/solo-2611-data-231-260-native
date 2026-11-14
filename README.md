# 切块图像标注回归原图的实例数据集装配

本目录实现了 `assemble.py`：把按固定切块规则产生、分别标注的 tile 图、
标签 mask、valid mask 及显式实例映射，装配回原图坐标的全图实例数据集。

```sh
python3 -m pip install --break-system-packages -r requirements.txt   # 或使用 venv
python3 assemble.py fixtures/plan.json output/plan
python3 assemble.py fixtures/bad-mapping.json output/bad   # 退出码 2，不写任何产物
python3 verify_assembly.py fixtures/plan.json output/plan  # 独立逐像元核对
python3 test_assemble.py                                   # 内置回归样例
```

## 变换与逆映射

唯一支持的变换顺序（由 scope/plan 固定）：

```
crop -> 整数倍最近邻放大 -> 可选水平翻转
```

程序把 `crop` 视为原图坐标下的**无翻转**区域 `[x, y, w, h]`，tile 存储的是
该区域执行上述顺序后的图像，尺寸必须恰为 `(w*scale, h*scale)`。逆映射
反过来执行：先撤销水平翻转，再按 `scale×scale` 块还原到唯一来源格。

同一来源格内的多个放大像元各自都是独立观测：它们都计入 coverage 计数，
但只把出现的**身份集合**用于状态判定，绝不把标签值做平均或产生新编号。

## 状态与冲突

每个原图像元的身份集合来自所有 `valid=1` 的观测：

| 状态 | 代码 | 条件 |
| --- | --- | --- |
| unknown | 0 | 没有任何有效观测 |
| certain_background | 1 | 唯一身份是背景（label 0 且 valid=1） |
| certain_instance | 2 | 唯一身份是某个全局实例 |
| conflict | 3 | 出现两个及以上不同身份（含背景 vs 实例） |

`valid=0` 是未观测，不贡献任何东西；`label=0 且 valid=1` 才是背景。
冲突不按 tile 先后覆盖，也不进入任何实例的确定 mask。同一 tile 内多个放大
像元互相不一致同样记为冲突（`within_tile_disagreement`）。

## 显式实例映射与整批失败

正标签只通过 `instance_map`（tile-local-id -> global-instance-id）解释，
不按标签数字跨 tile 合并，也不做颜色/几何匹配。以下情况整批拒绝（退出码 2，
不写产物，错误以 JSON 输出）：

- valid 像素中出现未在 `instance_map` 声明的正标签（如 bad-mapping 的 `7`）
- `instance_map` 指向 `instances` 中未声明或类别自相矛盾的全局实例
- crop 越界、非法 scale/flip、crop*scale 与图片/mask/valid 尺寸不符
- valid 值不在 {0,1}、标签为负、transform_order 不是支持的三步

## 输出

| 文件 | 内容 |
| --- | --- |
| `state.png` | 原尺寸状态码（scope.json 的 0/1/2/3） |
| `coverage.png` | 是否至少有一个有效观测 |
| `coverage_count.png` / `.npy` | 每格有效观测数（精确整数） |
| `instance_labels.png` | 每个实例的确定像元标签索引（0 = 非确定实例） |
| `instances/<idx>_<id>_mask.png` | 每实例确定 mask |
| `instances.json` | COCO 风格数据表 + 每实例明细 |
| `instances.csv` | 扁平实例表 |
| `report.json` | 尺寸/变换核对、覆盖统计、冲突明细 |

COCO `segmentation` 为 **uncompressed RLE**：按列优先遍历，首个 run 计零
（全背景 mask 为单个 `[N]`；全前景 mask 以 `[0, N]` 开头）。`area` 与
`bbox` 一律由最终确定 mask 重算；空 mask 的 `bbox` 为 `null`，不沿用 tile
旧值。实例即使无确定像元也保留在数据表内，并用 `empty_reason` 标注原因：

- `not_referenced_by_any_tile_instance_map`：没有任何 tile 映射到该实例
- `mapped_labels_never_observed_in_valid_pixels`：映射存在但从未在有效像元出现
- `all_observed_pixels_are_conflicts`：所有观测都落入冲突格

## 独立核对

`verify_assembly.py` 不调用装配器的逆映射逻辑：它对每个原图格直接枚举
所有 tile 像元，手工撤销翻转与放大，重新得到状态、逐实例 mask、area、bbox
与冲突表，再把输出 RLE 解码回 mask 逐像元比对。`test_assemble.py` 用翻转、
2/3 倍放大、重叠同实例、背景冲突、格内冲突、不完整 valid、空实例、
bad mapping、类别矛盾、越界、尺寸不符等样例覆盖上述规则，并对 RLE 编解码
做随机属性检查。
