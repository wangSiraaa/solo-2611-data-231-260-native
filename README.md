# HTML Microdata实体引用的结构化采集

`fixtures/` 为本地合成的冻结输入（catalog.html 主输入，variant.html 仅调整
HTML 属性与 itemref 顺序），范围语义见 `fixtures/README.md` 与
`fixtures/scope.json`。

实现为 `microdata_reader.py`，仅使用 Python 3.11 标准库（内置 HTML 解析器），
不联网、不推断 schema.org 业务规则。

```bash
# 人类可读摘要
python3 microdata_reader.py fixtures

# 完整记录（每实体：类型、身份来源、属性出现、来源节点、路径、value_kind、诊断）
python3 microdata_reader.py fixtures --json --pretty

# 跨文档目录：同 itemid 实体合并身份，保留所有描述与属性出现
python3 microdata_reader.py fixtures --catalog --pretty --out out/catalog.json

# 语义等价核对：忽略来源位置，比较实体与值多重集合
python3 microdata_reader.py fixtures --compare catalog.html variant.html

# 测试
python3 -m unittest -v test_microdata_reader.py
```

主要输出字段：

- 实体：`id`、`identity_kind`（`itemid`/`anonymous`）、`identity_source`、
  `itemid`/`itemid_raw`、`itemtype`、`descriptions`、`complete`
- 属性出现：`name`、`itemprop`、`value`、`value_kind`
  （`text`/`url`/`entity`/`missing`/`unsupported`）、`source_document`、
  `source_node_id`、`source_path`、`path`（`direct`/`itemref`）、`via`、
  `attribute`、`target_entity`
- 引用诊断：`references` 与 `diagnostics`（`invalid_itemref`、
  `cross_document_id`、`cyclic_itemref`、`nested_boundary`、
  `unsupported_element`、`missing_required_attribute`、`base_missing_href` 等）
