# Microdata 小型冻结页面

catalog.html 为主输入；pages.json 给 document_id 与原始页面 URL；scope.json 定义支持的受限 Microdata 规则。variant.html 与 catalog.html 仅调整 HTML 属性及 itemref 顺序，替换文件内容时保持 document_id 和 URL，再对比实体及值多重集合。

可用 Python HTML 解析库，例如 lxml.html；不需要浏览器或联网。itemref 只在 itemscope 节点解释，普通 div 上的 itemref 不会递归产生实体引用。一个 item 内重复到达同一 DOM 节点不重复产属性；两个不同源节点的相同属性值仍分别保留。

材料含同 IRI 多处描述、嵌套实体、共享/重复 itemref、无 itemprop 的独立实体、坏目标与含 &amp; 的值。bad 对象的已知属性仍可输出，但完整性为 false 并保留诊断。
