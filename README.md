# HDF5 离线交付（单文件 materialisation）

把 `fixtures/root.h5` 及其可达的外部依赖复制成单个 `delivery.h5`，并输出
`manifest.json`。移动或删除原输入目录后，交付文件仍能独立、正确地读取。

范围限定在这一个小型交付闭环：只支持 group、int64 数值 dataset（含空数组）、
字符串/整数普通属性、hard/soft/external link、一维有限 VDS。不支持对象引用、
region reference、压缩滤镜插件、外部 raw storage、复合/enum/vlen/opaque dataset
或并发写入。

## 安装与依赖

Python 3.11，依赖见 `requirements.txt`：

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt     # h5py==3.14.0, numpy>=1.26,<3
```

## 构建

```bash
.venv/bin/python -m hdf5_delivery build fixtures/root.h5 delivery.h5 \
    --manifest manifest.json
```

或使用等价入口 `build_delivery.py`：

```bash
.venv/bin/python build_delivery.py build fixtures/root.h5 delivery.h5
```

退出码 0 表示成功；任何问题都以非零退出并把“源文件 + 对象路径”写进错误信息。
构建先写临时文件，全部成功后才原子替换到目标路径，失败时不会在目标路径留下
可冒充完整交付的文件。默认拒绝覆盖已有目标；需要时加 `--overwrite`。

## 交付语义

**对象身份 = (物理文件 device/inode, HDF5 对象地址)。** 只有两者一起才构成身份。
同一物理文件中的同一对象可以有任意多条硬链接；组也可以硬链接回自身或根组。

* 每个源对象只复制一次，输出代表路径之外的所有可达路径都转成**硬链接**，
  因此对象共享关系在交付中保留；遍历按物理对象去重，硬链接环不会无限递归。
* soft link 沿其所在源文件的真实路径上下文解析；external link 的文件名相对
  该 link 所在源文件的目录解析。二者在输出中转为指向已复制对象的硬链接。
  两个外部路径最终指向同一对象时仍只产生一个输出对象。
* 根目录原有的链接名称保持不变；属性、dataset 的 dtype/shape/数值、fill value
  保持一致；空 dataset 保持为空。
* VDS 逐条 mapping 真实读取并物化成普通 dataset，保留未映射区域的 fill 值。
  读取前检查每个声明的源文件、dataset 和选区（含边界与元素数）确实存在且可用。
  HDF5 对缺失 VDS 源会静默返回 fill，本工具不会把这种静默结果当成功。
* 悬空外链、只由 soft/external link 构成的循环、越界 VDS 源和不支持类型都会
  带源文件/对象路径明确拒绝。
* 输出内部不得残留 soft link、external link 或虚拟 dataset（构建末尾自检，
  并且验证脚本会再次检查）。

`fixtures/dangling.h5`、`fixtures/soft-cycle.h5`、`fixtures/missing-vds.h5`
分别是三条必须失败的输入。

## manifest.json

记录每个已访问源对象的来源文件与对象身份、输出代表路径、别名关系，以及 VDS
映射来源：

```jsonc
{
  "format": "hdf5-delivery-manifest",
  "input":    {"realpath": "...", "file_id": "dev:inode", ...},
  "output":   {"path": "...", "manifest_path": "...", "size_bytes": 11168},
  "source_files": [ {"realpath": "...", "file_id": "dev:inode", "requested_paths": [...]} ],
  "objects": [
    {
      "identity": {"file_id": "dev:inode", "object_address": 1832, "combined": "dev:inode:1832"},
      "source": {"file": "...", "object_path": "/local/values", "kind": "dataset"},
      "output_representative": "/soft_values",
      "aliases": [{"output_path": "/local/values", "pointing_to": "/soft_values", ...}],
      "dataset": {"shape": [3], "dtype": "int64", "fill_value": 0}
    }
  ],
  "links": [ {"output_path": ..., "target": ..., "kind": "hard", "origin": {...}} ],
  "counts": {"objects": 7, "links": 10, "datasets": 4, "groups": 3}
}
```

VDS 对象的 `vds.mappings` 记录每次映射的源文件/对象路径/地址、虚拟选区、源选区
和元素数。由硬链接环产生的无限路径不枚举；`links` 只列有限的可达访问路径。

## 独立验证

```bash
.venv/bin/python verify_delivery.py
```

验证脚本不依赖构建器的内部检查，而是独立地：

1. 从源文件重建期望模型（对象身份、别名、属性、数值、空数组、校验后的 VDS 映射）。
2. 调用 CLI 构建 delivery（或用 `--delivery` 指定已有文件）。
3. 把 delivery 和 manifest 复制到隔离目录，**移动原输入目录**并切换 cwd。
4. 重新打开隔离副本，逐项核对数值/属性/空数组，比较别名对象地址是否相同，
   并确认没有 soft link、external link、虚拟 dataset、外部 raw storage 或引用类型。
5. 分别验证 `dangling.h5`、`soft-cycle.h5`、`missing-vds.h5` 失败，且不留下
   任何交付文件；另外生成并验证越界 VDS 源和 reference dtype 两个负例。

`--input` 可指向别的输入（例如自行构造的夹具）；验证会移动该输入所在目录，
因此必须指向专用目录。可用 `--failure-fixtures` 指定失败夹具目录，`--keep`
保留临时目录以便排查。

## 测试

```bash
.venv/bin/python -m unittest discover -s tests -v
```

测试覆盖：对象共享/别名去重、空 dataset、VDS 物化与 fill、输入删除后仍可读、
三类失败输入和悬空软链错误信息。

## 目录

```
hdf5_delivery/         构建器（identity/links/VDS/materialisation/manifest）
build_delivery.py      命令入口（等价于 python -m hdf5_delivery）
verify_delivery.py     独立验证脚本
tests/test_delivery.py 单元/集成测试
fixtures/              合成输入与失败夹具
```
