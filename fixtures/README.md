# EPUB3 文本语料输入

book.epub 是真实 ZIP EPUB，包含 mimetype、META-INF/container.xml、OPF 和 XHTML。正文 spine 顺序为 b,a；nav 顺序故意为 a,b；notes 的 linear=no。正文节点限 h1/h2/p；脚注 aside 不重复进入正文。scope.json 定义空白折叠、插入位置的 Unicode codepoint 单位及错误发布策略。

b 文档有继承的 xml:base，需要在规范 ZIP 内部路径上解析 href；共享脚注被不同正文引用，脚注间还有一个环。book.epub 故意含一个 missing 目标：允许产正文与已知脚注，但 complete=false，并报告缺失/循环；不是允许静默忽略坏引用。bad-spine.epub 则有不存在的 idref，整本拒绝。

只需 Python zipfile/XML 解析，不做 EPUB 阅读器或字体布局。ZIP 路径不要通过操作系统文件遍历执行；所有读取在归档资源命名空间内。
