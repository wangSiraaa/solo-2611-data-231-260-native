#!/usr/bin/env python3
"""Independent end-to-end verification for the HDF5 delivery.

This script does not import the ``hdf5_delivery`` package for its checks:

1. It builds (or accepts) a delivery plus manifest.
2. It captures an expected model directly from the source files: physical
   object identity, link edges, aliases, attributes, dataset values and
   validated VDS mappings.
3. It copies the delivery and manifest into an isolated directory, then
   physically moves the input directory away.
4. It reopens the isolated copy and compares every object, dataset (including
   empty arrays), attribute, alias identity and root link name.
5. It asserts the delivery contains no soft links, external links, virtual
   datasets, external raw storage or reference typed data.
6. It runs the builder against the supplied dangling-external, soft-cycle and
   missing-VDS-fixtures and asserts each fails with a source-path diagnostic
   and leaves no output file.  Two generated negative cases (out-of-bounds
   VDS source, object-reference dtype) are checked as well.

Run from the repository root::

    python verify_delivery.py
    python verify_delivery.py --input fixtures/root.h5
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import traceback
from dataclasses import dataclass, field
from pathlib import Path

import h5py
import numpy as np
from h5py import h5d, h5o, h5s

MAX_LINK_HOPS = 200


class VerificationError(AssertionError):
    pass


def check(condition: bool, message: str) -> None:
    if not condition:
        raise VerificationError(message)


def _text(value) -> str:
    return value.decode("utf-8") if isinstance(value, bytes) else str(value)


def norm_lead(path: str) -> str:
    parts = [p for p in _text(path).split("/") if p != ""]
    return "/" if not parts else "/" + "/".join(parts)


def join_path(base: str, name: str) -> str:
    return "/" + name if norm_lead(base) == "/" else norm_lead(base) + "/" + name


# ---------------------------------------------------------------------------
# source model


@dataclass
class FileCtx:
    realpath: str
    device: int
    inode: int
    handle: h5py.File

    @property
    def key(self):
        return (self.device, self.inode)


class SourceFiles:
    def __init__(self) -> None:
        self.by_key: dict[tuple[int, int], FileCtx] = {}

    def open(self, path: str, referenced_by: str | None = None) -> FileCtx:
        real = os.path.realpath(path)
        check(os.path.exists(real), f"expected source file missing: {real}")
        st = os.stat(real)
        key = (st.st_dev, st.st_ino)
        if key in self.by_key:
            return self.by_key[key]
        try:
            handle = h5py.File(real, "r")
        except Exception as exc:
            raise VerificationError(
                f"cannot open expected source {real}"
                + (f" (from {referenced_by})" if referenced_by else "")
                + f": {exc}"
            ) from exc
        ctx = FileCtx(realpath=real, device=key[0], inode=key[1], handle=handle)
        self.by_key[key] = ctx
        return ctx

    def external(self, current: FileCtx, filename: str, referenced_by: str) -> FileCtx:
        name = _text(filename)
        if name in ("", ".", "./"):
            return current
        if not os.path.isabs(name):
            name = os.path.join(os.path.dirname(current.realpath), name)
        return self.open(name, referenced_by)

    def close(self) -> None:
        for ctx in self.by_key.values():
            ctx.handle.close()


@dataclass
class Target:
    file: FileCtx
    obj: object
    obj_path: str
    kind: str
    identity: str  # file_id:address


def _identity(ctx: FileCtx, obj) -> str:
    info = h5o.get_info(obj.id)
    check(info.addr is not None, f"missing object address in {ctx.realpath}")
    return f"{ctx.device}:{ctx.inode}:{int(info.addr)}"


def _kind(obj) -> str:
    t = h5o.get_info(obj.id).type
    if t == h5o.TYPE_GROUP:
        return "group"
    if t == h5o.TYPE_DATASET:
        return "dataset"
    raise VerificationError(f"unexpected object type {t} at {h5py.h5i.get_name(obj.id)}")


def _child(group, name: str):
    try:
        link = group.get(name, getlink=True)
    except Exception as exc:
        raise VerificationError(f"cannot inspect link {name!r}: {exc}") from exc
    if isinstance(link, h5py.HardLink):
        try:
            return group[name], link
        except Exception as exc:
            raise VerificationError(f"cannot open hard link {name!r}: {exc}") from exc
    return None, link


def resolve_object(
    files: SourceFiles,
    ctx: FileCtx,
    path: str,
    stack=(),
) -> Target:
    path = norm_lead(path)
    key = (ctx.key, path)
    if key in stack:
        raise VerificationError(
            "cycle while resolving expected source: "
            + " -> ".join(p for _, p in (*stack, key))
        )
    if len(stack) > MAX_LINK_HOPS:
        raise VerificationError(f"too many link hops resolving {ctx.realpath}:{path}")
    stack = (*stack, key)
    if path == "/":
        obj = ctx.handle["/"]
        return Target(ctx, obj, "/", _kind(obj), _identity(ctx, obj))

    parts = [p for p in path.split("/") if p]
    current = ctx.handle["/"]
    current_path = "/"
    for index, part in enumerate(parts):
        if part == ".":
            continue
        child_path = join_path(current_path, part)
        child, link = _child(current, part)
        if child is not None:
            current = child
            current_path = child_path
            continue
        remainder = parts[index + 1 :]
        if isinstance(link, h5py.SoftLink):
            target = _text(link.path)
            target_path = (
                norm_lead(target)
                if target.startswith("/")
                else norm_lead(join_path(current_path, target))
            )
            if remainder:
                target_path = norm_lead(join_path(target_path, "/".join(remainder)))
            return resolve_object(files, ctx, target_path, stack)
        if isinstance(link, h5py.ExternalLink):
            target_ctx = files.external(ctx, link.filename, f"{ctx.realpath}:{child_path}")
            target_path = norm_lead(link.path)
            if remainder:
                target_path = norm_lead(join_path(target_path, "/".join(remainder)))
            return resolve_object(files, target_ctx, target_path, stack)
        raise VerificationError(
            f"unsupported link type {type(link).__name__} at {ctx.realpath}:{child_path}"
        )
    return Target(ctx, current, current_path, _kind(current), _identity(ctx, current))


def resolve_child(
    files: SourceFiles, ctx: FileCtx, group, group_path: str, name: str
) -> tuple[Target, object]:
    link_path = join_path(group_path, name)
    try:
        link = group.get(name, getlink=True)
    except Exception as exc:
        raise VerificationError(f"cannot inspect link {link_path}: {exc}") from exc
    if isinstance(link, h5py.HardLink):
        target = resolve_object(files, ctx, link_path)
    elif isinstance(link, h5py.SoftLink):
        target_path = (
            norm_lead(link.path)
            if _text(link.path).startswith("/")
            else norm_lead(join_path(group_path, link.path))
        )
        target = resolve_object(files, ctx, target_path)
    elif isinstance(link, h5py.ExternalLink):
        target_ctx = files.external(ctx, link.filename, f"{ctx.realpath}:{link_path}")
        target = resolve_object(files, target_ctx, norm_lead(link.path))
    else:
        raise VerificationError(f"unsupported link type at {ctx.realpath}:{link_path}")
    return target, link


def link_descriptor(link) -> dict:
    if isinstance(link, h5py.HardLink):
        return {"type": "hard"}
    if isinstance(link, h5py.SoftLink):
        return {"type": "soft", "target": _text(link.path)}
    if isinstance(link, h5py.ExternalLink):
        return {
            "type": "external",
            "file": _text(link.filename),
            "object": _text(link.path),
        }
    raise VerificationError(f"unsupported link {type(link).__name__}")


def attr_snapshot(obj, label: str) -> dict[str, tuple]:
    snap: dict[str, tuple] = {}
    for name in obj.attrs:
        try:
            aid = obj.attrs.get_id(name)
            dtype = aid.dtype
            shape = tuple(aid.shape)
            value = obj.attrs[name]
        except Exception as exc:
            raise VerificationError(f"cannot read attribute {name!r} on {label}: {exc}")
        ref = h5py.check_dtype(ref=dtype)
        vlen = h5py.check_dtype(vlen=dtype)
        if ref is not None:
            raise VerificationError(f"reference-typed attribute {name!r} on {label}")
        if vlen is not None and vlen not in (str, bytes):
            raise VerificationError(f"unsupported vlen attribute {name!r} on {label}")
        if isinstance(value, np.ndarray):
            value = value.copy()
        snap[name] = (dtype, shape, value)
    return snap


def values_equal(left, right) -> bool:
    if isinstance(left, np.ndarray) or isinstance(right, np.ndarray):
        try:
            return bool(np.array_equal(np.asarray(left), np.asarray(right)))
        except Exception:
            return False
    if isinstance(left, (bytes, np.bytes_)) or isinstance(right, (bytes, np.bytes_)):
        return bytes(left) == bytes(right)
    try:
        return bool(np.asarray(left == right))
    except Exception:
        return False


def assert_attrs_equal(expected: dict, obj, label: str) -> None:
    actual = attr_snapshot(obj, label)
    check(
        set(expected) == set(actual),
        f"attribute names differ on {label}: expected {sorted(expected)}, "
        f"got {sorted(actual)}",
    )
    for name, (dtype, shape, value) in expected.items():
        a_dtype, a_shape, a_value = actual[name]
        check(
            np.dtype(dtype) == np.dtype(a_dtype),
            f"attribute {name!r} dtype differs on {label}: "
            f"{dtype!r} vs {a_dtype!r}",
        )
        check(shape == a_shape, f"attribute {name!r} shape differs on {label}")
        check(
            values_equal(value, a_value),
            f"attribute {name!r} value differs on {label}: "
            f"{value!r} vs {a_value!r}",
        )


@dataclass
class ExpectedObject:
    identity: str
    file_realpath: str
    file_id: str
    object_path: str
    kind: str
    attrs: dict
    access_paths: list[str] = field(default_factory=list)
    output_representative: str = ""
    aliases: list[dict] = field(default_factory=list)
    dataset: dict | None = None
    vds: dict | None = None


@dataclass
class ExpectedModel:
    root_attrs: dict
    root_names: list[str]
    objects: dict[str, ExpectedObject]
    edges: list[dict]
    source_file_ids: set[str]
    vds_source_file_ids: set[str]


def _space_indices(space, label: str) -> tuple[np.ndarray | None, dict]:
    sel = space.get_select_type()
    ext = space.get_simple_extent_type()
    if sel == h5s.SEL_NONE:
        return np.array([], dtype=np.int64), {"type": "none", "element_count": 0}
    if ext == h5s.SCALAR and sel == h5s.SEL_ALL:
        return None, {"type": "all", "whole_dataset": True}
    if ext != h5s.SIMPLE:
        raise VerificationError(f"{label}: unsupported dataspace extent type {ext}")
    dims = tuple(int(d) for d in space.get_simple_extent_dims())
    if len(dims) != 1:
        raise VerificationError(f"{label}: expected 1-D, got {dims}")
    if sel == h5s.SEL_ALL:
        idx = np.arange(dims[0], dtype=np.int64)
        return idx, {"type": "all", "extent": [dims[0]]}
    if sel != h5s.SEL_HYPERSLABS:
        raise VerificationError(f"{label}: unsupported selection type {sel}")
    if not space.is_regular_hyperslab():
        raise VerificationError(f"{label}: non-regular hyperslab")
    start, stride, count, block = space.get_regular_hyperslab()
    if len(start) != 1:
        raise VerificationError(f"{label}: expected 1-D hyperslab")
    chunks = [
        np.arange(start[0] + i * stride[0], start[0] + i * stride[0] + block[0])
        for i in range(count[0])
    ]
    idx = (
        np.concatenate(chunks).astype(np.int64)
        if chunks
        else np.array([], dtype=np.int64)
    )
    return idx, {
        "type": "hyperslab",
        "start": [int(start[0])],
        "stride": [int(stride[0])],
        "count": [int(count[0])],
        "block": [int(block[0])],
    }


def validate_vds_expected(files: SourceFiles, target: Target) -> tuple[np.ndarray, dict]:
    dset = target.obj
    plist = dset.id.get_create_plist()
    check(plist.get_layout() == h5d.VIRTUAL, f"dataset not virtual: {target.obj_path}")
    check(dset.ndim == 1, f"expected 1-D VDS, got {dset.shape} at {target.obj_path}")
    mappings = []
    mapped = np.zeros(dset.shape, dtype=bool)
    for index in range(plist.get_virtual_count()):
        file_name = _text(plist.get_virtual_filename(index))
        dset_name = _text(plist.get_virtual_dsetname(index))
        vspace = plist.get_virtual_vspace(index)
        sspace = plist.get_virtual_srcspace(index)
        vidx, vrec = _space_indices(vspace, f"VDS {target.obj_path} mapping {index} virtual")
        if vidx is None:
            vidx = np.arange(dset.shape[0], dtype=np.int64)
        if len(vidx) and (vidx.min() < 0 or vidx.max() >= dset.shape[0]):
            raise VerificationError(
                f"VDS {target.obj_path} mapping {index} virtual selection out of bounds"
            )
        src_ctx = files.external(target.file, file_name, f"{target.file.realpath}:{target.obj_path}")
        src_path = norm_lead(dset_name)
        try:
            src = resolve_object(files, src_ctx, src_path)
        except VerificationError as exc:
            raise VerificationError(
                f"VDS {target.obj_path} mapping {index}: missing source "
                f"{src_ctx.realpath}:{src_path}: {exc}"
            ) from exc
        check(
            src.kind == "dataset",
            f"VDS {target.obj_path} source is {src.kind}: {src_ctx.realpath}:{src_path}",
        )
        src_dset = src.obj
        check(
            src_dset.dtype == dset.dtype,
            f"VDS dtype mismatch at {target.obj_path}: {src_dset.dtype} vs {dset.dtype}",
        )
        sidx, srec = _space_indices(sspace, f"VDS {target.obj_path} mapping {index} source")
        if sidx is None:
            check(
                src_dset.size == len(vidx),
                f"VDS {target.obj_path} maps {len(vidx)} elements to whole source "
                f"{src.obj_path} of size {src_dset.size}",
            )
            source_values = np.asarray(src_dset[...]).reshape(-1)
        else:
            check(
                src_dset.ndim == 1,
                f"VDS source {src.obj_path} is not 1-D: {src_dset.shape}",
            )
            if len(sidx) and (sidx.min() < 0 or sidx.max() >= src_dset.shape[0]):
                raise VerificationError(
                    f"VDS {target.obj_path} mapping {index} source selection "
                    f"[{int(sidx.min())},{int(sidx.max())}] out of bounds for "
                    f"{src.obj_path} shape {src_dset.shape}"
                )
            check(
                len(sidx) == len(vidx),
                f"VDS {target.obj_path} mapping {index}: {len(vidx)} virtual vs "
                f"{len(sidx)} source elements",
            )
            source_values = np.asarray(src_dset[sidx])
        source_values = source_values.astype(dset.dtype, copy=False)
        if len(vidx):
            mapped[vidx] = True
        mappings.append(
            {
                "index": index,
                "source": {
                    "file": src_ctx.realpath,
                    "file_id": f"{src_ctx.device}:{src_ctx.inode}",
                    "object_path": src.obj_path,
                    "object_address": int(src.identity.rsplit(":", 1)[1]),
                    "shape": list(src_dset.shape),
                    "dtype": str(src_dset.dtype),
                },
                "virtual_selection": vrec,
                "source_selection": srec,
                "element_count": int(len(vidx)),
                "_coverage": vidx,
                "_values": source_values,
            }
        )

    expected = np.asarray(dset[...])
    for mapping in mappings:
        vidx = mapping.pop("_coverage")
        values = mapping.pop("_values")
        if len(vidx):
            check(
                np.array_equal(expected[vidx], values),
                f"VDS {target.obj_path} mapping {mapping['index']} values do not "
                f"match source data",
            )
    meta = {
        "virtual_shape": list(dset.shape),
        "dtype": str(dset.dtype),
        "fill_value": _json_scalar(dset.fillvalue),
        "mapped_elements": int(mapped.sum()),
        "unmapped_elements": int((~mapped).sum()),
        "mappings": mappings,
    }
    return expected, meta


def _json_scalar(value):
    if isinstance(value, np.generic):
        return value.item()
    if isinstance(value, np.ndarray):
        return value.tolist()
    return value


def capture_expected(files: SourceFiles, root_path: str) -> ExpectedModel:
    root_ctx = files.open(root_path)
    root_obj = root_ctx.handle["/"]
    root_identity = _identity(root_ctx, root_obj)
    check(_kind(root_obj) == "group", f"root object is not a group: {root_path}")

    objects: dict[str, ExpectedObject] = {}
    objects[root_identity] = ExpectedObject(
        identity=root_identity,
        file_realpath=root_ctx.realpath,
        file_id=f"{root_ctx.device}:{root_ctx.inode}",
        object_path="/",
        kind="group",
        attrs=attr_snapshot(root_obj, f"{root_ctx.realpath}:/"),
        access_paths=["/"],
        output_representative="/",
    )
    edges: list[dict] = []
    # Queue entries carry both the source path context used to resolve links and
    # the output path the delivery builder would assign to that representative.
    queue = [(root_ctx, root_obj, "/", "/")]
    visited_groups = {root_identity}

    while queue:
        ctx, group, group_path, out_path = queue.pop(0)
        for name in sorted(group.keys()):
            edge_path = join_path(group_path, name)
            out_child = join_path(out_path, name)
            target, link = resolve_child(files, ctx, group, group_path, name)
            desc = link_descriptor(link)
            existing = objects.get(target.identity)
            representative = (
                existing.output_representative if existing is not None else out_child
            )
            edges.append(
                {
                    "source_path": edge_path,
                    "output_path": out_child,
                    "identity": target.identity,
                    "identity_representative": representative,
                    "link_type": desc["type"],
                    "origin": desc,
                }
            )
            if existing is not None:
                existing.aliases.append(
                    {
                        "source_path": edge_path,
                        "output_path": out_child,
                        "origin": desc,
                        "target_object_path": target.obj_path,
                    }
                )
                continue

            attrs = attr_snapshot(target.obj, f"{target.file.realpath}:{target.obj_path}")
            record = ExpectedObject(
                identity=target.identity,
                file_realpath=target.file.realpath,
                file_id=f"{target.file.device}:{target.file.inode}",
                object_path=target.obj_path,
                kind=target.kind,
                attrs=attrs,
                access_paths=[out_child],
                output_representative=out_child,
            )
            if target.kind == "dataset":
                dset = target.obj
                ref = h5py.check_dtype(ref=dset.dtype)
                check(
                    ref is None,
                    f"expected source uses unsupported reference dtype at "
                    f"{target.file.realpath}:{target.obj_path}",
                )
                check(
                    h5py.check_dtype(vlen=dset.dtype) is None,
                    f"expected source uses unsupported vlen dataset at "
                    f"{target.file.realpath}:{target.obj_path}",
                )
                if dset.is_virtual:
                    data, vds_meta = validate_vds_expected(files, target)
                    record.vds = vds_meta
                else:
                    data = np.asarray(dset[...])
                    vds_meta = None
                record.dataset = {
                    "shape": list(dset.shape),
                    "dtype": str(dset.dtype),
                    "fill_value": _json_scalar(dset.fillvalue),
                    "data": np.array(data, copy=True),
                }
            objects[target.identity] = record
            if target.kind == "group" and target.identity not in visited_groups:
                visited_groups.add(target.identity)
                queue.append((target.file, target.obj, target.obj_path, out_child))

    root_names = sorted(root_obj.keys())
    # Every declared VDS source file must be a real physical file (validated above).
    vds_files = {
        mapping["source"]["file_id"]
        for rec in objects.values()
        if rec.vds is not None
        for mapping in rec.vds["mappings"]
    }
    source_ids = {f"{ctx.device}:{ctx.inode}" for ctx in files.by_key.values()}
    return ExpectedModel(
        root_attrs=attr_snapshot(root_obj, f"{root_ctx.realpath}:/"),
        root_names=root_names,
        objects=objects,
        edges=edges,
        source_file_ids=source_ids,
        vds_source_file_ids=vds_files,
    )


# ---------------------------------------------------------------------------
# manifest checks


def validate_manifest_expected(manifest: dict, expected: ExpectedModel) -> None:
    check(manifest.get("format") == "hdf5-delivery-manifest", "bad manifest format")
    objects = manifest.get("objects")
    check(isinstance(objects, list), "manifest objects must be a list")
    ids = [o["identity"]["combined"] for o in objects]
    check(len(ids) == len(set(ids)), "manifest lists duplicate object identities")
    check(
        set(ids) == set(expected.objects),
        "manifest object identities differ from expected source objects: "
        f"missing={sorted(set(expected.objects)-set(ids))} "
        f"extra={sorted(set(ids)-set(expected.objects))}",
    )
    by_id = {o["identity"]["combined"]: o for o in objects}

    source_file_ids = {f["file_id"] for f in manifest.get("source_files", [])}
    check(
        source_file_ids == expected.source_file_ids
        or len(source_file_ids) >= len(expected.source_file_ids),
        f"manifest source files {sorted(source_file_ids)} do not cover "
        f"expected {sorted(expected.source_file_ids)}",
    )
    for file_id in expected.vds_source_file_ids:
        check(
            file_id in source_file_ids,
            f"manifest does not record VDS source file identity {file_id}",
        )

    for identity, exp in expected.objects.items():
        entry = by_id[identity]
        check(entry["source"]["file"] == exp.file_realpath, f"source file mismatch {identity}")
        check(
            entry["source"]["object_path"] == exp.object_path,
            f"source object path mismatch for {identity}: "
            f"{entry['source']['object_path']} vs {exp.object_path}",
        )
        check(entry["source"]["kind"] == exp.kind, f"kind mismatch {identity}")
        rep = entry["output_representative"]
        check(rep.startswith("/"), f"representative path must be absolute: {rep}")
        if exp.dataset is not None:
            check("dataset" in entry, f"manifest missing dataset metadata for {rep}")
            check(
                entry["dataset"]["shape"] == exp.dataset["shape"],
                f"dataset shape metadata mismatch for {rep}",
            )
            check(
                entry["dataset"]["dtype"] == exp.dataset["dtype"],
                f"dataset dtype metadata mismatch for {rep}",
            )
        if exp.vds is not None:
            check("vds" in entry, f"manifest missing VDS metadata for {rep}")
            got = entry["vds"]
            check(
                got["mappings"] and len(got["mappings"]) == len(exp.vds["mappings"]),
                f"VDS mapping count mismatch for {rep}",
            )
            for g, e in zip(got["mappings"], exp.vds["mappings"]):
                check(
                    g["source"]["file"] == e["source"]["file"]
                    and g["source"]["object_path"] == e["source"]["object_path"]
                    and g["element_count"] == e["element_count"],
                    f"VDS mapping source mismatch for {rep} mapping {e['index']}: {g} vs {e}",
                )
            check(
                got["mapped_elements"] == exp.vds["mapped_elements"]
                and got["unmapped_elements"] == exp.vds["unmapped_elements"],
                f"VDS mapped/unmapped element counts mismatch for {rep}",
            )
            check(
                got["fill_value"] == exp.vds["fill_value"],
                f"VDS fill value mismatch for {rep}",
            )

        got_aliases = entry.get("aliases", [])
        expected_alias_paths = {a["output_path"] for a in exp.aliases}
        got_alias_paths = {a["output_path"] for a in got_aliases}
        check(
            got_alias_paths == expected_alias_paths,
            f"alias paths mismatch for {identity} ({rep}): "
            f"expected {sorted(expected_alias_paths)}, got {sorted(got_alias_paths)}",
        )
        for alias in got_aliases:
            check(
                alias["pointing_to"] == rep,
                f"manifest alias {alias['output_path']} should point to {rep}",
            )

    counts = manifest.get("counts", {})
    check(counts.get("objects") == len(expected.objects), "manifest object count mismatch")
    check(
        counts.get("datasets")
        == sum(1 for o in expected.objects.values() if o.kind == "dataset"),
        "manifest dataset count mismatch",
    )
    links = manifest.get("links", [])
    check(
        {link["output_path"] for link in links}
        == {edge["output_path"] for edge in expected.edges},
        "manifest output link paths do not match the finite set of source access paths",
    )
    for link in links:
        edge = next(
            e for e in expected.edges if e["output_path"] == link["output_path"]
        )
        check(
            link["target"] == edge["output_path"]
            or link["target"] == edge["identity_representative"],
            f"manifest link {link['output_path']} targets {link['target']}",
        )
    check(
        all(link["kind"] == "hard" for link in links),
        "delivery manifest must record only hard output links",
    )


# ---------------------------------------------------------------------------
# delivery checks (after input has been moved away)


def iter_links(root: h5py.File):
    """Yield (path, link) for the whole reachable hard-link tree, cycle-safe."""
    seen: set[int] = set()

    def walk(group, prefix: str):
        addr = h5o.get_info(group.id).addr
        if addr in seen:
            return
        seen.add(addr)
        for name in group:
            path = join_path(prefix, name)
            link = group.get(name, getlink=True)
            yield path, link
            if isinstance(link, h5py.HardLink):
                child = group[name]
                if isinstance(child, h5py.Group):
                    yield from walk(child, path)

    yield from walk(root, "/")


def check_self_contained_links(root: h5py.File) -> None:
    for path, link in iter_links(root):
        check(
            isinstance(link, h5py.HardLink),
            f"delivery still contains {type(link).__name__} at {path}",
        )


def check_no_virtual_or_external(root: h5py.File) -> None:
    def visit(name, obj):
        path = "/" + name if name else "/"
        if isinstance(obj, h5py.Dataset):
            check(not obj.is_virtual, f"delivery contains a virtual dataset at {path}")
            plist = obj.id.get_create_plist()
            check(plist.get_layout() != h5d.VIRTUAL, f"virtual layout at {path}")
            check(
                plist.get_external_count() == 0,
                f"external raw storage remains at {path}",
            )
            check(
                h5py.check_dtype(ref=obj.dtype) is None,
                f"reference dtype remains at {path}",
            )

    root.visititems(visit)


def verify_isolated_copy(
    delivery_path: str,
    manifest: dict,
    expected: ExpectedModel,
) -> None:
    shown: dict[str, int] = {}
    try:
        out = h5py.File(delivery_path, "r")
    except Exception as exc:
        raise VerificationError(
            f"cannot reopen delivery after moving the input directory: {exc}"
        ) from exc
    with out:
        check(
            sorted(out.keys()) == expected.root_names,
            f"root link names changed: expected {expected.root_names}, got {sorted(out.keys())}",
        )
        assert_attrs_equal(expected.root_attrs, out, "delivery:/")

        check_self_contained_links(out)
        check_no_virtual_or_external(out)

        objects = {o["identity"]["combined"]: o for o in manifest["objects"]}
        for identity, exp in expected.objects.items():
            entry = objects[identity]
            rep = entry["output_representative"]
            check(rep in out, f"representative path missing from delivery: {rep}")
            obj = out[rep]
            check(
                _kind_of(obj) == exp.kind,
                f"object kind differs at {rep}: {_kind_of(obj)} vs {exp.kind}",
            )
            addr = int(h5o.get_info(obj.id).addr)
            shown[identity] = addr
            if exp.dataset is not None:
                check(isinstance(obj, h5py.Dataset), f"expected dataset at {rep}")
                check(
                    list(obj.shape) == exp.dataset["shape"],
                    f"dataset shape differs at {rep}: {obj.shape} vs {exp.dataset['shape']}",
                )
                check(
                    np.dtype(obj.dtype) == np.dtype(exp.dataset["dtype"]),
                    f"dataset dtype differs at {rep}: {obj.dtype} vs {exp.dataset['dtype']}",
                )
                check(
                    values_equal(obj.fillvalue, exp.dataset["fill_value"]),
                    f"dataset fill value differs at {rep}: "
                    f"{obj.fillvalue!r} vs {exp.dataset['fill_value']!r}",
                )
                actual = np.asarray(obj[...])
                expected_data = exp.dataset["data"]
                check(
                    actual.shape == expected_data.shape,
                    f"dataset read shape differs at {rep}",
                )
                check(
                    np.array_equal(actual, expected_data),
                    f"dataset values differ at {rep}: {actual!r} vs {expected_data!r}",
                )
                if len(actual) == 0:
                    check(
                        actual.size == 0,
                        f"empty dataset is not empty at {rep}",
                    )
            assert_attrs_equal(exp.attrs, obj, f"delivery:{rep}")

            for alias in entry.get("aliases", []):
                path = alias["output_path"]
                check(path in out, f"alias path missing in delivery: {path}")
                alias_obj = out[path]
                alias_addr = int(h5o.get_info(alias_obj.id).addr)
                check(
                    alias_addr == addr,
                    f"alias {path} is not the same output object as {rep} "
                    f"(addresses {alias_addr} vs {addr})",
                )
                check(
                    type(out.get(path, getlink=True)) is h5py.HardLink,
                    f"alias {path} is not a hard link in the delivery",
                )

        addresses = list(shown.values())
        check(
            len(addresses) == len(set(addresses)),
            "distinct source objects were collapsed into the same output object",
        )

        # Every finite source access path must still resolve, to the representative.
        for identity, exp in expected.objects.items():
            rep = objects[identity]["output_representative"]
            for path in exp.access_paths:
                if path == "/":
                    continue
                if path not in out:
                    continue
                got = int(h5o.get_info(out[path].id).addr)
                check(
                    got == shown[identity],
                    f"access path {path} no longer shares object identity with {rep}",
                )


def _kind_of(obj) -> str:
    t = h5o.get_info(obj.id).type
    if t == h5o.TYPE_GROUP:
        return "group"
    if t == h5o.TYPE_DATASET:
        return "dataset"
    return f"type{t}"


# ---------------------------------------------------------------------------
# negative fixtures and generated negative cases


@dataclass
class FailureCase:
    name: str
    source: str
    expected_tokens: tuple[str, ...]


def run_builder(input_path: str, output_path: str, cwd: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [
            sys.executable,
            "-m",
            "hdf5_delivery",
            "build",
            str(input_path),
            str(output_path),
            "--overwrite",
        ],
        cwd=cwd,
        text=True,
        capture_output=True,
    )


def assert_failure(case: FailureCase, output_dir: str, cwd: str) -> None:
    output = os.path.join(output_dir, f"fail-{case.name}.h5")
    manifest = os.path.join(output_dir, f"fail-{case.name}.json")
    if os.path.exists(output):
        os.unlink(output)
    if os.path.exists(manifest):
        os.unlink(manifest)
    result = run_builder(case.source, output, cwd)
    check(
        result.returncode != 0,
        f"{case.name}: builder unexpectedly succeeded (stdout={result.stdout!r})",
    )
    check(
        not os.path.exists(output),
        f"{case.name}: failed build left a partial delivery at {output}",
    )
    check(
        not os.path.exists(manifest),
        f"{case.name}: failed build left a partial manifest at {manifest}",
    )
    combined = result.stdout + result.stderr
    for token in case.expected_tokens:
        check(
            token in combined,
            f"{case.name}: failure message does not mention {token!r}: {combined!r}",
        )


def make_out_of_bounds_vds(directory: str) -> str:
    """A VDS whose declared source selection extends past the real source."""

    src = os.path.join(directory, "oob-source.h5")
    with h5py.File(src, "w") as f:
        f["x"] = np.array([10, 11, 12], dtype=np.int64)
    vds_path = os.path.join(directory, "oob-vds.h5")
    with h5py.File(vds_path, "w") as f:
        dcpl = h5py.h5p.create(h5py.h5p.DATASET_CREATE)
        dcpl.set_chunk((2,))
        vspace = h5py.h5s.create_simple((2,))
        vspace.select_hyperslab((0,), (2,), (1,), (1,))
        srcspace = h5py.h5s.create_simple((3,))
        srcspace.select_hyperslab((2,), (2,), (1,), (1,))
        dcpl.set_virtual(
            vspace, src.encode("utf-8"), b"x", srcspace
        )
        h5py.h5d.create(
            f.id, b"v", h5py.h5t.NATIVE_INT64, vspace, dcpl=dcpl
        ).close()
    return vds_path


def make_reference_dataset(directory: str) -> str:
    path = os.path.join(directory, "reference.h5")
    with h5py.File(path, "w") as f:
        f.create_dataset("r", (2,), dtype=h5py.special_dtype(ref=h5py.Reference))
    return path


# ---------------------------------------------------------------------------


def move_away(input_dir: str, quarantine: str) -> str:
    """Move the input directory away and return the destination path.

    The quarantine directory must live on the same filesystem as the input so
    that ``rename`` preserves device/inode identity; otherwise a copy-based
    move would silently give the restored files new identities.
    """

    original = os.path.abspath(input_dir)
    check(
        original not in ("/", os.path.abspath(os.getcwd())),
        f"refusing to move cwd/root as the input directory: {original}",
    )
    check(os.path.isdir(original), f"input directory does not exist: {original}")
    check(
        not os.path.exists(quarantine),
        f"quarantine destination already exists: {quarantine}",
    )

    def _same_device(left: str, right: str) -> bool:
        try:
            return os.stat(left).st_dev == os.stat(right).st_dev
        except OSError:
            return False

    if not _same_device(original, quarantine):
        # Moving across devices would copy the files and change inode identity
        # that the manifest records.  Relocate the quarantine next to the input.
        parent = os.path.dirname(original)
        quarantine = os.path.join(parent, f".hdf5-verify-quarantine-{os.getpid()}")
        check(not os.path.exists(quarantine), f"quarantine already exists: {quarantine}")

    os.makedirs(quarantine, exist_ok=True)
    destination = os.path.join(quarantine, "input-offline")
    shutil.move(original, destination)
    check(not os.path.exists(original), f"input directory is still present at {original}")
    return destination


def restore_input(destination: str, original: str) -> None:
    if os.path.exists(destination) and not os.path.exists(original):
        os.makedirs(os.path.dirname(original) or ".", exist_ok=True)
        shutil.move(destination, original)
    quarantine = os.path.dirname(destination)
    try:
        if quarantine.rstrip("/") != os.path.abspath(os.sep) and os.path.isdir(quarantine):
            os.rmdir(quarantine)
    except OSError:
        pass


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--input",
        default="fixtures/root.h5",
        help="root HDF5 input (default: fixtures/root.h5)",
    )
    parser.add_argument(
        "--delivery",
        help="existing delivery.h5 to verify; if omitted a fresh one is built",
    )
    parser.add_argument(
        "--manifest",
        help="manifest for --delivery (default: manifest.json next to it)",
    )
    parser.add_argument(
        "--failure-fixtures",
        default=None,
        help=(
            "directory containing dangling.h5, soft-cycle.h5 and missing-vds.h5 "
            "(default: the input directory, falling back to fixtures/)"
        ),
    )
    parser.add_argument(
        "--keep",
        action="store_true",
        help="keep the temporary build/isolation directory",
    )
    args = parser.parse_args(argv)

    repo_root = str(Path(__file__).resolve().parent)
    input_path = os.path.abspath(args.input)
    input_dir = os.path.dirname(input_path)
    check(os.path.isfile(input_path), f"input file does not exist: {input_path}")
    check(
        os.path.abspath(input_dir) not in ("/", repo_root),
        "verification moves the input directory; point --input at a dedicated "
        "fixture directory",
    )
    failure_dir = (
        os.path.abspath(args.failure_fixtures)
        if args.failure_fixtures
        else input_dir
    )
    if not os.path.isdir(failure_dir) or not all(
        os.path.isfile(os.path.join(failure_dir, f"{name}.h5"))
        for name in ("dangling", "soft-cycle", "missing-vds")
    ):
        fallback = os.path.join(repo_root, "fixtures")
        if os.path.isdir(fallback):
            failure_dir = fallback

    workdir = tempfile.mkdtemp(prefix="hdf5-verify-")
    quarantine = os.path.join(workdir, "quarantine")
    isolated = os.path.join(workdir, "isolated")
    build_dir = os.path.join(workdir, "build")
    for path in (isolated, build_dir):
        os.makedirs(path, exist_ok=True)

    files = SourceFiles()
    moved_to: str | None = None
    try:
        # 1. expected model from the source while it is available.
        expected = capture_expected(files, input_path)
        print(
            f"captured expected model: {len(expected.objects)} objects, "
            f"{len(expected.edges)} finite access paths, "
            f"{len(expected.vds_source_file_ids)} VDS source file(s)"
        )

        # 2. build a fresh delivery through the CLI unless one was supplied.
        if args.delivery:
            delivery = os.path.abspath(args.delivery)
            manifest_path = (
                os.path.abspath(args.manifest)
                if args.manifest
                else os.path.join(os.path.dirname(delivery), "manifest.json")
            )
            check(os.path.isfile(delivery), f"delivery does not exist: {delivery}")
            check(
                os.path.isfile(manifest_path),
                f"manifest does not exist: {manifest_path}",
            )
        else:
            delivery = os.path.join(build_dir, "delivery.h5")
            manifest_path = os.path.join(build_dir, "manifest.json")
            result = run_builder(input_path, delivery, repo_root)
            check(
                result.returncode == 0,
                f"builder failed: {result.stdout}\n{result.stderr}",
            )
            check(os.path.isfile(delivery), "builder produced no delivery file")
            check(os.path.isfile(manifest_path), "builder produced no manifest")
        with open(manifest_path, "r", encoding="utf-8") as fh:
            manifest = json.load(fh)

        # 3. manifest must describe the source model faithfully.
        validate_manifest_expected(manifest, expected)

        # 4. isolate: copy delivery + manifest; move the input directory away.
        isolated_delivery = os.path.join(isolated, "delivery.h5")
        isolated_manifest = os.path.join(isolated, "manifest.json")
        shutil.copy2(delivery, isolated_delivery)
        shutil.copy2(manifest_path, isolated_manifest)
        files.close()

        moved_to = move_away(input_dir, quarantine)
        print(f"input directory moved away: {input_dir} -> {moved_to}")

        # Change cwd so any relative resolution has no source directory context.
        neutral = os.path.join(workdir, "neutral")
        os.makedirs(neutral, exist_ok=True)
        old_cwd = os.getcwd()
        os.chdir(neutral)
        try:
            verify_isolated_copy(isolated_delivery, manifest, expected)
        finally:
            os.chdir(old_cwd)
        print("isolated delivery reopened and every object verified")

        # restore the input for the negative fixture runs.
        restore_input(moved_to, input_dir)
        moved_to = None

        # 5. required negative fixtures.
        cases = [
            FailureCase(
                "dangling-external",
                os.path.join(failure_dir, "dangling.h5"),
                ("absent.h5", "/missing"),
            ),
            FailureCase(
                "soft-cycle",
                os.path.join(failure_dir, "soft-cycle.h5"),
                ("cyclic", "soft-cycle.h5", "/a"),
            ),
            FailureCase(
                "missing-vds-source",
                os.path.join(failure_dir, "missing-vds.h5"),
                ("VDS", "absent.h5", "/v"),
            ),
        ]
        for case in cases:
            if not os.path.isfile(case.source):
                raise VerificationError(
                    f"required failure fixture missing: {case.source}"
                )
            assert_failure(case, build_dir, repo_root)
            print(f"negative case rejected as expected: {case.name}")

        # 6. generated negative cases: out-of-bounds VDS, reference dtype.
        generated = os.path.join(workdir, "generated")
        os.makedirs(generated, exist_ok=True)
        oob = FailureCase(
            "vds-out-of-bounds",
            make_out_of_bounds_vds(generated),
            ("VDS", "out-of-bounds"),
        )
        assert_failure(oob, generated, repo_root)
        print("generated negative case rejected: vds-out-of-bounds")

        ref = FailureCase(
            "reference-dtype",
            make_reference_dataset(generated),
            ("reference",),
        )
        assert_failure(ref, generated, repo_root)
        print("generated negative case rejected: reference-dtype")

        print(
            "PASS: delivery is self-contained, object sharing is preserved, "
            "values/attributes/empty arrays match, aliases share identity, "
            "and all required failure cases were rejected."
        )
        return 0
    except VerificationError as exc:
        print(f"VERIFICATION FAILED: {exc}", file=sys.stderr)
        traceback.print_exc()
        return 1
    finally:
        files.close()
        if moved_to is not None:
            restore_input(moved_to, input_dir)
        if not args.keep:
            shutil.rmtree(workdir, ignore_errors=True)
        else:
            print(f"temporary verification directory kept: {workdir}")


if __name__ == "__main__":
    raise SystemExit(main())
