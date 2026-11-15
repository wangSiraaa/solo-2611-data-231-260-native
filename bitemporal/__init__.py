"""双时态维度表：按已知知识时刻物化可回查的有效时间历史。

子模块按需导入，避免 ``python -m bitemporal.importer`` 时的重复加载告警。
"""

__all__ = [
    "Revision",
    "Segment",
    "apply_revision",
    "values_equal",
    "HistoryStore",
    "query",
    "slice_at",
    "RevisionError",
    "import_revisions",
    "export_history_jsonl",
]


def __getattr__(name: str):
    if name in ("Revision", "Segment", "apply_revision", "values_equal"):
        from . import model

        return getattr(model, name)
    if name in ("HistoryStore", "query", "slice_at"):
        from . import store

        return getattr(store, name)
    if name in ("RevisionError", "import_revisions", "export_history_jsonl"):
        from . import importer

        return getattr(importer, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
