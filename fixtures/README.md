# 真实 AcroForm PDF

```sh
python3 -m pip install -r requirements.txt
```

forms.pdf 含两页、字段父子树、同一逻辑文本字段的跨页多个 widget、Unicode 值、Checkbox 过期外观、Choice 导出/显示值、只有默认值的字段与 Radio 组。scope.json 限定支持范围。PDF 是可由 pypdf 直接读取的真实对象文件，不需要截图/OCR。

bad-cycle.pdf 为独立坏例：一个字段的 Kids 包含其祖先自身。读取时用对象引用集合保护遍历，不应递归卡死。逻辑值与 widget AS/AP 是两类证据；文本字段的 AS 名称不用于猜测文本值。支持单/多选 Choice 的通用规则，现有主例使用单选。
