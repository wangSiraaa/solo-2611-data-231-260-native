"""Core delivery builder: object dedupe, link resolution, VDS materialisation."""

from __future__ import annotations

import datetime as _dt
import json
import os
import tempfile
from collections import deque
from dataclasses import dataclass
from typing import Any

import h5py
import numpy as np
from h5py import h5d, h5o, h5s

from .errors import DeliveryError, where
from .identity import (
    Resolved,
    SourceFile,
    SourceStore,
    join_obj_path,
    norm_obj_path,
    object_kind,
    resolve_link,
    resolve_path,
    resolved_from_object,
)

SUPPORTED_DATASET_KINDS = frozenset("iufbS")


# ---------------------------------------------------------------------------
# selection helpers


def _regular_indices(start: int, stride: int, count: int, block: int) -> np.ndarray:
    chunks = []
    for i in range(count):
        base = start + i * stride
        chunks.append(np.arange(base, base + block, dtype=np.int64))
    if not chunks:
        return np.array([], dtype=np.int64)
    return np.concatenate(chunks)


def _space_selection(
    space,
    *,
    label: str,
    file_path: str,
    object_path: str,
    limit: int | None,
) -> tuple[np.ndarray | None, dict[str, Any], bool]:
    """Return (indices, manifest record, whole_dataset) for a 1-D selection.

    ``indices`` is ``None`` when the stored space is the scalar/H5S_ALL form
    HDF5 writes for an entire source dataset.
    """

    sel_type = space.get_select_type()
    ext_type = space.get_simple_extent_type()

    def fail(msg: str) -> DeliveryError:
        return DeliveryError(
            f"{msg}{where(file_path, object_path)} (VDS {label} selection)"
        )

    if sel_type == h5s.SEL_NONE:
        # HDF5 maps no elements for this mapping.  This is only meaningful for
        # the virtual space; a source mapping with no elements cannot be
        # validated against a concrete region and is rejected by the caller.
        return np.array([], dtype=np.int64), {"type": "none", "element_count": 0}, False

    if ext_type == h5s.SCALAR:
        if sel_type == h5s.SEL_ALL:
            return None, {"type": "all", "whole_dataset": True}, True
        raise fail(f"unsupported scalar VDS selection (select type {sel_type})")

    if ext_type != h5s.SIMPLE:
        raise fail(f"unsupported VDS dataspace extent type {ext_type}")

    dims = tuple(int(d) for d in space.get_simple_extent_dims())
    if len(dims) != 1:
        raise fail(
            f"only one-dimensional VDS mappings are supported, got dims {dims}"
        )

    if sel_type == h5s.SEL_ALL:
        indices = np.arange(dims[0], dtype=np.int64)
        record = {"type": "all", "extent": [dims[0]], "element_count": len(indices)}
    elif sel_type == h5s.SEL_HYPERSLABS:
        if not space.is_regular_hyperslab():
            raise fail("non-regular hyperslab VDS selection is not supported")
        start, stride, count, block = space.get_regular_hyperslab()
        if len(start) != 1:
            raise fail("only one-dimensional VDS hyperslabs are supported")
        indices = _regular_indices(
            int(start[0]), int(stride[0]), int(count[0]), int(block[0])
        )
        record = {
            "type": "hyperslab",
            "start": [int(start[0])],
            "stride": [int(stride[0])],
            "count": [int(count[0])],
            "block": [int(block[0])],
            "element_count": int(len(indices)),
        }
    elif sel_type == h5s.SEL_POINTS:
        raw = np.asarray(space.get_select_elem_pointlist()).reshape(-1)
        indices = raw.astype(np.int64, copy=False)
        record = {
            "type": "points",
            "points": indices.tolist(),
            "element_count": int(len(indices)),
        }
    else:
        raise fail(f"unsupported VDS selection type {sel_type}")

    if limit is not None:
        if len(indices) and (indices.min() < 0 or indices.max() >= limit):
            raise fail(
                f"selection {record} lies outside a dataspace of length {limit}"
            )
    return indices, record, False


# ---------------------------------------------------------------------------
# builder


@dataclass
class _ObjRecord:
    identity: tuple[int, int, int]
    resolved: Resolved
    output_path: str
    kind: str
    origin: dict[str, Any]
    aliases: list[dict[str, Any]]
    dataset: dict[str, Any] | None = None
    vds: dict[str, Any] | None = None

    def to_manifest(self) -> dict[str, Any]:
        res = self.resolved
        doc: dict[str, Any] = {
            "identity": {
                "file_realpath": res.file.realpath,
                "file_device": res.file.device,
                "file_inode": res.file.inode,
                "file_id": f"{res.file.device}:{res.file.inode}",
                "object_address": res.identity[2],
                "combined": f"{res.file.device}:{res.file.inode}:{res.identity[2]}",
            },
            "source": {
                "file": res.file.realpath,
                "object_path": res.obj_path,
                "kind": self.kind,
            },
            "output_representative": self.output_path,
            "origin": self.origin,
            "aliases": self.aliases,
        }
        if self.dataset is not None:
            doc["dataset"] = self.dataset
        if self.vds is not None:
            doc["vds"] = self.vds
        return doc


class _Builder:
    def __init__(self, store: SourceStore) -> None:
        self.store = store
        self.out: h5py.File | None = None
        self.representative: dict[tuple[int, int, int], str] = {}
        self.records: dict[tuple[int, int, int], _ObjRecord] = {}
        self.order: list[tuple[int, int, int]] = []
        self.links: list[dict[str, Any]] = []
        self.queue: deque[tuple[Resolved, str]] = deque()

    # -- public ---------------------------------------------------------

    def build(self, root: Resolved, out_file: h5py.File) -> dict[str, Any]:
        self.out = out_file
        root_kind = object_kind(
            root.obj, file_path=root.file.realpath, object_path=root.obj_path
        )
        if root_kind != "group":
            raise DeliveryError(
                f"root object is not a group{where(root.file.realpath, root.obj_path)}"
            )
        identity = root.identity
        self.representative[identity] = "/"
        self._register(
            root,
            "/",
            _ObjRecord(
                identity=identity,
                resolved=root,
                output_path="/",
                kind=root_kind,
                origin={"type": "root"},
                aliases=[],
            ),
        )
        self._copy_attributes(root.obj, out_file, root, "/")
        self.queue.append((root, "/"))

        while self.queue:
            group_res, group_out_path = self.queue.popleft()
            self._walk_group(group_res, group_out_path)

        self._assert_self_contained()
        return self._manifest()

    # -- records --------------------------------------------------------

    def _register(self, res: Resolved, out_path: str, record: _ObjRecord) -> None:
        self.records[res.identity] = record
        self.order.append(res.identity)

    def _make_record(
        self,
        res: Resolved,
        out_path: str,
        origin: dict[str, Any],
    ) -> _ObjRecord:
        return _ObjRecord(
            identity=res.identity,
            resolved=res,
            output_path=out_path,
            kind=res.kind,
            origin=origin,
            aliases=[],
        )

    @staticmethod
    def _origin_for(link, file: SourceFile, link_path: str) -> dict[str, Any]:
        if isinstance(link, h5py.HardLink):
            return {"type": "hard", "source_link_path": link_path}
        if isinstance(link, h5py.SoftLink):
            return {
                "type": "soft",
                "source_link_path": link_path,
                "target": str(link.path),
            }
        if isinstance(link, h5py.ExternalLink):
            return {
                "type": "external",
                "source_link_path": link_path,
                "external_file": str(link.filename),
                "external_object": str(link.path),
            }
        raise DeliveryError(
            f"unsupported link type {type(link).__name__}{where(file.realpath, link_path)}"
        )

    # -- traversal ------------------------------------------------------

    def _walk_group(self, group_res: Resolved, group_out_path: str) -> None:
        source_group = group_res.obj
        if not isinstance(source_group, h5py.Group):
            raise DeliveryError(
                f"expected a group{where(group_res.file.realpath, group_res.obj_path)}"
            )
        for name in sorted(source_group.keys()):
            link_path = join_obj_path(group_res.obj_path, name)
            try:
                link = source_group.get(name, getlink=True)
            except Exception as exc:
                raise DeliveryError(
                    f"cannot inspect link{where(group_res.file.realpath, link_path)}: {exc}"
                ) from exc
            origin = self._origin_for(link, group_res.file, link_path)
            resolved = resolve_link(
                self.store, group_res.file, source_group, group_res.obj_path, name
            )
            out_child = join_obj_path(group_out_path, name)
            existing = self.representative.get(resolved.identity)
            if existing is None:
                self._create_representative(resolved, out_child, origin)
                self.representative[resolved.identity] = out_child
            else:
                self._create_hard_link(group_out_path, name, existing, resolved, origin)
                record = self.records[resolved.identity]
                record.aliases.append(
                    {
                        "output_path": out_child,
                        "origin": origin,
                        "pointing_to": existing,
                    }
                )

    def _parent_and_name(self, out_path: str) -> tuple[str, str]:
        """Split an absolute output path; supports groups linked under a parent."""

        parent_path = out_path.rsplit("/", 1)[0] or "/"
        name = out_path.rsplit("/", 1)[-1]
        return parent_path, name

    def _create_representative(
        self,
        res: Resolved,
        out_path: str,
        origin: dict[str, Any],
    ) -> None:
        assert self.out is not None
        parent_path, name = self._parent_and_name(out_path)
        parent = self.out[parent_path]
        record = self._make_record(res, out_path, origin)
        if res.kind == "group":
            parent.create_group(name)
            self._copy_attributes(res.obj, parent[name], res, out_path)
            self._register(res, out_path, record)
            self.links.append(
                {
                    "output_path": out_path,
                    "target": out_path,
                    "kind": "hard",
                    "origin": origin,
                    "object_identity": record.to_manifest()["identity"],
                }
            )
            self.queue.append((res, out_path))
        elif res.kind == "dataset":
            data, dataset_meta, vds_meta = self._read_dataset(res)
            try:
                dset = parent.create_dataset(
                    name,
                    shape=data.shape,
                    dtype=data.dtype,
                    data=data,
                    fillvalue=res.obj.fillvalue,
                )
            except Exception as exc:
                raise DeliveryError(
                    f"cannot write dataset{where(res.file.realpath, res.obj_path)}: {exc}"
                ) from exc
            self._copy_attributes(res.obj, dset, res, out_path)
            record.dataset = dataset_meta
            record.vds = vds_meta
            self._register(res, out_path, record)
            self.links.append(
                {
                    "output_path": out_path,
                    "target": out_path,
                    "kind": "hard",
                    "origin": origin,
                    "object_identity": record.to_manifest()["identity"],
                }
            )
        else:  # pragma: no cover - object_kind already rejects other kinds
            raise DeliveryError(
                f"unsupported object kind {res.kind!r}"
                f"{where(res.file.realpath, res.obj_path)}"
            )

    def _create_hard_link(
        self,
        parent_out_path: str,
        name: str,
        target_out_path: str,
        resolved: Resolved,
        origin: dict[str, Any],
    ) -> None:
        assert self.out is not None
        parent = self.out[parent_out_path]
        try:
            parent.id.links.create_hard(
                name.encode("utf-8"), self.out.id, target_out_path.encode("utf-8")
            )
        except Exception as exc:
            raise DeliveryError(
                f"cannot create hard link {join_obj_path(parent_out_path, name)} -> "
                f"{target_out_path}: {exc}"
            ) from exc
        out_path = join_obj_path(parent_out_path, name)
        self.links.append(
            {
                "output_path": out_path,
                "target": target_out_path,
                "kind": "hard",
                "origin": origin,
                "object_identity": self.records[resolved.identity].to_manifest()[
                    "identity"
                ],
            }
        )

    # -- attributes -----------------------------------------------------

    def _copy_attributes(
        self,
        source_obj,
        dest_obj,
        res: Resolved,
        out_path: str,
    ) -> None:
        for name in source_obj.attrs:
            try:
                aid = source_obj.attrs.get_id(name)
                dtype = aid.dtype
                shape = aid.shape
            except Exception as exc:
                raise DeliveryError(
                    f"cannot inspect attribute {name!r}"
                    f"{where(res.file.realpath, res.obj_path)}: {exc}"
                ) from exc
            ref = h5py.check_dtype(ref=dtype)
            vlen = h5py.check_dtype(vlen=dtype)
            if ref is not None:
                raise DeliveryError(
                    f"unsupported reference-typed attribute {name!r} "
                    f"(dtype {dtype}){where(res.file.realpath, res.obj_path)}"
                )
            if vlen is not None and vlen not in (str, bytes):
                raise DeliveryError(
                    f"unsupported variable-length attribute {name!r} "
                    f"(dtype {dtype}){where(res.file.realpath, res.obj_path)}"
                )
            if dtype.kind == "O" and h5py.check_string_dtype(dtype) is None:
                raise DeliveryError(
                    f"unsupported object-typed attribute {name!r} "
                    f"(dtype {dtype}){where(res.file.realpath, res.obj_path)}"
                )
            try:
                value = source_obj.attrs[name]
                dest_obj.attrs.create(name, value, dtype=dtype, shape=shape)
            except DeliveryError:
                raise
            except Exception as exc:
                raise DeliveryError(
                    f"cannot copy attribute {name!r}{where(res.file.realpath, res.obj_path)}: {exc}"
                ) from exc

    # -- datasets -------------------------------------------------------

    def _check_storage(self, dset: h5py.Dataset, res: Resolved) -> None:
        """Reject storage shapes that would leak external state into delivery."""

        plist = dset.id.get_create_plist()
        external_count = getattr(plist, "get_external_count", lambda: 0)()
        if external_count:
            raise DeliveryError(
                f"dataset uses external raw storage ({external_count} file(s)), "
                f"which would leave an external dependency"
                f"{where(res.file.realpath, res.obj_path)}"
            )

    def _check_dtype(self, dtype: np.dtype, res: Resolved) -> None:
        ref = h5py.check_dtype(ref=dtype)
        vlen = h5py.check_dtype(vlen=dtype)
        if ref is not None:
            raise DeliveryError(
                f"unsupported reference-typed dataset (dtype {dtype})"
                f"{where(res.file.realpath, res.obj_path)}"
            )
        if vlen is not None:
            raise DeliveryError(
                f"unsupported variable-length dataset (dtype {dtype})"
                f"{where(res.file.realpath, res.obj_path)}"
            )
        if dtype.fields is not None:
            raise DeliveryError(
                f"unsupported compound dataset (dtype {dtype})"
                f"{where(res.file.realpath, res.obj_path)}"
            )
        if h5py.check_enum_dtype(dtype) is not None:
            raise DeliveryError(
                f"unsupported enum dataset (dtype {dtype})"
                f"{where(res.file.realpath, res.obj_path)}"
            )
        if h5py.check_opaque_dtype(dtype):
            raise DeliveryError(
                f"unsupported opaque dataset (dtype {dtype})"
                f"{where(res.file.realpath, res.obj_path)}"
            )
        if dtype.kind not in SUPPORTED_DATASET_KINDS:
            raise DeliveryError(
                f"unsupported dataset dtype {dtype}"
                f"{where(res.file.realpath, res.obj_path)}"
            )

    def _read_dataset(
        self, res: Resolved
    ) -> tuple[np.ndarray, dict[str, Any], dict[str, Any] | None]:
        dset = res.obj
        if not isinstance(dset, h5py.Dataset):
            raise DeliveryError(
                f"expected a dataset{where(res.file.realpath, res.obj_path)}"
            )
        self._check_dtype(dset.dtype, res)
        self._check_storage(dset, res)
        meta = {
            "shape": list(dset.shape),
            "dtype": str(dset.dtype),
            "fill_value": _jsonable_scalar(dset.fillvalue),
        }
        if dset.is_virtual:
            data, vds_meta = self._materialise_vds(res)
            return data, meta, vds_meta

        plist = dset.id.get_create_plist()
        layout = plist.get_layout()
        if layout == h5d.VIRTUAL:
            raise DeliveryError(
                f"dataset reports a virtual layout but h5py did not mark it virtual"
                f"{where(res.file.realpath, res.obj_path)}"
            )
        try:
            data = np.array(dset[...], copy=True)
        except Exception as exc:
            raise DeliveryError(
                f"cannot read dataset{where(res.file.realpath, res.obj_path)}: {exc}"
            ) from exc
        if tuple(data.shape) != tuple(dset.shape):
            raise DeliveryError(
                f"dataset read returned shape {data.shape}, expected {dset.shape}"
                f"{where(res.file.realpath, res.obj_path)}"
            )
        return data, meta, None

    def _materialise_vds(
        self, res: Resolved
    ) -> tuple[np.ndarray, dict[str, Any]]:
        assert self.out is not None
        dset = res.obj
        plist = dset.id.get_create_plist()
        if plist.get_layout() != h5d.VIRTUAL:
            raise DeliveryError(
                f"virtual dataset has a non-virtual layout"
                f"{where(res.file.realpath, res.obj_path)}"
            )
        if dset.ndim != 1:
            raise DeliveryError(
                f"only one-dimensional VDS datasets are supported, got shape {dset.shape}"
                f"{where(res.file.realpath, res.obj_path)}"
            )
        fill = dset.fillvalue
        data = np.full(dset.shape, fill, dtype=dset.dtype)
        mapped = np.zeros(dset.shape, dtype=bool)
        mappings: list[dict[str, Any]] = []

        for index in range(plist.get_virtual_count()):
            virtual_space = plist.get_virtual_vspace(index)
            source_space = plist.get_virtual_srcspace(index)
            file_name = plist.get_virtual_filename(index)
            dset_name = plist.get_virtual_dsetname(index)
            mapping_context = f"VDS mapping {index}"
            if isinstance(file_name, bytes):
                file_name = file_name.decode("utf-8")
            if isinstance(dset_name, bytes):
                dset_name = dset_name.decode("utf-8")

            try:
                source_file = self.store.external_file(
                    res.file,
                    file_name,
                    referenced_by=f"{res.file.realpath}:{res.obj_path}",
                )
            except DeliveryError as exc:
                raise DeliveryError(
                    f"{mapping_context} of {res.file.realpath}:{res.obj_path} "
                    f"references missing/unusable source file {file_name!r}: {exc}"
                ) from exc

            source_path = norm_obj_path(dset_name)
            try:
                source_res = resolve_path(self.store, source_file, source_path, stack=[])
            except DeliveryError as exc:
                raise DeliveryError(
                    f"{mapping_context} of {res.file.realpath}:{res.obj_path} "
                    f"references missing/unusable source dataset {file_name!r}:"
                    f"{source_path!r}: {exc}"
                ) from exc
            if not isinstance(source_res.obj, h5py.Dataset):
                raise DeliveryError(
                    f"{mapping_context} of {res.file.realpath}:{res.obj_path} "
                    f"resolves to a {source_res.kind}, not a dataset: "
                    f"{source_file.realpath}:{source_path}"
                )

            src_dset = source_res.obj
            if src_dset.is_virtual:
                raise DeliveryError(
                    f"{mapping_context} of {res.file.realpath}:{res.obj_path} "
                    f"points at a nested virtual dataset "
                    f"{source_file.realpath}:{source_res.obj_path}; nested VDS sources "
                    f"are not supported"
                )
            if src_dset.dtype != dset.dtype:
                raise DeliveryError(
                    f"{mapping_context} of {res.file.realpath}:{res.obj_path} "
                    f"has source dtype {src_dset.dtype} but VDS dtype {dset.dtype} at "
                    f"{source_file.realpath}:{source_res.obj_path}"
                )
            self._check_dtype(src_dset.dtype, source_res)

            virtual_idx, virtual_rec, virtual_whole = _space_selection(
                virtual_space,
                label=f"{mapping_context} virtual",
                file_path=res.file.realpath,
                object_path=res.obj_path,
                limit=dset.shape[0],
            )
            if virtual_whole:
                virtual_idx = np.arange(dset.shape[0], dtype=np.int64)
            assert virtual_idx is not None
            source_idx, source_rec, source_whole = _space_selection(
                source_space,
                label=f"{mapping_context} source",
                file_path=source_file.realpath,
                object_path=source_res.obj_path,
                limit=None,
            )
            if not source_whole and src_dset.ndim != 1:
                raise DeliveryError(
                    f"{mapping_context} of {res.file.realpath}:{res.obj_path} "
                    f"selects a hyperslab from a multi-dimensional source "
                    f"{source_file.realpath}:{source_res.obj_path} (shape {src_dset.shape})"
                )
            if (
                not source_whole
                and source_idx is not None
                and len(source_idx)
                and (source_idx.min() < 0 or source_idx.max() >= src_dset.shape[0])
            ):
                raise DeliveryError(
                    f"{mapping_context} of {res.file.realpath}:{res.obj_path} "
                    f"selects out-of-bounds source indices "
                    f"[{int(source_idx.min())}, {int(source_idx.max())}] from "
                    f"{source_file.realpath}:{source_res.obj_path} "
                    f"(shape {src_dset.shape})"
                )

            if source_whole:
                if src_dset.size != len(virtual_idx):
                    raise DeliveryError(
                        f"{mapping_context} of {res.file.realpath}:{res.obj_path} "
                        f"maps {len(virtual_idx)} virtual elements to the whole source "
                        f"{source_file.realpath}:{source_res.obj_path} which has "
                        f"{src_dset.size} elements"
                    )
                try:
                    source_values = np.asarray(src_dset[...]).reshape(-1)
                except Exception as exc:
                    raise DeliveryError(
                        f"cannot read VDS source {source_file.realpath}:"
                        f"{source_res.obj_path}: {exc}"
                    ) from exc
            else:
                if len(source_idx) != len(virtual_idx):
                    raise DeliveryError(
                        f"{mapping_context} of {res.file.realpath}:{res.obj_path} "
                        f"maps {len(virtual_idx)} virtual elements to "
                        f"{len(source_idx)} source elements at "
                        f"{source_file.realpath}:{source_res.obj_path}"
                    )
                try:
                    source_values = np.asarray(src_dset[source_idx])
                except Exception as exc:
                    raise DeliveryError(
                        f"cannot read VDS source selection from "
                        f"{source_file.realpath}:{source_res.obj_path}: {exc}"
                    ) from exc
            source_values = source_values.astype(dset.dtype, copy=False)

            if len(virtual_idx):
                data[virtual_idx] = source_values
                mapped[virtual_idx] = True

            mappings.append(
                {
                    "index": index,
                    "source": {
                        "file": source_file.realpath,
                        "file_id": f"{source_file.device}:{source_file.inode}",
                        "file_device": source_file.device,
                        "file_inode": source_file.inode,
                        "object_path": source_res.obj_path,
                        "object_address": source_res.identity[2],
                        "shape": list(src_dset.shape),
                        "dtype": str(src_dset.dtype),
                    },
                    "virtual_selection": virtual_rec,
                    "source_selection": source_rec,
                    "element_count": int(len(virtual_idx)),
                }
            )

        try:
            actual = np.asarray(dset[...])
        except Exception as exc:
            raise DeliveryError(
                f"cannot read virtual dataset{where(res.file.realpath, res.obj_path)}: {exc}"
            ) from exc
        if tuple(actual.shape) != tuple(dset.shape):
            raise DeliveryError(
                f"virtual dataset read returned shape {actual.shape}, expected {dset.shape}"
                f"{where(res.file.realpath, res.obj_path)}"
            )
        if not np.array_equal(actual, data):
            raise DeliveryError(
                f"materialised VDS data does not match HDF5's mapping semantics"
                f"{where(res.file.realpath, res.obj_path)}; this indicates an "
                f"unsupported selection or an out-of-bounds source"
            )

        vds_meta = {
            "virtual_shape": list(dset.shape),
            "dtype": str(dset.dtype),
            "fill_value": _jsonable_scalar(fill),
            "mapped_elements": int(mapped.sum()),
            "unmapped_elements": int((~mapped).sum()),
            "mappings": mappings,
        }
        return data, vds_meta

    # -- final checks and manifest --------------------------------------

    def _assert_self_contained(self) -> None:
        assert self.out is not None
        problems: list[str] = []

        def visit(name: str, obj) -> None:
            path = "/" + name if name else "/"
            if isinstance(obj, h5py.Dataset):
                if obj.is_virtual:
                    problems.append(f"virtual dataset remains at {path}")
                layout = obj.id.get_create_plist().get_layout()
                if layout == h5d.VIRTUAL:
                    problems.append(f"virtual layout remains at {path}")
                if hasattr(obj.id.get_create_plist(), "get_external_count"):
                    if obj.id.get_create_plist().get_external_count() > 0:
                        problems.append(f"external raw storage remains at {path}")
                if h5py.check_dtype(ref=obj.dtype) is not None:
                    problems.append(f"reference dtype remains at {path}")

        self.out.visititems(visit)

        # Avoid walking hard-link cycles forever: track group addresses.
        seen: set[int] = set()

        def walk(group, prefix: str) -> None:
            addr = h5o.get_info(group.id).addr
            if addr in seen:
                return
            seen.add(addr)
            for name in group:
                path = join_obj_path(prefix, name)
                link = group.get(name, getlink=True)
                if not isinstance(link, h5py.HardLink):
                    problems.append(
                        f"non-hard link remains at {path}: {type(link).__name__}"
                    )
                    continue
                child = group[name]
                if isinstance(child, h5py.Group):
                    walk(child, path)

        walk(self.out, "/")

        if problems:
            raise DeliveryError(
                "delivery is not self-contained: " + "; ".join(problems)
            )

    def _manifest(self) -> dict[str, Any]:
        objects = [self.records[ident].to_manifest() for ident in self.order]
        return {
            "objects": objects,
            "links": self.links,
            "counts": {
                "objects": len(objects),
                "links": len(self.links),
                "datasets": sum(1 for r in objects if r["source"]["kind"] == "dataset"),
                "groups": sum(1 for r in objects if r["source"]["kind"] == "group"),
            },
        }


def _jsonable_scalar(value: Any) -> Any:
    if isinstance(value, np.generic):
        return value.item()
    if isinstance(value, np.ndarray):
        return value.tolist()
    if isinstance(value, bytes):
        return value.decode("utf-8", "replace")
    return value


# ---------------------------------------------------------------------------
# public API


def build_delivery(
    input_path: str,
    output_path: str,
    manifest_path: str | None = None,
    *,
    overwrite: bool = False,
) -> dict[str, Any]:
    """Build ``output_path`` (and manifest) from ``input_path``.

    Nothing is written to the final paths unless the whole build succeeds.
    Returns the manifest dictionary that was written.
    """

    input_real = os.path.realpath(os.fspath(input_path))
    output_path = os.path.abspath(os.fspath(output_path))
    if manifest_path is None:
        manifest_path = os.path.join(
            os.path.dirname(output_path),
            "manifest.json",
        )
    manifest_path = os.path.abspath(os.fspath(manifest_path))

    if os.path.exists(output_path) and not overwrite:
        raise DeliveryError(
            f"output file already exists: {output_path} (pass overwrite=True to replace)"
        )
    if os.path.exists(manifest_path) and not overwrite:
        raise DeliveryError(
            f"manifest file already exists: {manifest_path} (pass overwrite=True to replace)"
        )
    for target in (output_path, manifest_path):
        if os.path.abspath(target) == input_real:
            raise DeliveryError(
                f"refusing to overwrite the input file with build output: {target}"
            )

    out_dir = os.path.dirname(output_path) or "."
    man_dir = os.path.dirname(manifest_path) or "."
    os.makedirs(out_dir, exist_ok=True)
    os.makedirs(man_dir, exist_ok=True)

    # Everything is written to temporary files and only renamed into place
    # after a fully successful build, so a failure can never leave a partial
    # file at the requested paths.  Existing files are replaced atomically on
    # success and left untouched on failure.
    fd, tmp_h5 = tempfile.mkstemp(prefix=".delivery-", suffix=".h5.tmp", dir=out_dir)
    os.close(fd)
    os.unlink(tmp_h5)
    fd, tmp_manifest = tempfile.mkstemp(
        prefix=".manifest-", suffix=".json.tmp", dir=man_dir
    )
    os.close(fd)
    os.unlink(tmp_manifest)

    store = SourceStore()
    try:
        root_file = store.get_file(input_real)
        root_res = resolved_from_object(root_file, root_file.handle["/"], "/")
        builder = _Builder(store)
        with h5py.File(tmp_h5, "w") as out:
            body = builder.build(root_res, out)
        out_size = os.path.getsize(tmp_h5)
        manifest = {
            "format": "hdf5-delivery-manifest",
            "version": 1,
            "created_utc": _dt.datetime.now(_dt.timezone.utc).isoformat(),
            "input": {
                "requested_path": os.fspath(input_path),
                "realpath": root_file.realpath,
                "file_id": f"{root_file.device}:{root_file.inode}",
                "file_device": root_file.device,
                "file_inode": root_file.inode,
            },
            "output": {
                "path": output_path,
                "manifest_path": manifest_path,
                "size_bytes": out_size,
            },
            "source_files": [
                {
                    "realpath": sf.realpath,
                    "file_id": f"{sf.device}:{sf.inode}",
                    "file_device": sf.device,
                    "file_inode": sf.inode,
                    "requested_paths": list(sf.requested_paths),
                }
                for sf in store.files()
            ],
            "objects": body["objects"],
            "links": body["links"],
            "counts": body["counts"],
        }
        with open(tmp_manifest, "w", encoding="utf-8") as fh:
            json.dump(manifest, fh, indent=2, sort_keys=False, ensure_ascii=False)
            fh.write("\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_h5, output_path)
        try:
            os.replace(tmp_manifest, manifest_path)
        except Exception:
            try:
                os.unlink(output_path)
            except OSError:
                pass
            raise
        return manifest
    except BaseException:
        for path in (tmp_h5, tmp_manifest):
            try:
                os.unlink(path)
            except OSError:
                pass
        raise
    finally:
        store.close()
