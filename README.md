# EPUB 正文与脚注的可追溯语料生产

把小型文本 EPUB3 整理成**正文语料 + 独立脚注 + 引用关系**，而不是按文件名拼接
ZIP 内的 XHTML。Python 3.11、仅标准库（`zipfile` / `xml.etree` / `urllib`），
不做阅读器、排版、OCR、CSS 可见性推断、全文搜索或跨格式转换。输入样例说明见
`fixtures/README.md`。

## 运行

```bash
python3 -m epub_extract fixtures/book.epub            # JSON 打到标准输出
python3 -m epub_extract fixtures/book.epub -o out.json
python3 -m unittest discover -s tests -t .            # 全部测试
```

致命结构问题（未知 spine idref、不支持的正文媒体类型、XML/容器损坏）抛
`ExtractionError`，CLI 退出码 2，**不发布成功语料**。引用类问题（目标缺失、
目标不是 footnote、外部 URL、引用环）不阻断产出，登记在 `errors` 且
`complete=false`，坏引用仍保留在来源单位的 `refs` 中，绝不静默跳章或猜脚注。

## 抽取规则

1. `META-INF/container.xml` 定位 OPF；按 manifest 解释资源，按 spine 的
   `itemref` 顺序处理 `linear=yes`/未声明的 XHTML；`linear=no` 不自动进正文，
   只能被 noteref 按需加载。nav 目录顺序与文件名均不代表阅读顺序。
2. 正文单位仅限 `h1/h2/p`，文档内按 DOM 树序；`aside[epub:type~=footnote]`
   中的同类单位只进 notes，不重复进正文；`script/style` 不产出文本。
3. 内联元素按 DOM 顺序混合；实体由 XML 解析器解码；HTML ASCII 空白连续段
   折叠为一个空格并修剪首尾（全角 U+3000 保留为普通字符）。
4. `a[epub:type~=noteref]` 的锚点文本（如 `[1]`）不计入规范正文，改为记录
   它在规范正文中的 Unicode 码点插入位置 `insertion_index` 与目标引用，
   脚注编号不会被当成正文词。
5. href 按节点继承的 `xml:base` 解析，目标为“归档路径 + fragment”；
   跨章节多个 noteref 指向同一 footnote 时共享一个脚注实体，每次引用各自
   保留来源（`referrers`）。脚注内可再引用脚注，环检测以 `path#id` 为身份，
   遇环保留诊断并终止遍历，不重复导出脚注正文。

## 三套互相独立的序号

| 坐标 | 字段 | 含义 |
| --- | --- | --- |
| 阅读顺序 | `body[].reading_index` | spine + 树序下的正文单位序号 |
| 脚注出现顺序 | `notes[].note_order` | 脚注首次被引用的发现顺序 |
| 引用位置 | `refs[].insertion_index` | noteref 在该单位规范文本中的码点偏移 |

每条正文/脚注都带 EPUB 摘要（title/identifier）、内部文档路径、节点 id、
树位置 `tree_position`、规范文本与引用列表，便于独立回溯源节点。

## 代码布局

```
epub_extract/
  model.py       # TextUnit / Footnote / NoteRef / RefError / ExtractionResult
  extractor.py   # container/OPF 解析、spine 遍历、xml:base、空白折叠、环检测
  __main__.py    # CLI
tests/
  epub_fixtures.py    # 合成 EPUB 的测试夹具
  test_extractor.py   # 含独立重取源节点的参考实现核对
  test_cli.py
```
