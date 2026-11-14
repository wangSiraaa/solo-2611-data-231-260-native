"""规范 read 身份解释。

支持两种声明（由 config 决定启用哪些）：

* suffix：第一个空白分隔 token 去掉结尾 ``/1``、``/2`` 后缀，后缀声明 mate；
* casava：第二个空白 token 以 ``1:`` 或 ``2:`` 开头时，首字段声明 mate。

文件角色（来源清单里的 mate）提供缺省 mate。明确声明冲突、角色不符、
空身份一律拒绝（隔离），身份绝不依据序列内容合并。
"""

from __future__ import annotations

from .model import SCHEME_CASAVA, SCHEME_SUFFIX, Config


class IdentityError(ValueError):
    """身份非法：空身份、声明冲突或与文件角色不符。"""


def _parse_suffix(header: bytes) -> tuple[bytes, int] | None:
    """解析 /1、/2 后缀。返回 (基础名, mate)，无后缀返回 None。"""
    # header 形如 b"@name/1 extra..."，去掉 @ 后按空白取第一个 token。
    tokens = header[1:].split()
    if not tokens:
        return None
    token = tokens[0]
    if len(token) >= 2 and token[-2:-1] == b"/" and token[-1:] in (b"1", b"2"):
        base = token[:-2]
        return base, int(token[-1:])
    return None


def _parse_casava(header: bytes) -> tuple[bytes, int] | None:
    """解析 CASAVA 第二字段 mate 标识，返回 (基础名, mate)，无声明返回 None。"""
    tokens = header[1:].split()
    if len(tokens) < 2:
        return None
    second = tokens[1]
    # 第二字段以 "1:" 或 "2:" 开头（CASAVA 形如 1:N:0:INDEX）。
    if len(second) >= 2 and second[1:2] == b":" and second[0:1] in (b"1", b"2"):
        return tokens[0], int(second[0:1])
    return None


def resolve_identity(header: bytes, role_mate: int, config: Config) -> tuple[bytes, int]:
    """把原始头解释为 (规范 read_id, mate)。

    角色 mate 仅作为缺省值；头中任何明确声明都必须与之一致，否则拒绝。
    两个启用方案同时给出 mate 但不一致，同样拒绝。
    """
    if role_mate not in (1, 2):
        raise IdentityError(f"文件角色 mate 非法: {role_mate!r}")

    schemes = config.identity_schemes
    suffix_out = _parse_suffix(header) if SCHEME_SUFFIX in schemes else None
    casava_out = _parse_casava(header) if SCHEME_CASAVA in schemes else None

    # 收集明确声明的 mate，以及候选规范名。
    declared: list[int] = []
    name_candidates: list[bytes] = []
    if suffix_out is not None:
        name_candidates.append(suffix_out[0])
        declared.append(suffix_out[1])
    if casava_out is not None:
        name_candidates.append(casava_out[0])
        declared.append(casava_out[1])

    # 无任何明确声明：使用缺省 mate，名称取首个空白 token；两个方案都启用但
    # 均无声明时名称规则一致（都是第一 token）。
    if not declared:
        tokens = header[1:].split()
        if not tokens or not tokens[0]:
            raise IdentityError("空身份：头部不含任何 read 名称")
        return tokens[0], role_mate

    # 明确声明之间冲突（suffix 与 casava 给出不同 mate）。
    if len(set(declared)) != 1:
        raise IdentityError(
            f"mate 声明冲突: suffix/casava 分别声明 {declared}"
        )
    mate = declared[0]

    # 规范名：两个方案都给出名称时必须一致；只启用单方案时用该方案的名称。
    non_empty = [name for name in name_candidates if name]
    if not non_empty:
        raise IdentityError("空身份：解析得到的 read 名称为空")
    if len(set(non_empty)) != 1:
        raise IdentityError("身份方案解析出的 read 名称不一致")
    read_id = non_empty[0]

    # 明确声明与文件角色不符。
    if mate != role_mate:
        raise IdentityError(
            f"角色不符：头中声明 mate {mate}，但文件角色为 mate {role_mate}"
        )
    return read_id, mate
