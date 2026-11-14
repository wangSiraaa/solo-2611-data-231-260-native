#!/usr/bin/env python3
"""Independent verification of assemble.py using explicit pixel enumeration.

This script intentionally avoids importing the assembler's transform helpers
(or uses them only to decode output RLE).  Every original pixel is enumerated
from scratch: for each source cell it walks every transformed tile pixel
through the inverse of ``crop -> upscale -> flip`` and records the observed
identities.  The resulting state / instance masks / area / bbox are then
compared with the program's output files, and every emitted RLE is decoded
back to a mask and compared cell-by-cell.

Usage:
    python3 verify_assembly.py fixtures/plan.json /tmp/out-a
"""

from __future__ import annotations

import json
import sys
from collections import Counter
from pathlib import Path

import numpy as np
from PIL import Image


def load_l(path: Path) -> np.ndarray:
    return np.asarray(Image.open(path).convert("L"))


def load_rgb(path: Path) -> np.ndarray:
    return np.asarray(Image.open(path).convert("RGB"))


def independent_expectations(plan_path: Path):
    plan = json.loads(plan_path.read_text())
    base = plan_path.parent
    h, w = int(plan["height"]), int(plan["width"])

    # observations[y][x] is a set of ("background") or ("instance", iid)
    obs = [[set() for _ in range(w)] for _ in range(h)]

    for tile in plan["tiles"]:
        x0, y0, cw, ch = [int(v) for v in tile["crop"]]
        scale = int(tile["scale"])
        flip = bool(tile["horizontal_flip"])
        label = load_l(base / tile["mask"])
        valid = load_l(base / tile["valid"])
        assert label.shape == (ch * scale, cw * scale), (tile["tile_id"], label.shape)
        assert valid.shape == label.shape
        mapping = {int(k): v for k, v in tile["instance_map"].items()}

        # Forward transform check on the image, if present.
        original = load_rgb(base / plan["original"])
        patch = original[y0 : y0 + ch, x0 : x0 + cw]
        expected = np.repeat(np.repeat(patch, scale, axis=0), scale, axis=1)
        if flip:
            expected = expected[:, ::-1]
        tile_image = load_rgb(base / tile["image"])
        assert tile_image.shape == expected.shape
        assert np.array_equal(tile_image, expected), tile["tile_id"]

        for y in range(h):
            for x in range(w):
                # Enumerate transformed pixels and invert manually.
                for ty in range(label.shape[0]):
                    for tx in range(label.shape[1]):
                        if valid[ty, tx] != 1:
                            continue
                        # invert horizontal flip
                        ux = label.shape[1] - 1 - tx if flip else tx
                        # invert nearest upscale
                        col = ux // scale
                        row = ty // scale
                        ox, oy = x0 + col, y0 + row
                        if (ox, oy) != (x, y):
                            continue
                        raw = int(label[ty, tx])
                        if raw == 0:
                            obs[y][x].add("background")
                        else:
                            assert raw in mapping, (tile["tile_id"], raw)
                            obs[y][x].add(mapping[raw])

    states = np.zeros((h, w), dtype=np.uint8)
    masks = {}
    for y in range(h):
        for x in range(w):
            s = obs[y][x]
            if not s:
                states[y, x] = 0
            elif len(s) == 1:
                only = next(iter(s))
                if only == "background":
                    states[y, x] = 1
                else:
                    states[y, x] = 2
                    masks.setdefault(only, np.zeros((h, w), dtype=bool))[y, x] = True
            else:
                states[y, x] = 3
    for iid in [i["id"] for i in plan["instances"]]:
        masks.setdefault(iid, np.zeros((h, w), dtype=bool))

    conflicts = [
        {"x": x, "y": y, "candidates": sorted(obs[y][x])}
        for y in range(h)
        for x in range(w)
        if states[y, x] == 3
    ]
    return plan, states, masks, conflicts, obs


def decode_rle(counts, h, w):
    flat = np.zeros(h * w, dtype=np.uint8)
    pos = 0
    val = 0
    for c in counts:
        if val:
            flat[pos : pos + c] = 1
        pos += c
        val ^= 1
    assert pos == h * w
    return flat.reshape((w, h)).T.astype(bool)


def bbox(mask):
    rows, cols = np.where(mask)
    if len(rows) == 0:
        return None
    return [int(cols.min()), int(rows.min()),
            int(cols.max() - cols.min() + 1), int(rows.max() - rows.min() + 1)]


def main(plan_path: str, out_dir: str) -> int:
    plan_path = Path(plan_path)
    out = Path(out_dir)
    plan, exp_state, exp_masks, exp_conflicts, obs = independent_expectations(plan_path)
    h, w = exp_state.shape

    failures = []

    got_state = load_l(out / "state.png")
    if not np.array_equal(got_state, exp_state):
        failures.append(("state", got_state, exp_state))

    got_cov = load_l(out / "coverage.png") > 0
    expected_cov = np.zeros((h, w), bool)
    for y in range(h):
        for x in range(w):
            expected_cov[y, x] = bool(obs[y][x])
    if not np.array_equal(got_cov, expected_cov):
        failures.append(("coverage", got_cov, expected_cov))

    got_count = np.load(out / "coverage_count.npy")
    exp_count = np.zeros((h, w), dtype=got_count.dtype)
    # Recount with a direct tile-pixel enumeration.
    for tile in plan["tiles"]:
        x0, y0, cw, ch = [int(v) for v in tile["crop"]]
        scale = int(tile["scale"])
        flip = bool(tile["horizontal_flip"])
        valid = load_l(plan_path.parent / tile["valid"])
        for ty in range(valid.shape[0]):
            for tx in range(valid.shape[1]):
                if valid[ty, tx] != 1:
                    continue
                ux = valid.shape[1] - 1 - tx if flip else tx
                exp_count[y0 + ty // scale, x0 + ux // scale] += 1
    if not np.array_equal(got_count, exp_count):
        failures.append(("coverage_count", got_count, exp_count))

    got_labels = np.asarray(Image.open(out / "instance_labels.png"))
    exp_labels = np.zeros((h, w), dtype=got_labels.dtype)
    order = {inst["id"]: idx + 1 for idx, inst in enumerate(plan["instances"])}
    for iid, mask in exp_masks.items():
        exp_labels[mask] = order[iid]
    if not np.array_equal(got_labels, exp_labels):
        failures.append(("instance_labels", got_labels, exp_labels))

    # Per-instance masks, area, bbox, and decoded RLE.
    payload = json.loads((out / "instances.json").read_text())
    mask_file_by_instance = {
        row["instance_id"]: row["mask_file"] for row in payload["instances"]
    }
    for ann in payload["annotations"]:
        iid = ann["instance_id"]
        exp = exp_masks[iid]
        got_mask = load_l(out / "instances" / Path(mask_file_by_instance[iid]).name)
        if not np.array_equal(got_mask > 0, exp):
            failures.append((f"mask:{iid}", got_mask > 0, exp))
        exp_area = int(exp.sum())
        if ann["area"] != exp_area:
            failures.append((f"area:{iid}", ann["area"], exp_area))
        if bbox(exp) != ann["bbox"]:
            failures.append((f"bbox:{iid}", ann["bbox"], bbox(exp)))
        decoded = decode_rle(ann["segmentation"]["counts"], h, w)
        if not np.array_equal(decoded, exp):
            failures.append((f"rle:{iid}", decoded, exp))
        # counts must start with a background run and sum to h*w.
        if sum(ann["segmentation"]["counts"]) != h * w:
            failures.append((f"rle_sum:{iid}", sum(ann["segmentation"]["counts"]), h * w))
        if ann["segmentation"]["size"] != [h, w]:
            failures.append((f"rle_size:{iid}", ann["segmentation"]["size"], [h, w]))

    # Conflict report: coordinates and candidate sets must match exactly.
    report = json.loads((out / "report.json").read_text())
    got_conflicts = [
        {"x": c["x"], "y": c["y"], "candidates": sorted(
            ("background" if cand["kind"] == "background" else cand["identity"])
            for cand in c["candidates"]
        )}
        for c in report["conflicts"]
    ]
    if got_conflicts != exp_conflicts:
        failures.append(("conflicts", got_conflicts, exp_conflicts))

    # Every instance must appear in the table, even when empty.
    table_ids = {row["instance_id"] for row in payload["instances"]}
    assert table_ids == {i["id"] for i in plan["instances"]}, table_ids

    if failures:
        print("MISMATCHES:")
        for name, got, exp in failures:
            print(f"  {name}\n    got={got!r}\n    exp={exp!r}")
        return 1

    print(
        f"OK: {h}x{w} pixels, state/coverage/count/labels/masks/RLE/bbox/conflicts "
        f"all match independent enumeration ({len(exp_conflicts)} conflict(s), "
        f"{len(plan['instances'])} instance(s))."
    )
    return 0


if __name__ == "__main__":
    if len(sys.argv) != 3:
        print(__doc__)
        raise SystemExit(64)
    raise SystemExit(main(sys.argv[1], sys.argv[2]))
