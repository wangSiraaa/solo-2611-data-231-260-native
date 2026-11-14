# PDF AcroForm填写值的一致字段数据采集

本目录仅为输入与模拟环境，不包含应实现的数据处理程序。全部样例为本地合成。具体范围及命令见fixtures/README.md。

## 实现（已提供）

- `acroform_extract.py` — 采集模块，提供 `extract()` / `inspect()` / `AcroFormExtractor` 及 CLI。
  只读遍历 AcroForm 的 Fields/Kids/Parent 树：非终端字段部分名拼成完整逻辑名并保留
  名称分段与对象引用；同一终端字段的多个 widget（可跨页）只产出一条记录；
  FT/Ff/V/DV 沿父链继承；字段环明确抛 `AcroFormStructureError`，断裂父链与归属
  冲突记 diagnostics；字段 /V 为逻辑值，widget /AS、/AP 仅作外观证据
  （不一致标 `appearance_mismatch`，无证据标 `unknown`）；缺 /V 记 `missing`，
  不用 /DV 冒充。`inspect()` 用独立遍历（字段树 + 页面 Annots + 全 xref 扫描）
  核对字段数量与页面引用，`get_fields()` 仅作对照注记。
  范围：普通 AcroForm 小文件；不支持 XFA、签名验证、脚本计算或通用 PDF 修复。
- `make_extra_fixtures.py` — 生成补充测试 PDF（给定样例未覆盖的情形）。
- `test_acroform_extract.py` — 核对测试（给定 fixtures + 补充样例，pypdf 5/6 均通过）。

```sh
python3 -m pip install -r requirements.txt
python3 acroform_extract.py extract fixtures/forms.pdf    # 字段数据及页面控件证据 JSON
python3 acroform_extract.py inspect fixtures/forms.pdf    # 独立核对报告
python3 acroform_extract.py extract fixtures/bad-cycle.pdf # 字段环：明确失败，退出码 2
python3 make_extra_fixtures.py output                     # 生成补充测试 PDF
python3 -m pytest test_acroform_extract.py -q             # 28 项核对测试
```
