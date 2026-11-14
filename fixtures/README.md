# 带内联修订的 DOCX 输入

revisions.docx 为真实 WordprocessingML 包；读取 word/document.xml，scope.json 限定主文档普通段落/简单表格以及内联 ins/del/moveFrom/moveTo。只生成数据视图，不实现 Word 完整接受修订功能。

移动的 source 与 target 各有自己的 RangeStart/End id。两侧以 start 的相同 w:name 配对；内部 moveFrom 与 moveTo 的修订 id 可以不同，不能拿这两个 id 相等作为配对条件。给定例 name=moveA、源 range id=10/内部 change id=11、目标 range id=20/内部 change id=21。每侧范围在一个段落内，仅含一个对应移动容器。

bad-move.docx 删除目标侧 RangeStart/End 但保留 moveTo 容器，必须报告不可证明移动配对。原始/最终两视图按 scope 分别过滤修订，容器可有多个 run；完整删除的段落在最终视图保留空文本和结构身份。无需渲染、页码计算或调用 Word。
