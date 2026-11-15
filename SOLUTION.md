# 交付说明：LDIF 离线导出（load / apply / verify）

## 交付物

| 路径 | 内容 |
|---|---|
| `ldifdir.py` | 处理器源码（Python 3.11，仅标准库），子命令 `load` / `apply` / `verify` |
| `fixtures/` | 任务给定输入：`base.ldif`、`changes.ldif`、`profile.json` |
| `extra/changes_extra.ldif` | 另补的小例：同名碰撞（modrdn 与 add 两式）、`deleteoldrdn=0`，以及 RDN 矛盾守卫、entryUUID 不可改、重复 add 值 |
| `out_load/` | `load` 产物：base 的三张 JSONL 表 |
| `out/` | `apply` 产物：最终三张表、`results.jsonl`、`snapshots/` 中间快照 |

## 运行命令

```sh
# 1) 物化初始条目
python3 ldifdir.py load --base fixtures/base.ldif --profile fixtures/profile.json --out out_load

# 2) 逐条原子应用变更（两个变更文件按顺序连续编号），并按记录序号导出中间快照
python3 ldifdir.py apply --base fixtures/base.ldif --profile fixtures/profile.json \
    --changes fixtures/changes.ldif --changes extra/changes_extra.ldif \
    --out out --snapshot-after 3,4,5

# 3) 核对输出目录
python3 ldifdir.py verify --out out
python3 ldifdir.py verify --out out_load
```

## 输出表（JSONL）

- `entries.jsonl`：`{uuid, dn, rdn, parent_dn}`，DN 为结构化解析后的规范串。
- `attributes.jsonl`：`{uuid, attribute, value}`，多值属性每个值一行，不压成标量；属性类型规范为小写，值保持精确 Unicode 串。
- `member_edges.jsonl`：`{src_uuid, attribute, member_dn, target_uuid, status}`。member 里存的 DN 字符串从不改写，每次快照用当前目录重新解析；指向不存在的 DN 记 `dangling`、`target_uuid: null`。
- `results.jsonl`：每条记录一行 `{seq, changetype, dn, ok, reason, affected_uuids, summary, hash_before, hash_after}`；失败记录 `hash_before == hash_after`（内容不变）。

## 数据与规则覆盖

给定 8 条 + 另补 6 条，共 14 条记录（6 成功 / 8 失败）：

| # | 记录 | 结果 | 覆盖点 |
|---|---|---|---|
| 1 | modify jane（add 后 delete 不存在值） | FAIL | 多子块失败整条回滚（快照 003 中无 `temporary`） |
| 2 | modify jane（replace base64 中文） | OK | `::` base64 UTF-8（`已确认`） |
| 3 | modrdn jane → ou=Archive | OK | 子树移动（badge 一起迁、UUID 不变）、`deleteoldrdn=1` 删 `cn=Doe, Jane` 加 `cn=Jane+D`、member 开始悬空 |
| 4 | delete jane（有子节点） | FAIL | 非叶删除 |
| 5 | modify Team 重写 member | OK | 引用修复；`uid=jane+cn=Jane\+D` 逆序 AVA 与 RDN 判等（多 AVA 顺序无关） |
| 6 | delete badge | OK | 叶删除 |
| 7 | add（`dn::` 与 `cn::` base64） | OK | base64 DN 行 + 中文 `李雷` |
| 8 | modrdn ou=People 到自己后代下 | FAIL | 非法自包含移动（结构化后缀判定） |
| 9 | modrdn 李雷 → `cn=Team`（已存在） | FAIL | 同名碰撞（跨父移动） |
| 10 | add 已存在的 DN | FAIL | 同名碰撞（add） |
| 11 | modrdn 李雷 → `cn=LiLei`，`deleteoldrdn=0` | OK | 旧值保留：`cn={李雷, LiLei}` |
| 12 | modify 删除当前 RDN 需要的值 | FAIL | 名称/属性矛盾守卫 |
| 13 | modify 改 entryUUID | FAIL | entryUUID 不可改 |
| 14 | modify add 已存在值（折行展开后的原文） | FAIL | 重复 add 值；同时验证折行拼接 |

base.ldif 本身覆盖：多 AVA RDN（`cn=Doe\, Jane+uid=jane`，转义逗号）、新 RDN 含转义加号（`cn=Jane\+D`）、LDIF 折行、base64 中文属性、`version: 1` 记录。

## 核对结果（verify 全部通过）

- **UUID 总量**：base 6 + 成功 add 1（记录 7）− 成功 delete 1（记录 6）= 最终 6。
- **父子结构**：结构化 DN 上重建，唯一根 `dc=example,dc=test`，其余条目父项均在，`parent_dn` 列与解析一致。
- **RDN 属性**：每个条目都持有其叶子 RDN 的全部 AVA（含 `cn=Jane+D`、`cn=LiLei`）。
- **引用解析**：member 边逐条从当前 entries 重新解析比对；快照 003/004 中 Team→jane 为 `dangling`，快照 005 起 `resolved → jane-001`。
- **失败前后内容不变**：8 条失败记录 `hash_before == hash_after`，且 `affected_uuids` 为空。

## 实现要点

- DN 解析为「RDN 元组（叶子在前）」，RDN 为排序后的 `(类型小写, 精确Unicode值)` AVA 元组：层级切分只认未转义逗号/加号，判等、查父、查祖先、子树判定全在结构上进行，不用字符串 `endswith`。
- 每条记录在目录的深拷贝上执行，全部子块成功才提交，天然原子；modify 块按序执行，值按集合处理（重复 add、删不存在属性/值、replace 新集合有重复均失败；delete/replace 无值删整属性）。
- 记录结束时校验目标条目持有当前 RDN 所需属性值；entryUUID 恰一值、全局唯一、modify 不可触碰；根 DN（profile 指定）不可删除或改名，是唯一允许无本地父条目的项。
- modrdn 同时支持同级更名与 `newsuperior` 跨父移动，整棵子树一起换 DN、UUID 不变；新父不存在、移入自己子树、目标或任后代新 DN 冲突均整条失败。
- 未实现（按要求）：LDAP 连接、认证、schema 继承、通用匹配规则、二进制属性。
