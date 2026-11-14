# 坐标标注与已给定 token

documents.json 绑定 doc.txt 原始 UTF-8 摘要；tokens.json 给主 token 序列。annotations.json 的三个 emoji 标注在不同 layer 使用 utf8/codepoint/utf16 三种单位，须指向相同原始范围。一个 emoji 被切为两个 token，单个 piece 不必能独立 UTF-8 解码。原文含预组合与分解重音，禁止归一化。

scope.json 给 BILOU 和冲突规则。tokens-internal-special.json 是同一原文的独立 token 变体：emoji 的两个实际 token 之间插入 offset=null 的特殊 token。因此跨这两块的实体不可表示；特殊 token 始终 O，不能制造 B/O/L 伪完整实体。正常 tokens.json 仅有最前面的特殊 token，不影响后续实体。

无效与 partial-token 标注不参与同层 overlap 判定；可表示的同层重叠实体全部 conflict。用已给原文、坐标和 token 做数据投影，无须 tokenizer 或语言模型。
