"""独立标量解释器：仅用于小数据验收，与向量化引擎不共享任何求值逻辑。

逐行、逐节点递归求值，结果三态：("ok", 值) / ("null",) / ("error", code, path)。
记录与引擎相同粒度的节点进入事件（节点路径, 原始行号），用于核对
“每行每表达式节点是否被求值”在两个实现间一致。
"""
from __future__ import annotations

from .compiler import Plan
from .engine import Trace
from .model import (
    ERR_BAD_TO_INT, ERR_DIV_ZERO, ERR_OVERFLOW, INT64_MAX, INT64_MIN,
    TO_INT_RE, Node,
)

_OK = "ok"
_NULL = "null"
_ERROR = "error"


def _ok(v):
    return (_OK, v)


def _err(code, path):
    return (_ERROR, code, path)


NULL = (_NULL,)


def _eval(node: Node, row: dict, expr: str, row_idx: int, trace: Trace):
    op = node.op
    if op == "col":
        v = row[node.col_name]
        return NULL if v is None else _ok(v)
    if op == "literal":
        return NULL if node.lit_kind == "null" else _ok(node.literal)
    trace.record(expr, node.path, 0, (row_idx,))
    return _METHODS[op](node, row, expr, row_idx, trace)


def _is_null(r) -> bool:
    return r[0] == _NULL


def _is_err(r) -> bool:
    return r[0] == _ERROR


# ---------------------------------------------------------------- 标量算子

def _s_add(node, row, expr, idx, trace):
    l = _eval(node.children[0], row, expr, idx, trace)
    if _is_err(l):
        return l
    r = _eval(node.children[1], row, expr, idx, trace)
    if _is_err(r):
        return r
    if _is_null(l) or _is_null(r):
        return NULL
    v = l[1] + r[1]
    if v < INT64_MIN or v > INT64_MAX:
        return _err(ERR_OVERFLOW, node.path)
    return _ok(v)


def _s_div(node, row, expr, idx, trace):
    l = _eval(node.children[0], row, expr, idx, trace)
    if _is_err(l):
        return l
    r = _eval(node.children[1], row, expr, idx, trace)
    if _is_err(r):
        return r
    if _is_null(l) or _is_null(r):
        return NULL
    a, b = l[1], r[1]
    if b == 0:
        return _err(ERR_DIV_ZERO, node.path)
    if a == INT64_MIN and b == -1:
        return _err(ERR_OVERFLOW, node.path)
    q = abs(a) // abs(b)               # 向零截断
    if (a < 0) != (b < 0):
        q = -q
    return _ok(q)


def _s_to_int(node, row, expr, idx, trace):
    s = _eval(node.children[0], row, expr, idx, trace)
    if _is_err(s):
        return s
    if _is_null(s):
        return NULL
    text = s[1]
    if TO_INT_RE.fullmatch(text) is None:
        return _err(ERR_BAD_TO_INT, node.path)
    v = int(text)
    if v < INT64_MIN or v > INT64_MAX:
        return _err(ERR_BAD_TO_INT, node.path)
    return _ok(v)


def _s_eq(node, row, expr, idx, trace):
    l = _eval(node.children[0], row, expr, idx, trace)
    if _is_err(l):
        return l
    r = _eval(node.children[1], row, expr, idx, trace)
    if _is_err(r):
        return r
    if _is_null(l) or _is_null(r):
        return NULL
    return _ok(l[1] == r[1])


def _s_gt(node, row, expr, idx, trace):
    l = _eval(node.children[0], row, expr, idx, trace)
    if _is_err(l):
        return l
    r = _eval(node.children[1], row, expr, idx, trace)
    if _is_err(r):
        return r
    if _is_null(l) or _is_null(r):
        return NULL
    return _ok(l[1] > r[1])            # 文本序 = UTF-8 字节序


def _s_is_null(node, row, expr, idx, trace):
    r = _eval(node.children[0], row, expr, idx, trace)
    if _is_err(r):
        return r
    return _ok(_is_null(r))


def _s_not(node, row, expr, idx, trace):
    r = _eval(node.children[0], row, expr, idx, trace)
    if _is_err(r):
        return r
    if _is_null(r):
        return NULL
    return _ok(not r[1])


def _s_if(node, row, expr, idx, trace):
    c = _eval(node.children[0], row, expr, idx, trace)
    if _is_err(c):
        return c
    if not _is_null(c) and c[1] is True:
        return _eval(node.children[1], row, expr, idx, trace)
    return _eval(node.children[2], row, expr, idx, trace)   # FALSE 或 NULL → else


def _s_coalesce(node, row, expr, idx, trace):
    for child in node.children:
        r = _eval(child, row, expr, idx, trace)
        if _is_err(r):
            return r                    # 已求值错误不被后续默认值吞掉
        if not _is_null(r):
            return r                    # 取到非 NULL：不再求后续项
    return NULL


def _s_and(node, row, expr, idx, trace):
    l = _eval(node.children[0], row, expr, idx, trace)
    if _is_err(l):
        return l
    if not _is_null(l) and l[1] is False:
        return _ok(False)               # 左 FALSE：短路
    r = _eval(node.children[1], row, expr, idx, trace)
    if _is_err(r):
        return r
    if not _is_null(l):                 # 左 TRUE → 结果即右侧
        return r
    # 左 UNKNOWN
    if _is_null(r):
        return NULL
    return _ok(False) if r[1] is False else NULL


def _s_or(node, row, expr, idx, trace):
    l = _eval(node.children[0], row, expr, idx, trace)
    if _is_err(l):
        return l
    if not _is_null(l) and l[1] is True:
        return _ok(True)                # 左 TRUE：短路
    r = _eval(node.children[1], row, expr, idx, trace)
    if _is_err(r):
        return r
    if not _is_null(l):                 # 左 FALSE → 结果即右侧
        return r
    # 左 UNKNOWN
    if _is_null(r):
        return NULL
    return _ok(True) if r[1] is True else NULL


_METHODS = {
    "add": _s_add,
    "div": _s_div,
    "to_int": _s_to_int,
    "eq": _s_eq,
    "gt": _s_gt,
    "is_null": _s_is_null,
    "not": _s_not,
    "if": _s_if,
    "coalesce": _s_coalesce,
    "and": _s_and,
    "or": _s_or,
}


def run_scalar(plans: dict[str, Plan], columns: dict[str, list], n_rows: int,
               trace: Trace | None = None):
    """逐行求值全部表达式，返回与 Engine.run 同构的输出 + trace。"""
    trace = trace if trace is not None else Trace()
    outputs: dict[str, list] = {name: [None] * n_rows for name in plans}
    for name, plan in plans.items():
        for i in range(n_rows):
            row = {col: columns[col][i] for col in columns}
            r = _eval(plan.root, row, name, i, trace)
            if r[0] == _ERROR:
                outputs[name][i] = {"value": None, "is_null": False,
                                    "error": {"code": r[1], "node_path": r[2]}}
            elif r[0] == _NULL:
                outputs[name][i] = {"value": None, "is_null": True, "error": None}
            else:
                outputs[name][i] = {"value": r[1], "is_null": False, "error": None}
    return outputs, trace
