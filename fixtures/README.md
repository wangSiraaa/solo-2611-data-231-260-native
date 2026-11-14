# 切块标注装配输入

```sh
python3 -m pip install -r requirements.txt
```

original.png 是 10×8 真 RGB 图，a/b 各有图、单通道标签 mask 与 valid mask。plan.json 给整数 crop、最近邻放大倍数、水平翻转、局部标签到全局实例的显式对应，所有路径相对 fixtures。scope.json 定义状态码及列优先 uncompressed RLE。

a 的变换顺序是 crop→2倍放大→水平翻转；b 为 crop→1倍。图像可用于尺寸/变换检查，数据产物依据给定 mask/valid 与映射，不运行视觉模型。valid=0 是未观测，label=0 且 valid=1 才是背景。材料包含重叠区实例一致与背景冲突，不允许按输入先后覆盖。

bad-mapping.json 故意移除 b 中出现的局部标签 7 的映射，须拒绝完整发布。未知/冲突格不计入实例确定 mask，area/bbox 只从最终确定像元重算。
