#!/usr/bin/env python3
"""Synthetic regression cases for assemble.py.

These cases exercise the behaviours the task calls out: overlapping tiles,
flips, integer upscale, incomplete valid masks, background/instance conflicts,
unmapped labels and empty instances.  Each geometry case is cross-checked with
verify_assembly.py, whose expectations come from an independent brute-force
enumeration rather than from the assembler's own inverse mapping.
"""

from __future__ import annotations

import json
import shutil
import tempfile
from pathlib import Path

import numpy as np
from PIL import Image

import assemble
import verify_assembly

FIXTURES = Path(__file__).resolve().parent / "fixtures"


def write_png(path: Path, arr: np.ndarray) -> None:
    Image.fromarray(arr).save(path)


def make_case(
    root: Path,
    name: str,
    height: int,
    width: int,
    tiles: list[dict],
    instances: list[dict] | None = None,
) -> Path:
    """Create a self-consistent case directory and return its plan path."""

    d = root / name
    d.mkdir(parents=True)

    original = np.zeros((height, width, 3), dtype=np.uint8)
    for y in range(height):
        for x in range(width):
            original[y, x] = (x * 17 + 3, y * 19 + 5, (x + y) % 256)
    write_png(d / "original.png", original)

    instances = instances or [
        {"id": "obj-A", "category_id": 1},
        {"id": "obj-B", "category_id": 2},
    ]
    tile_entries = []
    for i, t in enumerate(tiles):
        x0, y0, cw, ch = t["crop"]
        scale = t["scale"]
        flip = t["horizontal_flip"]
        patch = original[y0 : y0 + ch, x0 : x0 + cw]
        up = np.repeat(np.repeat(patch, scale, axis=0), scale, axis=1)
        if flip:
            up = up[:, ::-1]

        image_name = f"t{i}-image.png"
        mask_name = f"t{i}-mask.png"
        valid_name = f"t{i}-valid.png"
        write_png(d / image_name, up)

        mask = t["mask"]
        valid = t["valid"]
        assert mask.shape == (ch * scale, cw * scale), (name, t["tile_id"], mask.shape)
        assert valid.shape == mask.shape
        write_png(d / mask_name, mask)
        write_png(d / valid_name, valid)

        tile_entries.append(
            {
                "tile_id": t["tile_id"],
                "crop": list(t["crop"]),
                "scale": scale,
                "horizontal_flip": flip,
                "image": image_name,
                "mask": mask_name,
                "valid": valid_name,
                "instance_map": t["instance_map"],
            }
        )

    plan = {
        "image_id": name,
        "original": "original.png",
        "width": width,
        "height": height,
        "tiles": tile_entries,
        "instances": instances,
    }
    plan_path = d / "plan.json"
    plan_path.write_text(json.dumps(plan, indent=2))
    return plan_path


def run_case(plan_path: Path, out_root: Path):
    out = out_root / plan_path.parent.name
    assembly = assemble.build_assembly(plan_path)
    assemble.write_outputs(assembly, out)
    rc = verify_assembly.main(str(plan_path), str(out))
    assert rc == 0, f"independent verification failed for {plan_path}"
    return out


def expect_rejected(plan_path: Path, code: str) -> dict:
    try:
        assemble.build_assembly(plan_path)
    except assemble.AssemblyError as exc:
        assert any(e["code"] == code for e in exc.errors), exc.errors
        return exc.errors[0]
    raise AssertionError(f"expected rejection with {code} for {plan_path}")


def main() -> int:
    work = Path(tempfile.mkdtemp(prefix="assemble-regression-"))
    try:
        # 1. Fixture plan, as shipped.
        out = work / "fixtures-case"
        out.mkdir()
        assembly = assemble.build_assembly(FIXTURES / "plan.json")
        assemble.write_outputs(assembly, out)
        assert verify_assembly.main(str(FIXTURES / "plan.json"), str(out)) == 0
        report = json.loads((out / "report.json").read_text())
        assert report["summary"]["state_counts"] == {
            "unknown": 28,
            "certain_background": 35,
            "certain_instance": 16,
            "conflict": 1,
        }, report["summary"]["state_counts"]
        conflicts = report["conflicts"]
        assert len(conflicts) == 1 and (conflicts[0]["x"], conflicts[0]["y"]) == (4, 2)
        assert set(conflicts[0]["candidate_identities"]) == {"background", "obj-A"}
        print("case 1: fixture plan OK")

        # 2. bad-mapping must reject the whole batch and write nothing.
        bad_out = work / "bad-mapping-out"
        try:
            assemble.build_assembly(FIXTURES / "bad-mapping.json")
        except assemble.AssemblyError as exc:
            assert {(e["code"], e.get("tile_id"), e.get("local_id")) for e in exc.errors} == {
                ("unmapped_local_label", "b", 7)
            }, exc.errors
        else:
            raise AssertionError("bad-mapping.json was accepted")
        assert not bad_out.exists()
        print("case 2: bad mapping rejected OK")

        # 3. Upscale + flip, incomplete valid, conflict within one tile
        #    (background vs instance), an unobserved cell and an unreferenced
        #    empty instance.
        #
        #    Tile crop is (0, 0, 2, 2) at scale 2, flipped, so the inverse map
        #    sends transformed columns {2,3} -> original x=0 and {0,1} -> x=1,
        #    and transformed rows {0,1} -> y=0, {2,3} -> y=1.
        h, w = 3, 3
        mask_a = np.array(
            [
                [0, 1, 1, 1],
                [0, 1, 1, 1],
                [0, 0, 0, 0],
                [0, 0, 0, 0],
            ],
            dtype=np.uint8,
        )
        valid_a = np.array(
            [
                [1, 1, 1, 1],
                [1, 1, 1, 1],
                [0, 0, 1, 1],
                [0, 0, 1, 1],
            ],
            dtype=np.uint8,
        )
        plan = make_case(
            work,
            "flip-upscale-conflict",
            h,
            w,
            [
                {
                    "tile_id": "x",
                    "crop": (0, 0, 2, 2),
                    "scale": 2,
                    "horizontal_flip": True,
                    "mask": mask_a,
                    "valid": valid_a,
                    "instance_map": {"1": "obj-A"},
                }
            ],
        )
        out = run_case(plan, work)
        state = np.asarray(Image.open(out / "state.png"))
        expected_state = np.array(
            [
                [2, 3, 0],
                [1, 0, 0],
                [0, 0, 0],
            ],
            dtype=np.uint8,
        )
        assert np.array_equal(state, expected_state), state
        payload = json.loads((out / "instances.json").read_text())
        rows = {r["instance_id"]: r for r in payload["instances"]}
        assert rows["obj-B"]["area"] == 0
        assert rows["obj-B"]["bbox"] is None
        assert rows["obj-B"]["empty_reason"] == "not_referenced_by_any_tile_instance_map"
        assert rows["obj-A"]["area"] == 1
        assert rows["obj-A"]["bbox"] == [0, 0, 1, 1]
        print("case 3: flip/upscale/incomplete valid/empty instance OK")

        # 4. Overlap with different local ids mapping to the same instance is
        #    certain, while a third tile's local id to another instance conflicts.
        h, w = 2, 4
        m1 = np.array([[1, 1, 0, 0]], dtype=np.uint8)  # crop (0,0,4,1)
        v1 = np.ones_like(m1)
        m2 = np.array([[9, 9, 9, 9]], dtype=np.uint8)  # crop (0,0,4,1)
        v2 = np.ones_like(m2)
        m3 = np.zeros((1, 2), dtype=np.uint8)
        m3[0, 1] = 5  # crop (2,0,2,1): conflicts with obj-A at x=3
        v3 = np.ones_like(m3)
        plan = make_case(
            work,
            "overlap-same-instance",
            h,
            w,
            [
                {
                    "tile_id": "p",
                    "crop": (0, 0, 4, 1),
                    "scale": 1,
                    "horizontal_flip": False,
                    "mask": m1,
                    "valid": v1,
                    "instance_map": {"1": "obj-A"},
                },
                {
                    "tile_id": "q",
                    "crop": (0, 0, 4, 1),
                    "scale": 1,
                    "horizontal_flip": False,
                    "mask": m2,
                    "valid": v2,
                    "instance_map": {"9": "obj-A"},
                },
                {
                    "tile_id": "r",
                    "crop": (2, 0, 2, 1),
                    "scale": 1,
                    "horizontal_flip": False,
                    "mask": m3,
                    "valid": v3,
                    "instance_map": {"5": "obj-B"},
                },
                {
                    "tile_id": "s",
                    "crop": (0, 1, 4, 1),
                    "scale": 1,
                    "horizontal_flip": False,
                    "mask": np.zeros((1, 4), np.uint8),
                    "valid": np.ones((1, 4), np.uint8),
                    "instance_map": {},
                },
            ],
        )
        out = run_case(plan, work)
        state = np.asarray(Image.open(out / "state.png"))
        assert list(state[0]) == [2, 2, 3, 3], state
        assert list(state[1]) == [1, 1, 1, 1], state
        report = json.loads((out / "report.json").read_text())
        cell = [c for c in report["conflicts"] if (c["x"], c["y"]) == (3, 0)][0]
        assert set(cell["candidate_identities"]) == {"obj-A", "obj-B", "background"}
        assert cell["observation_count"] == 3
        bg_cell = [c for c in report["conflicts"] if (c["x"], c["y"]) == (2, 0)][0]
        assert set(bg_cell["candidate_identities"]) == {"background", "obj-A"}
        print("case 4: overlap same-instance vs cross-instance conflict OK")

        # 5. Whole-cell disagreement inside a single scale-3 tile must be kept.
        m = np.array([[0, 0, 1]], dtype=np.uint8)  # one row, scale 3 -> one cell
        v = np.array([[1, 1, 1]], dtype=np.uint8)
        plan = make_case(
            work,
            "intra-cell-conflict",
            1,
            1,
            [
                {
                    "tile_id": "u",
                    "crop": (0, 0, 1, 1),
                    "scale": 3,
                    "horizontal_flip": False,
                    "mask": np.repeat(m, 3, axis=0),
                    "valid": np.repeat(v, 3, axis=0),
                    "instance_map": {"1": "obj-A"},
                }
            ],
        )
        out = run_case(plan, work)
        state = np.asarray(Image.open(out / "state.png"))
        assert state[0, 0] == assemble.STATE_CONFLICT
        report = json.loads((out / "report.json").read_text())
        assert report["conflicts"][0]["within_tile_disagreement"] is True
        assert report["summary"]["observation_count_total"] == 9
        # Both candidates' per-pixel values must be retained.
        cands = {c["identity"]: c for c in report["conflicts"][0]["candidates"]}
        assert cands["background"]["observations"][0]["valid_pixel_count"] == 6
        assert cands["obj-A"]["observations"][0]["valid_pixel_count"] == 3
        print("case 5: intra-cell disagreement retained OK")

        # 6. Invalid pixels contribute nothing: fully invalid tile -> unknown.
        plan = make_case(
            work,
            "all-invalid",
            1,
            1,
            [
                {
                    "tile_id": "z",
                    "crop": (0, 0, 1, 1),
                    "scale": 1,
                    "horizontal_flip": False,
                    "mask": np.zeros((1, 1), np.uint8),
                    "valid": np.zeros((1, 1), np.uint8),
                    "instance_map": {},
                }
            ],
        )
        out = run_case(plan, work)
        state = np.asarray(Image.open(out / "state.png"))
        assert state[0, 0] == assemble.STATE_UNKNOWN
        print("case 6: all-invalid tile leaves unknown OK")

        # 7. Rejection rules: declared category contradiction and undeclared
        #    global instance.  Use copies of the fixture plan.
        contradictory = json.loads((FIXTURES / "plan.json").read_text())
        contradictory["instances"].append({"id": "obj-A", "category_id": 2})
        p = work / "contradictory" / "plan.json"
        p.parent.mkdir(parents=True)
        (p.parent / "original.png").write_bytes((FIXTURES / "original.png").read_bytes())
        for name in ("a-image.png", "a-mask.png", "a-valid.png",
                     "b-image.png", "b-mask.png", "b-valid.png"):
            (p.parent / name).write_bytes((FIXTURES / name).read_bytes())
        p.write_text(json.dumps(contradictory))
        expect_rejected(p, "conflicting_instance_category")

        undeclared = json.loads((FIXTURES / "plan.json").read_text())
        undeclared["tiles"][0]["instance_map"]["1"] = "obj-Z"
        p2 = work / "undeclared" / "plan.json"
        p2.parent.mkdir(parents=True)
        (p2.parent / "original.png").write_bytes((FIXTURES / "original.png").read_bytes())
        for name in ("a-image.png", "a-mask.png", "a-valid.png",
                     "b-image.png", "b-mask.png", "b-valid.png"):
            (p2.parent / name).write_bytes((FIXTURES / name).read_bytes())
        p2.write_text(json.dumps(undeclared))
        expect_rejected(p2, "undeclared_global_instance")
        print("case 7: category contradiction / undeclared instance rejected OK")

        # 8. Size / crop validation.
        bad_size = json.loads((FIXTURES / "plan.json").read_text())
        bad_size["tiles"][0]["crop"] = [0, 0, 7, 6]  # 7*2 != 12
        p3 = work / "bad-size" / "plan.json"
        p3.parent.mkdir(parents=True)
        (p3.parent / "original.png").write_bytes((FIXTURES / "original.png").read_bytes())
        for name in ("a-image.png", "a-mask.png", "a-valid.png",
                     "b-image.png", "b-mask.png", "b-valid.png"):
            (p3.parent / name).write_bytes((FIXTURES / name).read_bytes())
        p3.write_text(json.dumps(bad_size))
        errors = expect_rejected(p3, "tile_image_size_mismatch")
        assert errors["expected"] == [14, 12], errors

        out_of_bounds = json.loads((FIXTURES / "plan.json").read_text())
        out_of_bounds["tiles"][1]["crop"] = [7, 2, 6, 6]
        p4 = work / "out-of-bounds" / "plan.json"
        p4.parent.mkdir(parents=True)
        (p4.parent / "original.png").write_bytes((FIXTURES / "original.png").read_bytes())
        for name in ("a-image.png", "a-mask.png", "a-valid.png",
                     "b-image.png", "b-mask.png", "b-valid.png"):
            (p4.parent / name).write_bytes((FIXTURES / name).read_bytes())
        p4.write_text(json.dumps(out_of_bounds))
        expect_rejected(p4, "crop_out_of_bounds")
        print("case 8: size and crop checks reject OK")

        # 9. RLE format checks: column-major, leading zero run, empty mask.
        m = np.zeros((3, 4), dtype=bool)
        m[0, 0] = True
        m[1, 0] = True
        m[2, 3] = True
        counts = assemble.encode_rle(m)
        assert counts == [0, 2, 9, 1], counts
        assert np.array_equal(assemble.decode_rle(counts, (3, 4)), m)
        # An all-background mask is a single zero run; the empty instance table
        # still carries this RLE (area 0 / bbox null).
        empty = assemble.encode_rle(np.zeros((2, 5), dtype=bool))
        assert empty == [10], empty
        print("case 9: RLE column-major encoding OK")

        print("\nall regression cases passed")
        return 0
    finally:
        shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
