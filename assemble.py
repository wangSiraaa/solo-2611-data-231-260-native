#!/usr/bin/env python3
"""Assemble tile-level annotations back into original-image coordinates.

The program is deliberately limited to the operations named by the task:

    crop -> integer nearest-neighbour upscale -> optional horizontal flip

No visual recognition, no instance matching, no rendering and no
anti-aliased resampling is performed.  Tile-local positive labels are
interpreted only through the explicit ``instance_map`` supplied in the plan.

Outputs (under ``OUT``):

    state.png               state codes from scope.json
    coverage.png            1 where at least one valid observation exists
    coverage_count.png      number of valid tile pixels observed per original pixel
    coverage_count.npy      same data, exact integer array
    instance_labels.png     label index per line instance (0 = not certain instance)
    instances/*.png         binary certain-instance mask per instance
    instances.json          COCO-style image/category/annotation table
    instances.csv           flat instance table
    report.json             size/transform checks, coverage and conflict report
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

import numpy as np
from PIL import Image


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

TRANSFORM_ORDER = [
    "crop",
    "integer nearest upscale",
    "horizontal flip",
]

STATE_UNKNOWN = 0
STATE_CERTAIN_BACKGROUND = 1
STATE_CERTAIN_INSTANCE = 2
STATE_CONFLICT = 3

STATE_NAMES = {
    STATE_UNKNOWN: "unknown",
    STATE_CERTAIN_BACKGROUND: "certain_background",
    STATE_CERTAIN_INSTANCE: "certain_instance",
    STATE_CONFLICT: "conflict",
}

# Internal sentinel for a valid, observed background sample.
BACKGROUND = object()


class AssemblyError(Exception):
    """Raised when the batch must be rejected as a whole."""

    def __init__(self, errors: list[dict[str, Any]], message: str = "assembly rejected"):
        self.errors = errors
        super().__init__(message)


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _err(code: str, message: str, **details: Any) -> dict[str, Any]:
    out: dict[str, Any] = {"code": code, "message": message}
    out.update(details)
    return out


def _resolve(base: Path, value: str) -> Path:
    p = Path(value)
    return p if p.is_absolute() else (base / p)


def _read_single_channel(path: Path, kind: str) -> np.ndarray:
    """Read a mask-like image and require a single-channel integer array.

    Palette (P) images are read as palette indices, which is exactly the
    label semantics expected here.  Mode ``1`` is normalised to 0/1.
    """

    with Image.open(path) as im:
        im.load()
        mode = im.mode
        arr = np.asarray(im)

    if arr.ndim != 2:
        raise ValueError(
            f"{kind} must be a single-channel image, got mode={mode} shape={arr.shape}"
        )
    if arr.dtype == np.bool_:
        arr = arr.astype(np.uint8)
    if not np.issubdtype(arr.dtype, np.integer):
        raise ValueError(
            f"{kind} must contain integer labels, got mode={mode} dtype={arr.dtype}"
        )
    return arr


def _read_image(path: Path) -> tuple[np.ndarray, np.ndarray, str, tuple[int, int]]:
    with Image.open(path) as im:
        im.load()
        mode = im.mode
        size = im.size  # (width, height)
        raw = np.asarray(im)
        rgb = np.asarray(im.convert("RGB"))
    return raw, rgb, mode, size


def encode_rle(mask: np.ndarray) -> list[int]:
    """COCO-style uncompressed RLE.

    Counts are generated in column-major (Fortran) order and the first run is
    always a run of zeros (possibly of length zero).
    """

    if mask.ndim != 2:
        raise ValueError("RLE mask must be two-dimensional")
    flat = np.asarray(mask, dtype=bool).ravel(order="F")
    counts: list[int] = []
    current = 0  # background
    run = 0
    for value in flat:
        v = 1 if value else 0
        if v == current:
            run += 1
        else:
            counts.append(run)
            run = 1
            current = v
    counts.append(run)
    return [int(c) for c in counts]


def decode_rle(counts: Iterable[int], size: tuple[int, int]) -> np.ndarray:
    """Decode COCO uncompressed RLE.  ``size`` is (height, width)."""

    height, width = size
    counts = [int(c) for c in counts]
    if any(c < 0 for c in counts):
        raise ValueError("RLE counts must be non-negative")
    if sum(counts) != height * width:
        raise ValueError(
            f"RLE counts sum to {sum(counts)}, expected {height * width}"
        )
    flat = np.zeros(height * width, dtype=np.uint8)
    pos = 0
    value = 0
    for count in counts:
        if value:
            flat[pos : pos + count] = 1
        pos += count
        value ^= 1
    return flat.reshape((height, width), order="F").astype(bool)


def bbox_from_mask(mask: np.ndarray) -> list[int] | None:
    """Return [x, y, w, h] from the actual binary mask, or None if empty."""

    rows = np.any(mask, axis=1)
    cols = np.any(mask, axis=0)
    if not rows.any():
        return None
    y0 = int(np.argmax(rows))
    y1 = int(len(rows) - 1 - np.argmax(rows[::-1]))
    x0 = int(np.argmax(cols))
    x1 = int(len(cols) - 1 - np.argmax(cols[::-1]))
    return [x0, y0, x1 - x0 + 1, y1 - y0 + 1]


def _safe_name(value: str) -> str:
    safe = re.sub(r"[^0-9A-Za-z._-]+", "_", value).strip("._")
    return safe or "instance"


# ---------------------------------------------------------------------------
# Plan parsing / validation
# ---------------------------------------------------------------------------


@dataclass
class TileSpec:
    tile_id: str
    order: int
    crop: tuple[int, int, int, int]  # x, y, width, height
    scale: int
    horizontal_flip: bool
    image_path: Path
    mask_path: Path
    valid_path: Path
    instance_map: dict[int, str]

    # loaded data / audit information
    image: np.ndarray | None = None
    image_rgb: np.ndarray | None = None
    mask: np.ndarray | None = None
    valid: np.ndarray | None = None
    image_mode: str | None = None
    image_size: tuple[int, int] | None = None
    mask_size: tuple[int, int] | None = None
    valid_size: tuple[int, int] | None = None
    expected_size: tuple[int, int] | None = None
    image_matches_transform: bool | None = None


@dataclass
class Plan:
    plan_path: Path
    base_dir: Path
    image_id: str
    original_path: Path
    width: int
    height: int
    original: np.ndarray | None
    original_mode: str | None
    tiles: list[TileSpec]
    instances: list[tuple[str, int]]  # ordered (instance_id, category_id)
    category_by_instance: dict[str, int]
    transform_order: list[str] = field(default_factory=lambda: list(TRANSFORM_ORDER))


def _parse_plan(plan_path: Path, *, strict_image_pixels: bool) -> Plan:
    errors: list[dict[str, Any]] = []

    try:
        raw = json.loads(plan_path.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001 - reported as a validation error
        raise AssemblyError(
            [_err("plan_unreadable", f"cannot read plan: {exc}", path=str(plan_path))]
        ) from exc

    if not isinstance(raw, dict):
        raise AssemblyError([_err("plan_invalid", "plan root must be a JSON object")])

    base_dir = plan_path.parent

    transform_order = raw.get("transform_order", TRANSFORM_ORDER)
    if transform_order != TRANSFORM_ORDER:
        errors.append(
            _err(
                "unsupported_transform_order",
                "this assembler implements exactly crop -> integer nearest upscale -> horizontal flip",
                found=transform_order,
                expected=TRANSFORM_ORDER,
            )
        )

    image_id = raw.get("image_id")
    if not isinstance(image_id, str) or not image_id:
        errors.append(_err("invalid_image_id", "image_id must be a non-empty string"))
        image_id = ""

    original_value = raw.get("original")
    if not isinstance(original_value, str) or not original_value:
        errors.append(_err("invalid_original", "original must be a non-empty path string"))
        original_path = base_dir / "__missing_original__"
    else:
        original_path = _resolve(base_dir, original_value)

    original: np.ndarray | None = None
    original_rgb: np.ndarray | None = None
    original_mode: str | None = None
    if not original_path.is_file():
        errors.append(
            _err("original_missing", "original image does not exist", path=str(original_path))
        )
        image_w = image_h = 0
    else:
        try:
            original, original_rgb, original_mode, image_size = _read_image(original_path)
            image_w, image_h = image_size
        except Exception as exc:  # noqa: BLE001
            errors.append(
                _err("original_unreadable", f"cannot read original: {exc}", path=str(original_path))
            )
            image_w = image_h = 0

    width = raw.get("width")
    height = raw.get("height")
    if width is None:
        width = image_w
    if height is None:
        height = image_h
    if not _is_int(width) or width <= 0:
        errors.append(_err("invalid_width", "width must be a positive integer", value=width))
        width = image_w
    if not _is_int(height) or height <= 0:
        errors.append(_err("invalid_height", "height must be a positive integer", value=height))
        height = image_h
    if image_w and image_h and (width, height) != (image_w, image_h):
        errors.append(
            _err(
                "original_size_mismatch",
                "plan width/height do not match the original image size",
                plan=[width, height],
                image=[image_w, image_h],
            )
        )

    # ---- instances ---------------------------------------------------------
    instances_raw = raw.get("instances")
    instances: list[tuple[str, int]] = []
    category_by_instance: dict[str, int] = {}
    if not isinstance(instances_raw, list):
        errors.append(_err("invalid_instances", "instances must be a list"))
        instances_raw = []
    for i, item in enumerate(instances_raw):
        if not isinstance(item, dict):
            errors.append(
                _err("invalid_instance_entry", "instance entry must be an object", index=i)
            )
            continue
        iid = item.get("id")
        cid = item.get("category_id")
        if not isinstance(iid, str) or not iid:
            errors.append(
                _err("invalid_instance_id", "instance id must be a non-empty string", index=i)
            )
            continue
        if not _is_int(cid):
            errors.append(
                _err(
                    "invalid_category_id",
                    "category_id must be an integer",
                    instance_id=iid,
                    value=cid,
                )
            )
            continue
        if iid in category_by_instance:
            if category_by_instance[iid] != cid:
                errors.append(
                    _err(
                        "conflicting_instance_category",
                        "the same global instance is declared with contradictory categories",
                        instance_id=iid,
                        categories=[category_by_instance[iid], cid],
                    )
                )
            # Identical duplicate is harmless; keep the first declaration.
            continue
        category_by_instance[iid] = cid
        instances.append((iid, cid))

    # ---- tiles -------------------------------------------------------------
    tiles_raw = raw.get("tiles")
    tiles: list[TileSpec] = []
    if not isinstance(tiles_raw, list) or not tiles_raw:
        errors.append(_err("invalid_tiles", "tiles must be a non-empty list"))
        tiles_raw = []

    seen_tile_ids: set[str] = set()
    for index, item in enumerate(tiles_raw):
        tile_errors_before = len(errors)
        if not isinstance(item, dict):
            errors.append(_err("invalid_tile", "tile entry must be an object", index=index))
            continue

        tile_id = item.get("tile_id")
        if not isinstance(tile_id, str) or not tile_id:
            errors.append(
                _err("invalid_tile_id", "tile_id must be a non-empty string", index=index)
            )
            tile_id = f"__tile_{index}__"
        if tile_id in seen_tile_ids:
            errors.append(
                _err("duplicate_tile_id", "tile_id must be unique", tile_id=tile_id, index=index)
            )
        seen_tile_ids.add(tile_id)

        crop = item.get("crop")
        if (
            not isinstance(crop, (list, tuple))
            or len(crop) != 4
            or not all(_is_int(v) for v in crop)
        ):
            errors.append(
                _err(
                    "invalid_crop",
                    "crop must be [x, y, width, height] with integer values",
                    tile_id=tile_id,
                    value=crop,
                )
            )
            crop_tuple = (0, 0, 0, 0)
        else:
            crop_tuple = tuple(int(v) for v in crop)  # type: ignore[assignment]
            cx, cy, cw, ch = crop_tuple
            if cx < 0 or cy < 0 or cw <= 0 or ch <= 0:
                errors.append(
                    _err(
                        "invalid_crop",
                        "crop x/y must be >= 0 and width/height must be > 0",
                        tile_id=tile_id,
                        crop=list(crop_tuple),
                    )
                )
            elif image_w and image_h and (cx + cw > image_w or cy + ch > image_h):
                errors.append(
                    _err(
                        "crop_out_of_bounds",
                        "crop extends outside the original image",
                        tile_id=tile_id,
                        crop=list(crop_tuple),
                        original_size=[image_w, image_h],
                    )
                )

        scale = item.get("scale")
        if not _is_int(scale) or scale <= 0:
            errors.append(
                _err(
                    "invalid_scale",
                    "scale must be a positive integer (nearest-neighbour upscale)",
                    tile_id=tile_id,
                    value=scale,
                )
            )
            scale_int = 1
        else:
            scale_int = int(scale)

        flip = item.get("horizontal_flip")
        if not isinstance(flip, bool):
            errors.append(
                _err(
                    "invalid_horizontal_flip",
                    "horizontal_flip must be a boolean",
                    tile_id=tile_id,
                    value=flip,
                )
            )
            flip_bool = False
        else:
            flip_bool = flip

        paths: dict[str, Path] = {}
        for key, code in (
            ("image", "tile_image_missing"),
            ("mask", "tile_mask_missing"),
            ("valid", "tile_valid_missing"),
        ):
            value = item.get(key)
            if not isinstance(value, str) or not value:
                errors.append(
                    _err(
                        "invalid_tile_path",
                        f"{key} must be a non-empty path string",
                        tile_id=tile_id,
                        field=key,
                        value=value,
                    )
                )
                paths[key] = base_dir / f"__missing_{tile_id}_{key}__"
            else:
                paths[key] = _resolve(base_dir, value)
                if not paths[key].is_file():
                    errors.append(
                        _err(code, f"{key} file does not exist", tile_id=tile_id, path=str(paths[key]))
                    )

        instance_map_raw = item.get("instance_map")
        instance_map: dict[int, str] = {}
        if not isinstance(instance_map_raw, dict):
            errors.append(
                _err(
                    "invalid_instance_map",
                    "instance_map must be an object mapping tile-local ids to global instance ids",
                    tile_id=tile_id,
                )
            )
        else:
            for key, value in instance_map_raw.items():
                try:
                    local_id = int(key)
                except (TypeError, ValueError):
                    errors.append(
                        _err(
                            "invalid_instance_map_key",
                            "instance_map keys must be positive integer tile-local ids",
                            tile_id=tile_id,
                            key=key,
                        )
                    )
                    continue
                if local_id <= 0:
                    errors.append(
                        _err(
                            "invalid_instance_map_key",
                            "local label 0 is observed background and must not be mapped",
                            tile_id=tile_id,
                            key=key,
                        )
                    )
                    continue
                if local_id in instance_map:
                    errors.append(
                        _err(
                            "duplicate_instance_map_key",
                            "instance_map contains duplicate integer local ids",
                            tile_id=tile_id,
                            local_id=local_id,
                        )
                    )
                    continue
                if not isinstance(value, str) or not value:
                    errors.append(
                        _err(
                            "invalid_instance_map_value",
                            "instance_map values must be non-empty global instance ids",
                            tile_id=tile_id,
                            local_id=local_id,
                            value=value,
                        )
                    )
                    continue
                if value not in category_by_instance:
                    errors.append(
                        _err(
                            "undeclared_global_instance",
                            "instance_map refers to a global instance not declared in instances",
                            tile_id=tile_id,
                            local_id=local_id,
                            global_instance_id=value,
                        )
                    )
                instance_map[local_id] = value

        spec = TileSpec(
            tile_id=tile_id,
            order=index,
            crop=crop_tuple,
            scale=scale_int,
            horizontal_flip=flip_bool,
            image_path=paths["image"],
            mask_path=paths["mask"],
            valid_path=paths["valid"],
            instance_map=instance_map,
        )

        # Load and check data even when earlier metadata checks failed, so the
        # report can enumerate as many concrete problems as possible.
        if spec.image_path.is_file():
            try:
                (
                    spec.image,
                    spec.image_rgb,
                    spec.image_mode,
                    spec.image_size,
                ) = _read_image(spec.image_path)
            except Exception as exc:  # noqa: BLE001
                errors.append(
                    _err(
                        "tile_image_unreadable",
                        f"cannot read tile image: {exc}",
                        tile_id=tile_id,
                        path=str(spec.image_path),
                    )
                )
        if spec.mask_path.is_file():
            try:
                spec.mask = _read_single_channel(spec.mask_path, "mask")
                spec.mask_size = (int(spec.mask.shape[1]), int(spec.mask.shape[0]))
            except Exception as exc:  # noqa: BLE001
                errors.append(
                    _err(
                        "tile_mask_unreadable",
                        f"cannot read tile mask: {exc}",
                        tile_id=tile_id,
                        path=str(spec.mask_path),
                    )
                )
        if spec.valid_path.is_file():
            try:
                spec.valid = _read_single_channel(spec.valid_path, "valid")
                spec.valid_size = (int(spec.valid.shape[1]), int(spec.valid.shape[0]))
            except Exception as exc:  # noqa: BLE001
                errors.append(
                    _err(
                        "tile_valid_unreadable",
                        f"cannot read valid mask: {exc}",
                        tile_id=tile_id,
                        path=str(spec.valid_path),
                    )
                )

        cx, cy, cw, ch = spec.crop
        if cw > 0 and ch > 0:
            spec.expected_size = (cw * spec.scale, ch * spec.scale)
        expected = spec.expected_size

        if expected is not None:
            if spec.image_size is not None and spec.image_size != expected:
                errors.append(
                    _err(
                        "tile_image_size_mismatch",
                        "tile image size does not match crop * scale",
                        tile_id=tile_id,
                        image_size=list(spec.image_size),
                        expected=list(expected),
                    )
                )
            if spec.mask_size is not None and spec.mask_size != expected:
                errors.append(
                    _err(
                        "tile_mask_size_mismatch",
                        "tile mask size does not match crop * scale",
                        tile_id=tile_id,
                        mask_size=list(spec.mask_size),
                        expected=list(expected),
                    )
                )
            if spec.valid_size is not None and spec.valid_size != expected:
                errors.append(
                    _err(
                        "tile_valid_size_mismatch",
                        "tile valid mask size does not match crop * scale",
                        tile_id=tile_id,
                        valid_size=list(spec.valid_size),
                        expected=list(expected),
                    )
                )

        # Content check is informational unless strict mode is requested.  A
        # grayscale/palette tile is compared through its RGB conversion so the
        # check works regardless of the stored mode.
        if (
            original_rgb is not None
            and spec.image_rgb is not None
            and spec.image_size == expected
            and cw > 0
            and ch > 0
            and 0 <= cx <= image_w - cw
            and 0 <= cy <= image_h - ch
        ):
            patch = original_rgb[cy : cy + ch, cx : cx + cw]
            expected_pixels = np.repeat(
                np.repeat(patch, spec.scale, axis=0), spec.scale, axis=1
            )
            if spec.horizontal_flip:
                expected_pixels = expected_pixels[:, ::-1]
            spec.image_matches_transform = bool(
                expected_pixels.shape == spec.image_rgb.shape
                and np.array_equal(expected_pixels, spec.image_rgb)
            )
            if strict_image_pixels and not spec.image_matches_transform:
                errors.append(
                    _err(
                        "tile_image_transform_mismatch",
                        "tile image pixels do not equal the transformed original crop",
                        tile_id=tile_id,
                    )
                )

        if spec.valid is not None:
            values = np.unique(spec.valid)
            if not np.all((values == 0) | (values == 1)):
                errors.append(
                    _err(
                        "invalid_valid_values",
                        "valid mask may contain only 0 (unobserved) and 1 (observed)",
                        tile_id=tile_id,
                        values=[int(v) for v in values.tolist()],
                    )
                )
        if spec.mask is not None and spec.mask.size:
            if int(spec.mask.min()) < 0:
                errors.append(
                    _err(
                        "negative_label",
                        "label mask must not contain negative labels",
                        tile_id=tile_id,
                        min_label=int(spec.mask.min()),
                    )
                )

        # Positive labels that are actually observed must be declared.
        if spec.mask is not None and spec.valid is not None:
            if spec.mask.shape == spec.valid.shape:
                observed = spec.mask[spec.valid == 1]
                positives = np.unique(observed[observed > 0])
                for label in positives.tolist():
                    label = int(label)
                    if label not in spec.instance_map:
                        count = int(
                            np.count_nonzero((spec.valid == 1) & (spec.mask == label))
                        )
                        errors.append(
                            _err(
                                "unmapped_local_label",
                                "positive label observed in valid pixels has no instance_map entry",
                                tile_id=tile_id,
                                local_id=label,
                                valid_pixel_count=count,
                            )
                        )

        tiles.append(spec)

        # Avoid an explosion of repeated errors from a wholly broken file.
        if tile_errors_before == len(errors) and len(errors) > 100:
            break

    if errors:
        raise AssemblyError(errors)

    assert original is not None  # implied by absence of errors
    return Plan(
        plan_path=plan_path,
        base_dir=base_dir,
        image_id=image_id,
        original_path=original_path,
        width=int(width),
        height=int(height),
        original=original,
        original_mode=original_mode,
        tiles=tiles,
        instances=instances,
        category_by_instance=category_by_instance,
        transform_order=list(TRANSFORM_ORDER),
    )


# ---------------------------------------------------------------------------
# Assembly
# ---------------------------------------------------------------------------


@dataclass
class Assembly:
    plan: Plan
    state: np.ndarray
    coverage_binary: np.ndarray
    coverage_count: np.ndarray
    instance_masks: dict[str, np.ndarray]
    label_indices: dict[str, int]
    instance_labels: np.ndarray
    observation_details: dict[tuple[int, int], dict[Any, list[dict[str, Any]]]]
    instance_table: list[dict[str, Any]]
    coco: dict[str, Any]
    report: dict[str, Any]


def _cell_provenance(spec: TileSpec, local_id: int, count: int, cell: tuple[int, int]) -> dict[str, Any]:
    cx, cy, _, _ = spec.crop
    col, row = cell
    return {
        "tile_id": spec.tile_id,
        "tile_order": spec.order,
        "local_id": int(local_id),
        "valid_pixel_count": int(count),
        "crop_cell": [int(col), int(row)],
        "original_cell": [int(cx + col), int(cy + row)],
    }


def build_assembly(
    plan_path: str | os.PathLike[str],
    *,
    strict_image_pixels: bool = False,
    verify_image_pixels: bool = True,
) -> Assembly:
    plan_path = Path(plan_path)
    if strict_image_pixels:
        verify_image_pixels = True
    plan = _parse_plan(plan_path, strict_image_pixels=strict_image_pixels)

    height, width = plan.height, plan.width
    observation_details: dict[tuple[int, int], dict[Any, list[dict[str, Any]]]] = {}
    coverage_count = np.zeros((height, width), dtype=np.uint32)

    for spec in plan.tiles:
        assert spec.mask is not None and spec.valid is not None
        cx, cy, cw, ch = spec.crop
        scale = spec.scale

        valid = spec.valid == 1
        labels = spec.mask

        # Inverse transform: undo flip, then collapse each scale x scale
        # nearest-neighbour block back to one source pixel.
        if spec.horizontal_flip:
            valid = valid[:, ::-1]
            labels = labels[:, ::-1]

        # (ch, scale, cw, scale) -> (ch, cw, scale, scale)
        valid_blocks = valid.reshape(ch, scale, cw, scale).transpose(0, 2, 1, 3)
        label_blocks = labels.reshape(ch, scale, cw, scale).transpose(0, 2, 1, 3)

        for row in range(ch):
            y = cy + row
            for col in range(cw):
                x = cx + col
                cell_valid = valid_blocks[row, col]
                observed_count = int(np.count_nonzero(cell_valid))
                if observed_count == 0:
                    continue

                cell_labels = label_blocks[row, col][cell_valid]
                unique, counts = np.unique(cell_labels, return_counts=True)

                # Every valid tile pixel is an observation.  Several tile
                # pixels in one source cell are counted for coverage, while
                # distinct identities are retained as separate candidates.
                coverage_count[y, x] += observed_count
                per_identity = observation_details.setdefault((y, x), {})
                for local_id_raw, count_raw in zip(unique.tolist(), counts.tolist()):
                    local_id = int(local_id_raw)
                    identity = BACKGROUND if local_id == 0 else spec.instance_map[local_id]
                    per_identity.setdefault(identity, []).append(
                        _cell_provenance(spec, local_id, int(count_raw), (col, row))
                    )

    state = np.full((height, width), STATE_UNKNOWN, dtype=np.uint8)
    instance_masks: dict[str, np.ndarray] = {
        iid: np.zeros((height, width), dtype=bool) for iid, _ in plan.instances
    }

    for y in range(height):
        for x in range(width):
            per_identity = observation_details.get((y, x))
            if not per_identity:
                state[y, x] = STATE_UNKNOWN
                continue
            if len(per_identity) == 1:
                identity = next(iter(per_identity))
                if identity is BACKGROUND:
                    state[y, x] = STATE_CERTAIN_BACKGROUND
                else:
                    state[y, x] = STATE_CERTAIN_INSTANCE
                    instance_masks[identity][y, x] = True
            else:
                state[y, x] = STATE_CONFLICT

    coverage_binary = (coverage_count > 0).astype(np.uint8)

    # Global instance-label image; only certain instance pixels are labelled.
    label_indices = {iid: i + 1 for i, (iid, _) in enumerate(plan.instances)}
    label_dtype = np.uint16 if len(label_indices) <= 65535 else np.int32
    instance_labels = np.zeros((height, width), dtype=label_dtype)
    for iid, mask in instance_masks.items():
        instance_labels[mask] = label_indices[iid]

    # ---- per-instance data -------------------------------------------------
    # Count observations per identity and conflicts per candidate, and note
    # which local ids reference each instance even if nothing was observed.
    observed_contributions: dict[str, int] = {}
    conflict_pixels_by_instance: dict[str, int] = {}
    for (y, x), per_identity in observation_details.items():
        for identity, entries in per_identity.items():
            if identity is BACKGROUND:
                continue
            observed_contributions[identity] = observed_contributions.get(identity, 0) + sum(
                entry["valid_pixel_count"] for entry in entries
            )
            if state[y, x] == STATE_CONFLICT:
                conflict_pixels_by_instance[identity] = (
                    conflict_pixels_by_instance.get(identity, 0) + 1
                )

    referenced_by: dict[str, list[dict[str, Any]]] = {iid: [] for iid, _ in plan.instances}
    for spec in plan.tiles:
        for local_id, iid in sorted(spec.instance_map.items()):
            referenced_by.setdefault(iid, []).append(
                {"tile_id": spec.tile_id, "local_id": int(local_id)}
            )

    instance_table: list[dict[str, Any]] = []
    annotations: list[dict[str, Any]] = []
    mask_files: dict[str, str] = {}

    used_names: set[str] = set()
    for idx, (iid, category_id) in enumerate(plan.instances):
        safe = _safe_name(iid)
        base = f"{idx:03d}_{safe}"
        candidate = base
        suffix = 1
        while candidate in used_names:
            suffix += 1
            candidate = f"{base}_{suffix}"
        used_names.add(candidate)
        mask_files[iid] = f"instances/{candidate}_mask.png"

        mask = instance_masks[iid]
        area = int(np.count_nonzero(mask))
        bbox = bbox_from_mask(mask)
        counts = encode_rle(mask)
        observations = int(observed_contributions.get(iid, 0))
        conflicts = int(conflict_pixels_by_instance.get(iid, 0))

        empty_reason: str | None = None
        if area == 0:
            if not referenced_by.get(iid):
                empty_reason = "not_referenced_by_any_tile_instance_map"
            elif observations == 0:
                empty_reason = "mapped_labels_never_observed_in_valid_pixels"
            else:
                empty_reason = "all_observed_pixels_are_conflicts"

        table_row = {
            "instance_id": iid,
            "category_id": int(category_id),
            "label_index": int(label_indices[iid]),
            "area": area,
            "bbox": bbox,
            "state": "certain" if area > 0 else "empty",
            "empty_reason": empty_reason,
            "certain_pixels": area,
            "observed_valid_pixel_contributions": observations,
            "conflict_pixels_mentioning_instance": conflicts,
            "referenced_by": referenced_by.get(iid, []),
            "mask_file": mask_files[iid],
            "segmentation": {"counts": counts, "size": [height, width]},
        }
        instance_table.append(table_row)

        annotations.append(
            {
                "id": idx + 1,
                "image_id": plan.image_id,
                "category_id": int(category_id),
                "instance_id": iid,
                "segmentation": {"counts": counts, "size": [height, width]},
                "area": area,
                "bbox": bbox,  # None for an empty mask
                "iscrowd": 0,
                "state": table_row["state"],
                "empty_reason": empty_reason,
            }
        )

    # ---- conflict report ---------------------------------------------------
    conflicts: list[dict[str, Any]] = []
    for (y, x) in sorted(observation_details.keys()):
        if state[y, x] != STATE_CONFLICT:
            continue
        per_identity = observation_details[(y, x)]

        candidates: list[dict[str, Any]] = []
        identities_by_tile: dict[str, set[Any]] = {}
        for identity, entries in per_identity.items():
            if identity is BACKGROUND:
                kind = "background"
                identity_value: str | None = None
                identity_label = "background"
            else:
                kind = "instance"
                identity_value = str(identity)
                identity_label = str(identity)

            sorted_entries = sorted(
                entries, key=lambda e: (e["tile_order"], e["local_id"], e["crop_cell"])
            )
            for entry in sorted_entries:
                identities_by_tile.setdefault(entry["tile_id"], set()).add(identity)

            candidates.append(
                {
                    "kind": kind,
                    "identity": identity_label,
                    "global_instance_id": identity_value,
                    "observations": sorted_entries,
                }
            )

        candidates.sort(
            key=lambda c: (0 if c["kind"] == "background" else 1, c["identity"])
        )
        within_tile_disagreement = any(len(ids) > 1 for ids in identities_by_tile.values())

        conflicts.append(
            {
                "x": int(x),
                "y": int(y),
                "observation_count": int(coverage_count[y, x]),
                "candidate_identities": [c["identity"] for c in candidates],
                "candidates": candidates,
                "within_tile_disagreement": bool(within_tile_disagreement),
                "state": STATE_NAMES[STATE_CONFLICT],
            }
        )

    state_counts = {
        name: int(np.count_nonzero(state == code))
        for code, name in sorted(STATE_NAMES.items())
    }

    unique_counts, count_frequencies = np.unique(coverage_count, return_counts=True)
    coverage_histogram = {
        str(int(value)): int(freq)
        for value, freq in zip(unique_counts.tolist(), count_frequencies.tolist())
    }
    covered_pixels = int(np.count_nonzero(coverage_binary))
    observation_count_total = int(coverage_count.sum())

    # ---- report ------------------------------------------------------------
    tile_reports = []
    for spec in plan.tiles:
        tile_reports.append(
            {
                "tile_id": spec.tile_id,
                "crop": list(spec.crop),
                "scale": spec.scale,
                "horizontal_flip": spec.horizontal_flip,
                "expected_transformed_size": list(spec.expected_size)
                if spec.expected_size
                else None,
                "image_size": list(spec.image_size) if spec.image_size else None,
                "mask_size": list(spec.mask_size) if spec.mask_size else None,
                "valid_size": list(spec.valid_size) if spec.valid_size else None,
                "crop_within_original": bool(
                    0 <= spec.crop[0]
                    and 0 <= spec.crop[1]
                    and spec.crop[0] + spec.crop[2] <= width
                    and spec.crop[1] + spec.crop[3] <= height
                ),
                "image_matches_transformed_crop": spec.image_matches_transform
                if verify_image_pixels
                else None,
                "instance_map": {str(k): v for k, v in sorted(spec.instance_map.items())},
            }
        )

    empty_instances = [
        {
            "instance_id": row["instance_id"],
            "reason": row["empty_reason"],
            "referenced_by": row["referenced_by"],
        }
        for row in instance_table
        if row["area"] == 0
    ]

    report = {
        "ok": True,
        "image": {
            "image_id": plan.image_id,
            "file_name": plan.original_path.name,
            "path": str(plan.original_path),
            "width": width,
            "height": height,
        },
        "transform_order": plan.transform_order,
        "validation": {
            "original_size": [width, height],
            "width_height_match_original": True,
            "tiles": tile_reports,
        },
        "summary": {
            "pixels_total": int(height * width),
            "covered_pixels": covered_pixels,
            "uncovered_pixels": int(height * width - covered_pixels),
            "observation_count_total": observation_count_total,
            "state_counts": state_counts,
            "conflict_pixels": state_counts["conflict"],
            "instance_count": len(plan.instances),
            "empty_instances": empty_instances,
        },
        "coverage": {
            "min_observations": int(coverage_count.min()) if coverage_count.size else 0,
            "max_observations": int(coverage_count.max()) if coverage_count.size else 0,
            "histogram": coverage_histogram,
        },
        "conflicts": conflicts,
    }

    # Deduplicate categories: several instances may share one category id.
    category_ids: list[int] = []
    for _, cid in plan.instances:
        if cid not in category_ids:
            category_ids.append(int(cid))
    coco = {
        "info": {
            "description": "Tile annotations assembled into original-image coordinates",
            "transform_order": plan.transform_order,
        },
        "images": [
            {
                "id": plan.image_id,
                "file_name": plan.original_path.name,
                "width": width,
                "height": height,
            }
        ],
        "categories": [
            {"id": cid, "name": f"category_{cid}"} for cid in category_ids
        ],
        "annotations": annotations,
    }

    return Assembly(
        plan=plan,
        state=state,
        coverage_binary=coverage_binary,
        coverage_count=coverage_count,
        instance_masks=instance_masks,
        label_indices=label_indices,
        instance_labels=instance_labels,
        observation_details=observation_details,
        instance_table=instance_table,
        coco=coco,
        report=report,
    )


# ---------------------------------------------------------------------------
# Output writing
# ---------------------------------------------------------------------------


def write_outputs(assembly: Assembly, output_dir: str | os.PathLike[str]) -> dict[str, str]:
    out = Path(output_dir)
    instances_dir = out / "instances"
    instances_dir.mkdir(parents=True, exist_ok=True)

    written: dict[str, str] = {}

    state_path = out / "state.png"
    Image.fromarray(assembly.state, mode="L").save(state_path)
    written["state"] = str(state_path)

    coverage_path = out / "coverage.png"
    Image.fromarray(assembly.coverage_binary, mode="L").save(coverage_path)
    written["coverage"] = str(coverage_path)

    count_path = out / "coverage_count.png"
    if assembly.coverage_count.size and int(assembly.coverage_count.max()) > 65535:
        # PNG mode I keeps the exact 32-bit value.
        Image.fromarray(assembly.coverage_count.astype(np.int32), mode="I").save(count_path)
    else:
        Image.fromarray(assembly.coverage_count.astype(np.uint16)).save(count_path)
    written["coverage_count_png"] = str(count_path)

    count_npy_path = out / "coverage_count.npy"
    np.save(count_npy_path, assembly.coverage_count)
    written["coverage_count_npy"] = str(count_npy_path)

    labels_path = out / "instance_labels.png"
    if assembly.instance_labels.dtype == np.uint16:
        Image.fromarray(assembly.instance_labels).save(labels_path)
    else:
        Image.fromarray(assembly.instance_labels.astype(np.int32), mode="I").save(labels_path)
    written["instance_labels"] = str(labels_path)

    for row in assembly.instance_table:
        iid = row["instance_id"]
        mask = assembly.instance_masks[iid].astype(np.uint8)
        path = out / row["mask_file"]
        path.parent.mkdir(parents=True, exist_ok=True)
        Image.fromarray(mask, mode="L").save(path)
        written[f"mask:{iid}"] = str(path)

    instances_path = out / "instances.json"
    instances_payload = {
        "image": {
            "image_id": assembly.plan.image_id,
            "file_name": assembly.plan.original_path.name,
            "width": assembly.plan.width,
            "height": assembly.plan.height,
        },
        "transform_order": assembly.plan.transform_order,
        "categories": assembly.coco["categories"],
        "annotations": assembly.coco["annotations"],
        "instances": assembly.instance_table,
        "label_indices": {
            iid: int(idx) for iid, idx in assembly.label_indices.items()
        },
    }
    instances_path.write_text(
        json.dumps(instances_payload, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    written["instances_json"] = str(instances_path)

    csv_path = out / "instances.csv"
    with csv_path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(
            [
                "instance_id",
                "category_id",
                "label_index",
                "area",
                "bbox_x",
                "bbox_y",
                "bbox_w",
                "bbox_h",
                "state",
                "empty_reason",
                "certain_pixels",
                "observed_valid_pixel_contributions",
                "conflict_pixels_mentioning_instance",
                "mask_file",
                "rle_counts",
            ]
        )
        for row in assembly.instance_table:
            bbox = row["bbox"]
            writer.writerow(
                [
                    row["instance_id"],
                    row["category_id"],
                    row["label_index"],
                    row["area"],
                    "" if bbox is None else bbox[0],
                    "" if bbox is None else bbox[1],
                    "" if bbox is None else bbox[2],
                    "" if bbox is None else bbox[3],
                    row["state"],
                    row["empty_reason"] or "",
                    row["certain_pixels"],
                    row["observed_valid_pixel_contributions"],
                    row["conflict_pixels_mentioning_instance"],
                    row["mask_file"],
                    json.dumps(row["segmentation"]["counts"], separators=(",", ":")),
                ]
            )
    written["instances_csv"] = str(csv_path)

    report_path = out / "report.json"
    assembly.report["outputs"] = written
    report_path.write_text(
        json.dumps(assembly.report, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    written["report_json"] = str(report_path)

    return written


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Assemble tile annotations into original-image coordinates. "
            "Supports only integer crop, nearest-neighbour integer upscale and "
            "optional horizontal flip."
        )
    )
    parser.add_argument("plan", help="path to plan.json")
    parser.add_argument("out", help="output directory")
    parser.add_argument(
        "--no-image-pixel-check",
        action="store_true",
        help="skip the informational check that tile images equal the transformed original crop",
    )
    parser.add_argument(
        "--strict-image-pixels",
        action="store_true",
        help="reject the batch if tile image pixels differ from the transformed original crop",
    )
    parser.add_argument(
        "--quiet",
        action="store_true",
        help="do not print the JSON summary on success",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_arg_parser().parse_args(argv)
    try:
        assembly = build_assembly(
            args.plan,
            strict_image_pixels=args.strict_image_pixels,
            verify_image_pixels=not args.no_image_pixel_check,
        )
        written = write_outputs(assembly, args.out)
    except AssemblyError as exc:
        payload = {"ok": False, "error": "assembly_rejected", "errors": exc.errors}
        print(json.dumps(payload, indent=2, ensure_ascii=False), file=sys.stderr)
        return 2
    except Exception as exc:  # noqa: BLE001
        payload = {
            "ok": False,
            "error": "assembly_failed",
            "message": str(exc),
            "exception": type(exc).__name__,
        }
        print(json.dumps(payload, indent=2, ensure_ascii=False), file=sys.stderr)
        return 1

    if not args.quiet:
        summary = {
            "ok": True,
            "image": assembly.report["image"],
            "summary": assembly.report["summary"],
            "conflicts": len(assembly.report["conflicts"]),
            "outputs": written,
        }
        print(json.dumps(summary, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
