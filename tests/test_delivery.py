"""End-to-end tests for the delivery builder and verifier.

Run with::

    python -m unittest discover -s tests -v

The tests create their own synthetic fixtures in a temporary directory so that
they do not mutate the checked-in ones, and they exercise the same public
entry points (``python -m hdf5_delivery`` and ``verify_delivery.py``) an
operator would use.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

import h5py
import numpy as np

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

from hdf5_delivery import DeliveryError, build_delivery  # noqa: E402


def make_external_tree(directory: str) -> str:
    """Create a fixture exercising hard-link dedupe, soft links and aliases."""

    ext_a = os.path.join(directory, "ext-a.h5")
    with h5py.File(ext_a, "w") as f:
        group = f.create_group("shared")
        group.attrs["unit"] = "count"
        group.create_dataset("values", data=np.array([7, 8, 9], dtype=np.int64))
        f.id.links.create_hard(b"alias", f.id, b"/shared")

    part_a = os.path.join(directory, "part-a.h5")
    with h5py.File(part_a, "w") as f:
        f["part"] = np.array([10, 11, 12], dtype=np.int64)
    part_b = os.path.join(directory, "part-b.h5")
    with h5py.File(part_b, "w") as f:
        f["part"] = np.array([20, 21], dtype=np.int64)

    root = os.path.join(directory, "root.h5")
    with h5py.File(root, "w") as f:
        f.attrs["purpose"] = "synthetic transfer"
        f["empty"] = np.array([], dtype=np.int64)
        f["external_one"] = h5py.ExternalLink("ext-a.h5", "/shared")
        f["external_two"] = h5py.ExternalLink("ext-a.h5", "/alias")
        local = f.create_group("local")
        local["values"] = np.array([1, 2, 3], dtype=np.int64)
        local.id.links.create_hard(b"self", f.id, b"/local")
        f.id.links.create_hard(b"local_alias", f.id, b"/local")
        f["soft_values"] = h5py.SoftLink("/local/values")
        layout = h5py.VirtualLayout(shape=(7,), dtype="i8")
        layout[0:3] = h5py.VirtualSource("part-a.h5", "part", shape=(3,))
        layout[4:6] = h5py.VirtualSource("part-b.h5", "part", shape=(2,))
        f.create_virtual_dataset("virtual", layout, fillvalue=-99)
    return root


def make_dangling(directory: str) -> str:
    path = os.path.join(directory, "dangling.h5")
    with h5py.File(path, "w") as f:
        f["missing"] = h5py.ExternalLink("absent.h5", "/x")
    return path


def make_soft_cycle(directory: str) -> str:
    path = os.path.join(directory, "soft-cycle.h5")
    with h5py.File(path, "w") as f:
        f["a"] = h5py.SoftLink("/b")
        f["b"] = h5py.SoftLink("/a")
    return path


def make_missing_vds(directory: str) -> str:
    path = os.path.join(directory, "missing-vds.h5")
    with h5py.File(path, "w") as f:
        layout = h5py.VirtualLayout(shape=(2,), dtype="i8")
        layout[:] = h5py.VirtualSource("absent.h5", "x", shape=(2,))
        f.create_virtual_dataset("v", layout, fillvalue=-5)
    return path


class BuildTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix="hdf5-test-")
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def build(self, input_path: str, name: str = "out") -> tuple[str, str]:
        out = os.path.join(self.tmp, f"{name}.h5")
        man = os.path.join(self.tmp, f"{name}.json")
        manifest = build_delivery(input_path, out, man)
        self.assertTrue(os.path.isfile(out))
        with open(man) as fh:
            self.assertEqual(json.load(fh), manifest)
        return out, man

    def test_delivery_is_self_contained_and_shared(self) -> None:
        root = make_external_tree(self.tmp)
        out, _ = self.build(root)
        with h5py.File(out, "r") as f:
            self.assertEqual(sorted(f.keys()), [
                "empty", "external_one", "external_two", "local",
                "local_alias", "soft_values", "virtual",
            ])
            addrs = {
                name: h5py.h5o.get_info(f[name].id).addr
                for name in f.keys()
            }
            self.assertEqual(addrs["external_one"], addrs["external_two"])
            self.assertEqual(addrs["local"], addrs["local_alias"])
            self.assertEqual(addrs["local"], h5py.h5o.get_info(f["local/self"].id).addr)
            self.assertEqual(addrs["soft_values"], h5py.h5o.get_info(f["local/values"].id).addr)
            self.assertFalse(f["virtual"].is_virtual)
            self.assertEqual(f["empty"].shape, (0,))
            np.testing.assert_array_equal(
                f["virtual"][()], np.array([10, 11, 12, -99, 20, 21, -99])
            )
            for name in f:
                self.assertIs(type(f.get(name, getlink=True)), h5py.HardLink)

    def test_output_survives_input_removal(self) -> None:
        root = make_external_tree(self.tmp)
        inputs = os.path.join(self.tmp, "inputs")
        os.makedirs(inputs, exist_ok=False)
        for name in os.listdir(self.tmp):
            if name.endswith(".h5"):
                shutil.move(os.path.join(self.tmp, name), os.path.join(inputs, name))
        root = os.path.join(inputs, "root.h5")
        out, _ = self.build(root)
        shutil.rmtree(inputs)
        with h5py.File(out, "r") as f:
            np.testing.assert_array_equal(f["virtual"][()], np.array([10, 11, 12, -99, 20, 21, -99]))
            np.testing.assert_array_equal(f["external_one/values"][()], np.array([7, 8, 9]))

    def test_failure_inputs_are_rejected_and_leave_no_output(self) -> None:
        for maker in (make_dangling, make_soft_cycle, make_missing_vds):
            with self.subTest(maker=maker.__name__):
                source = maker(self.tmp)
                out = os.path.join(self.tmp, f"reject-{maker.__name__}.h5")
                man = os.path.join(self.tmp, f"reject-{maker.__name__}.json")
                with self.assertRaises(DeliveryError) as ctx:
                    build_delivery(source, out, man)
                message = str(ctx.exception)
                self.assertTrue(
                    os.path.basename(source).replace(".h5", "") in message
                    or ".h5" in message,
                    message,
                )
                self.assertFalse(os.path.exists(out))
                self.assertFalse(os.path.exists(man))

    def test_unknown_link_target_keeps_source_context(self) -> None:
        source = os.path.join(self.tmp, "nested-soft.h5")
        with h5py.File(source, "w") as f:
            f["a"] = h5py.SoftLink("/b")
            f["b"] = h5py.SoftLink("/a")
        with self.assertRaises(DeliveryError) as ctx:
            build_delivery(source, os.path.join(self.tmp, "x.h5"))
        self.assertIn(source, str(ctx.exception))


class VerifyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix="hdf5-verify-test-")
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def test_verifier_passes_for_fixture_copy(self) -> None:
        inputs = os.path.join(self.tmp, "fixtures")
        shutil.copytree(os.path.join(REPO, "fixtures"), inputs)
        result = subprocess.run(
            [sys.executable, os.path.join(REPO, "verify_delivery.py"),
             "--input", os.path.join(inputs, "root.h5")],
            cwd=REPO,
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("PASS", result.stdout)


if __name__ == "__main__":
    unittest.main()
