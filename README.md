source.csv有UTF8 BOM、表头可选引号、混合CRLF/LF记录终止符、字段内CRLF、转义引号、中文、空字段及末行无终止符。patch的row从表头后的第一逻辑记录起0基，column为0基物理列序；expected为解码后精确字符串，不做换行归一化。两个坏patch分别旧值错误和同cell重复，malformed用于词法拒绝。原件必须只读，输出另一文件。
