"""Byte-faithful CSV cell patcher.

Public API::

    from csv_fidelity import apply_patch
    result = apply_patch(source_bytes, patch_dict)

The original bytes are never rewritten wholesale.  Only the byte lexeme of a
patched field is replaced; every other byte (BOM, quoting of untouched fields,
mixed record terminators, final-newline presence) is copied verbatim.
"""

from .errors import PatchError, PatchErrorCode
from .patcher import apply_patch
from .scanner import Cell, Document, scan_document
from .verify import verify

__all__ = [
    "apply_patch",
    "verify",
    "scan_document",
    "Document",
    "Cell",
    "PatchError",
    "PatchErrorCode",
]
__version__ = "1.0.0"
