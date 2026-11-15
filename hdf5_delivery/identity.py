"""Source-file handling, physical object identity and link resolution.

Object identity in this package is always ``(device, inode, object address)``.
The device/inode pair identifies the physical HDF5 file even when several
path names (or a relative external-link path) point at it; the address is only
meaningful inside that file.

Link resolution deliberately mirrors HDF5's own path rules rather than
``os.path``/``posixpath`` normalisation:

* a soft link whose target starts with ``/`` is absolute;
* any other soft link target is interpreted relative to the group that
  contains the link (simple concatenation);
* a component named ``.`` is skipped, a component named ``..`` is *not*
  special-cased by HDF5 and is looked up as an ordinary link name;
* an external link's object name is interpreted from the external file's
  root, and its file name is relative to the directory of the file that
  contains the link.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field

import h5py
from h5py import h5o

from .errors import DeliveryError, where

MAX_LINK_HOPS = 200


def _text(value) -> str:
    if isinstance(value, bytes):
        return value.decode("utf-8")
    return str(value)


def norm_obj_path(path: str) -> str:
    """Normalise only the leading slash; keep ``.``/``..`` semantics intact."""

    path = _text(path)
    parts = [p for p in path.split("/") if p != ""]
    if not parts:
        return "/"
    return "/" + "/".join(parts)


def join_obj_path(base: str, name: str) -> str:
    base = norm_obj_path(base)
    if base == "/":
        return "/" + name
    return base + "/" + name


@dataclass
class SourceFile:
    """One physical HDF5 file, opened read-only exactly once."""

    realpath: str
    device: int
    inode: int
    handle: h5py.File
    requested_paths: list[str] = field(default_factory=list)

    @property
    def key(self) -> tuple[int, int]:
        return (self.device, self.inode)


class SourceStore:
    """Registry of physical source files."""

    def __init__(self) -> None:
        self._by_key: dict[tuple[int, int], SourceFile] = {}

    def get_file(self, path: str, *, required_from: str | None = None) -> SourceFile:
        real = os.path.realpath(_text(path))
        if not os.path.exists(real):
            msg = f"source file does not exist: {path!r}"
            if required_from:
                msg += f" (referenced by {required_from})"
            raise DeliveryError(msg)
        if os.path.isdir(real):
            raise DeliveryError(f"source path is a directory, not an HDF5 file: {real}")
        try:
            stat = os.stat(real)
            key = (stat.st_dev, stat.st_ino)
        except OSError as exc:  # pragma: no cover - unusual filesystem failure
            raise DeliveryError(f"cannot stat source file {real}: {exc}") from exc
        existing = self._by_key.get(key)
        if existing is not None:
            if real not in existing.requested_paths:
                existing.requested_paths.append(real)
            return existing
        try:
            handle = h5py.File(real, "r")
        except Exception as exc:
            msg = f"cannot open source HDF5 file {real}: {exc}"
            if required_from:
                msg += f" (referenced by {required_from})"
            raise DeliveryError(msg) from exc
        sf = SourceFile(realpath=real, device=key[0], inode=key[1], handle=handle)
        sf.requested_paths.append(real)
        self._by_key[key] = sf
        return sf

    def external_file(
        self,
        current: SourceFile,
        filename: str,
        *,
        referenced_by: str,
    ) -> SourceFile:
        name = _text(filename)
        if name in ("", ".", "./"):
            return current
        if not os.path.isabs(name):
            name = os.path.join(os.path.dirname(current.realpath), name)
        return self.get_file(name, required_from=referenced_by)

    def close(self) -> None:
        for sf in self._by_key.values():
            try:
                sf.handle.close()
            except Exception:
                pass
        self._by_key.clear()

    def files(self) -> list[SourceFile]:
        """Physical files in discovery order (root first)."""

        return list(self._by_key.values())


@dataclass
class Resolved:
    """A resolved source object plus the context needed to record it."""

    file: SourceFile
    obj: h5py.Group | h5py.Dataset
    obj_path: str
    kind: str
    identity: tuple[int, int, int]

    @property
    def file_id(self) -> str:
        return f"{self.file.device}:{self.file.inode}"

    def describe_source(self) -> str:
        return f"{self.file.realpath}:{self.obj_path}"


def object_kind(obj, *, file_path: str | None = None, object_path: str | None = None) -> str:
    info = h5o.get_info(obj.id)
    if info.type == h5o.TYPE_GROUP:
        return "group"
    if info.type == h5o.TYPE_DATASET:
        return "dataset"
    kind_name = {h5o.TYPE_NAMED_DATATYPE: "named datatype"}.get(info.type, "unknown")
    raise DeliveryError(
        f"unsupported source object type {kind_name} (H5O type={info.type})"
        f"{where(file_path, object_path)}"
    )


def h5i_name(obj) -> str:
    try:
        return _text(h5py.h5i.get_name(obj.id))
    except Exception:
        return "<unknown>"


def resolved_from_object(file: SourceFile, obj, obj_path: str) -> Resolved:
    info = h5o.get_info(obj.id)
    if info.addr is None:
        raise DeliveryError(
            f"cannot determine object address for {file.realpath}:{obj_path}"
        )
    kind = object_kind(obj, file_path=file.realpath, object_path=obj_path)
    return Resolved(
        file=file,
        obj=obj,
        obj_path=norm_obj_path(obj_path),
        kind=kind,
        identity=(file.device, file.inode, int(info.addr)),
    )


def _failure(message: str, file: SourceFile, path: str, detail: str = "") -> DeliveryError:
    text = f"{message}{where(file.realpath, path)}"
    if detail:
        text += f": {detail}"
    return DeliveryError(text)


def _open_child(group_obj, part: str, file: SourceFile, path: str):
    try:
        link = group_obj.get(part, getlink=True)
    except Exception as exc:
        raise _failure("cannot inspect link", file, path, str(exc)) from exc
    if link is None:
        raise _failure("link does not exist", file, path)
    if not isinstance(link, h5py.HardLink):
        return None, link
    try:
        return group_obj[part], link
    except Exception as exc:
        raise _failure("cannot open hard-linked object", file, path, str(exc)) from exc


def resolve_path(
    store: SourceStore,
    file: SourceFile,
    path: str,
    *,
    stack: list[tuple[tuple[int, int], str]] | None = None,
    depth: int = 0,
) -> Resolved:
    """Resolve ``path`` in ``file`` following soft and external links.

    ``stack`` is the chain of paths currently being resolved.  Re-entering a
    path means the link graph contains a cycle consisting only of links; this
    is rejected instead of letting HDF5 loop until its own link limit.
    """

    stack = list(stack or [])
    path = norm_obj_path(path)
    key = (file.key, path)
    if key in stack:
        chain = " -> ".join(p for _, p in stack + [key])
        raise _failure(
            "cyclic soft/external link chain",
            file,
            path,
            "resolution stack: " + chain,
        )
    if depth > MAX_LINK_HOPS:
        raise _failure("too many link hops", file, path, f"limit={MAX_LINK_HOPS}")

    stack.append(key)

    if path == "/":
        return resolved_from_object(file, file.handle["/"], "/")

    parts = [p for p in path.split("/") if p]
    current_obj = file.handle["/"]
    current_path = "/"

    for index, part in enumerate(parts):
        if part == ".":
            continue

        child_path = join_obj_path(current_path, part)
        child, link = _open_child(current_obj, part, file, child_path)

        if child is not None:
            current_obj = child
            current_path = child_path
            continue

        remainder = parts[index + 1 :]

        if isinstance(link, h5py.SoftLink):
            target = _text(link.path)
            if target.startswith("/"):
                target_path = norm_obj_path(target)
            else:
                target_path = norm_obj_path(join_obj_path(current_path, target))
            if remainder:
                target_path = norm_obj_path(
                    join_obj_path(target_path, "/".join(remainder))
                )
            return resolve_path(store, file, target_path, stack=stack, depth=depth + 1)

        if isinstance(link, h5py.ExternalLink):
            target_file = store.external_file(
                file,
                link.filename,
                referenced_by=f"{file.realpath}:{child_path}",
            )
            target_path = norm_obj_path(_text(link.path))
            if remainder:
                target_path = norm_obj_path(
                    join_obj_path(target_path, "/".join(remainder))
                )
            try:
                return resolve_path(
                    store, target_file, target_path, stack=stack, depth=depth + 1
                )
            except DeliveryError as exc:
                raise DeliveryError(
                    f"while resolving external link {file.realpath}:{child_path} "
                    f"-> {_text(link.filename)}:{_text(link.path)}: {exc}"
                ) from exc

        raise _failure("unsupported link type", file, child_path, type(link).__name__)

    return resolved_from_object(file, current_obj, current_path)


def resolve_link(
    store: SourceStore,
    file: SourceFile,
    group_obj,
    group_path: str,
    name: str,
) -> Resolved:
    """Resolve a single link found in ``group_obj`` (without exploiting it)."""

    link_path = join_obj_path(group_path, name)
    try:
        link = group_obj.get(name, getlink=True)
    except Exception as exc:
        raise _failure("cannot inspect link", file, link_path, str(exc)) from exc
    if link is None:
        raise _failure("link does not exist", file, link_path)

    if isinstance(link, h5py.HardLink):
        try:
            child = group_obj[name]
        except Exception as exc:
            raise _failure(
                "cannot open hard-linked object", file, link_path, str(exc)
            ) from exc
        return resolved_from_object(file, child, link_path)

    if isinstance(link, h5py.SoftLink):
        target = _text(link.path)
        if target.startswith("/"):
            target_path = norm_obj_path(target)
        else:
            target_path = norm_obj_path(join_obj_path(group_path, target))
        try:
            return resolve_path(store, file, target_path, stack=[])
        except DeliveryError as exc:
            raise DeliveryError(
                f"dangling or invalid soft link at {file.realpath}:{link_path} "
                f"-> {target!r}: {exc}"
            ) from exc

    if isinstance(link, h5py.ExternalLink):
        target_file = store.external_file(
            file,
            link.filename,
            referenced_by=f"{file.realpath}:{link_path}",
        )
        target_path = norm_obj_path(_text(link.path))
        try:
            return resolve_path(store, target_file, target_path, stack=[])
        except DeliveryError as exc:
            raise DeliveryError(
                f"dangling or invalid external link at {file.realpath}:{link_path} "
                f"-> {_text(link.filename)}:{_text(link.path)}: {exc}"
            ) from exc

    raise _failure("unsupported link type", file, link_path, type(link).__name__)
