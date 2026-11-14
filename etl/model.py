"""共享数据模型：类型、AST 节点、列向量、错误码。

只放数据结构，不放求值逻辑；向量化引擎与标量解释器各自实现求值。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

import numpy as np

INT64_MIN = -(2**63)
INT64_MAX = 2**63 - 1

TYPE_INT = "int64"
TYPE_TEXT = "text"
TYPE_BOOL = "bool"
BASE_TYPES = (TYPE_INT, TYPE_TEXT, TYPE_BOOL)

# 行错误码：错误不是 NULL，逐行独立记录 code + AST 路径。
ERR_DIV_ZERO = "div_zero"        # div 除数为 0
ERR_OVERFLOW = "overflow"        # int64 溢出（add 溢出、INT64_MIN / -1）
ERR_BAD_TO_INT = "bad_to_int"    # to_int 词法非法或超出 int64 范围

# to_int 只接受该词法：-?(0|[1-9][0-9]*)，无前导零、无符号+、无空白。
TO_INT_RE = re.compile(r"-?(0|[1-9][0-9]*)")

OPS = (
    "col", "literal",
    "add", "div", "to_int", "eq", "gt", "is_null",
    "if", "coalesce", "and", "or", "not",
)
# 实际运算节点（进入时记 trace）；col 读取与 literal 填充不是运算。
OP_NODES = (
    "add", "div", "to_int", "eq", "gt", "is_null",
    "if", "coalesce", "and", "or", "not",
)


class CompileError(Exception):
    """预检（结构/类型）错误：在任何一行数据求值之前拒绝整个作业。"""


@dataclass
class Node:
    """编译后的 AST 节点。path 形如 $、$/2、$/2/0（根为 $，按子位置展开）。"""
    op: str
    path: str
    children: list["Node"] = field(default_factory=list)
    col_name: str | None = None          # op == "col"
    literal: object = None               # op == "literal" 的 Python 值
    lit_kind: str | None = None          # "int" | "text" | "bool" | "null"
    type: str | None = None              # 预检定型结果：int64 / text / bool
    nullable: bool = False


@dataclass
class Vector:
    """一个节点在一个 batch 上的求值结果（稠密存储，长度 = batch 行数）。

    只有进入该节点 selection 且未出错、非 NULL 的行，values 才有定义；
    错误行不进 Vector，记录在 BatchCtx.errors。
    """
    kind: str                 # int64 / text / bool
    values: np.ndarray        # int64 / bool_ / object
    null: np.ndarray          # bool 掩码

    @classmethod
    def empty(cls, kind: str, n: int) -> "Vector":
        if kind == TYPE_INT:
            values = np.zeros(n, dtype=np.int64)
        elif kind == TYPE_BOOL:
            values = np.zeros(n, dtype=np.bool_)
        else:
            values = np.empty(n, dtype=object)
        return cls(kind, values, np.zeros(n, dtype=np.bool_))
