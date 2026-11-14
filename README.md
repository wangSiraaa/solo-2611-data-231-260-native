# LDIF目录变更的子树身份与关系快照

本目录仅含任务输入与可运行测试设施，不含待实现的处理器。

目录离线导出需要把LDIF初始条目和后续变更物化为可查询快照，不能把DN当普通逗号分隔路径。请用Python 3.11实现load和apply，输入已给出的base.ldif、changes.ldif与profile.json，输出entries、attributes和member_edges三张JSONL或SQLite表。entryUUID是不可修改的条目身份，DN是可变位置；保留多值属性，不压成单个标量。
支持指定文本属性、LDIF折行和双冒号base64 UTF-8文本，以及add、modify、delete、modrdn四类change record。DN支持反斜线转义分隔符和十六进制UTF-8转义、多AVA RDN；属性类型不区分大小写，RDN属性值按精确Unicode字符串比较，不自动casefold或归一化，多AVA顺序无关。拆分层级必须避开已转义的逗号和加号。用结构化DN判等、查父项及祖先，不能简单endswith。根DN是允许没有本地父条目的唯一条目，不能删除或改名。
每个记录作为一个原子变更。modify按顺序执行add/delete/replace属性块：属性值按集合处理，重复add值、删除不存在属性或指定值都失败；delete无值删除整属性，replace无值删除该属性，replace新集合必须无重复。任一子块失败则整条记录回滚，记原因后继续下条。entryUUID必须恰有一值、全局唯一且不可改；每条记录结束时目标条目必须包含当前RDN需要的属性值，不能通过modify偷偷使名称与属性矛盾。
modrdn支持同级更名和newsuperior跨父项移动，目标及全部后代的DN一起改变，UUID不变。deleteoldrdn=1从目标属性移除旧RDN中不再出现的AVA值，再加入新RDN需要的值；=0保留旧值并加入新值。新父不存在、移动到自己子树、目标或任何后代新DN冲突均整条失败；普通delete仅允许叶节点。不要改写其他条目member里的DN字符串：每次快照用当前目录重新解析其指向UUID，旧引用可以暂时悬空，显式member变更后再恢复。
为每条输入记录输出成功/失败、受影响UUID和快照摘要，提供按记录序号导出中间快照以检查移动后尚未修复的member。现成数据覆盖失败多子块回滚、带逗号与加号的多AVA名称、子树移动、非叶删除、引用修复、中文base64和非法自包含移动。另补同名碰撞及deleteoldrdn=0小例。核对UUID总量、父子结构、RDN属性、引用解析及失败前后内容不变，交付源码、快照和命令。不连接LDAP服务器，不实现认证、完整schema继承、通用LDAP匹配规则或二进制属性。

输入文件：
- `fixtures/base.ldif`
- `fixtures/changes.ldif`
- `fixtures/profile.json`

环境：Python 3.11；全部业务数据为本地合成材料。
