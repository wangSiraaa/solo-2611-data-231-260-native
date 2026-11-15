"""端到端核对测试（仅依赖标准库 unittest + netCDF4/numpy）。

核对原则：测试独立打开源 NetCDF、关闭自动 mask/scale，按索引变量自行
推导每条观测的父键与继承值，再与转换产出逐行比对——不复用转换器内部
解码函数，也不按行号或排序猜测归属。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

import netCDF4
import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from cf_rel import convert_file  # noqa: E402
from cf_rel.errors import ValidationError  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FIXTURES = os.path.join(ROOT, "fixtures")
MAPPING_PATH = os.path.join(ROOT, "mapping.json")

EPOCH = datetime(2026, 1, 1, tzinfo=timezone.utc)


def load_mapping() -> dict:
    with open(MAPPING_PATH, encoding="utf-8") as fh:
        return json.load(fh)


def read_jsonl(path: str) -> list[dict]:
    with open(path, encoding="utf-8") as fh:
        return [json.loads(line) for line in fh if line.strip()]


# ---------------------------------------------------------------------------
# 测试侧独立解码（与转换器实现互不共享）
# ---------------------------------------------------------------------------

def open_raw(path: str) -> netCDF4.Dataset:
    ds = netCDF4.Dataset(path, "r")
    ds.set_auto_mask(False)
    ds.set_auto_scale(False)
    return ds


def attr(var, name):
    return var.getncattr(name) if name in var.ncattrs() else None


def decode_numeric(var) -> list:
    """独立实现：先按存储 dtype 识别填充值，再做 scale/offset。"""
    markers = []
    for a in ("_FillValue", "missing_value"):
        value = attr(var, a)
        if value is not None:
            for m in np.atleast_1d(np.asarray(value)):
                markers.append(np.asarray(m, dtype=var.dtype).item())
    scale = attr(var, "scale_factor")
    offset = attr(var, "add_offset")
    out = []
    for stored in np.asarray(var[:]).reshape(-1).tolist():
        if any(np.array_equal(stored, m) for m in markers):
            out.append(None)
            continue
        value = stored
        if scale is not None:
            value = float(value) * float(scale)
        if offset is not None:
            value = float(value) + float(offset)
        out.append(value)
    return out


def iso_utc(seconds) -> str:
    return (EPOCH + timedelta(seconds=float(seconds))).isoformat().replace(
        "+00:00", "Z")


# ---------------------------------------------------------------------------
# 真实 fixture 核对
# ---------------------------------------------------------------------------

class FixtureConversionTests(unittest.TestCase):
    """对 contiguous / indexed 两个真实文件做独立逐观测核对。"""

    def _check_fixture(self, nc_name: str, layout: str):
        nc_path = os.path.join(FIXTURES, nc_name)
        mapping = load_mapping()
        with tempfile.TemporaryDirectory() as tmp:
            result = convert_file(nc_path, mapping, tmp)
            self.assertEqual(result.layout, layout)
            out_dir = os.path.join(tmp, os.path.splitext(nc_name)[0])
            traj_rows = read_jsonl(os.path.join(out_dir, "trajectory.jsonl"))
            prof_rows = read_jsonl(os.path.join(out_dir, "profile.jsonl"))
            obs_rows = read_jsonl(os.path.join(out_dir, "observation.jsonl"))
            with open(os.path.join(out_dir, "variables.json"),
                      encoding="utf-8") as fh:
                variables_doc = json.load(fh)
            self._assert_semantics(
                nc_path, layout, traj_rows, prof_rows,
                obs_rows, variables_doc)

    def _assert_semantics(self, nc_path, layout,
                          traj_rows, prof_rows, obs_rows, variables_doc):
        with open_raw(nc_path) as ds:
            n_traj = ds.dimensions["trajectory"].size
            n_prof = ds.dimensions["profile"].size
            n_obs = ds.dimensions["obs"].size

            traj_ids = [str(x) for x in ds.variables["trajectory_id"][:]]
            prof_ids = [str(x) for x in ds.variables["profile_id"][:]]
            traj_of_prof = [
                int(x) for x in ds.variables["trajectoryIndex"][:]]

            if layout == "contiguous":
                row_sizes = [int(x) for x in ds.variables["rowSize"][:]]
                # 显式核对 rowSize 语义：非负且总和等于 obs 维度
                self.assertTrue(all(s >= 0 for s in row_sizes))
                self.assertEqual(sum(row_sizes), n_obs)
                prof_of_obs = []
                for p, size in enumerate(row_sizes):
                    prof_of_obs.extend([p] * size)
            else:
                prof_of_obs = [
                    int(x) for x in ds.variables["profileIndex"][:]]

            depth_expect = decode_numeric(ds.variables["depth"])
            quality_expect = decode_numeric(ds.variables["quality"])
            time_expect = [int(x) for x in ds.variables["time"][:]]
            lat_expect = [float(x) for x in ds.variables["latitude"][:]]
            campaign = [str(x) for x in ds.variables["campaign"][:]]
            note = str(np.asarray(ds.variables["station_note"][:]).item())

            # ---- 表规模与零观测剖面 ----
            self.assertEqual(len(traj_rows), n_traj)
            self.assertEqual(len(prof_rows), n_prof)  # P-1 必须保留
            self.assertEqual(len(obs_rows), n_obs)

            # 业务 ID 唯一且顺序保留原始索引
            self.assertEqual(
                [r["trajectory_id"] for r in traj_rows], traj_ids)
            self.assertEqual(
                [r["profile_id"] for r in prof_rows], prof_ids)
            self.assertEqual(
                len(set(traj_ids)), n_traj)
            self.assertEqual(
                len(set(prof_ids)), n_prof)

            # ---- profile 表逐行核对父键（逆序轨迹归属） ----
            for p, row in enumerate(prof_rows):
                self.assertEqual(row["profile_index"], p)
                self.assertEqual(row["profile_id"], prof_ids[p])
                t = traj_of_prof[p]
                self.assertEqual(row["trajectory_index"], t)
                self.assertEqual(row["trajectory_id"], traj_ids[t])
                self.assertEqual(
                    row["variables"]["time"], time_expect[p])
                self.assertEqual(
                    row["variables"]["time_utc"], iso_utc(time_expect[p]))
                self.assertEqual(
                    row["variables"]["latitude"], lat_expect[p])
                if layout == "contiguous":
                    self.assertEqual(row["row_size"], row_sizes[p])
                    self.assertEqual(row["obs_count"], row_sizes[p])

            # 零观测剖面 P-1 必须存在且 obs_count == 0
            empty = [r for r in prof_rows if r["obs_count"] == 0]
            self.assertEqual([r["profile_id"] for r in empty], ["P-1"])

            # ---- observation 表按原 obs 索引逐条独立核对 ----
            self.assertEqual(
                [r["observation_index"] for r in obs_rows],
                list(range(n_obs)))  # 原索引齐全且顺序保留
            for o, row in enumerate(obs_rows):
                p = prof_of_obs[o]       # 测试独立从索引变量推导
                t = traj_of_prof[p]
                self.assertEqual(row["source_file"],
                                 os.path.basename(nc_path))
                self.assertEqual(row["profile_index"], p)
                self.assertEqual(row["profile_id"], prof_ids[p])
                self.assertEqual(row["trajectory_index"], t)
                self.assertEqual(row["trajectory_id"], traj_ids[t])

                # 自有变量：packed 填充值 / 0 不是缺测
                self.assertEqual(
                    row["variables"]["depth"], depth_expect[o])
                self.assertEqual(
                    row["variables"]["quality"], quality_expect[o])

                # 继承视图：沿索引链广播，逐值与源文件比对
                self.assertEqual(
                    row["inherited"]["scalar"],
                    {"station_note": note})
                self.assertEqual(
                    row["inherited"]["profile"]["time"], time_expect[p])
                self.assertEqual(
                    row["inherited"]["profile"]["time_utc"],
                    iso_utc(time_expect[p]))
                self.assertEqual(
                    row["inherited"]["profile"]["latitude"],
                    lat_expect[p])
                self.assertEqual(
                    row["inherited"]["trajectory"]["campaign"],
                    campaign[t])

            # packed 缺测与合法 0 的具体断言
            by_index = {r["observation_index"]: r for r in obs_rows}
            self.assertIsNone(by_index[2]["variables"]["depth"])
            self.assertIsNone(by_index[3]["variables"]["quality"])
            self.assertEqual(by_index[0]["variables"]["quality"], 0)
            self.assertEqual(by_index[2]["variables"]["quality"], 0)
            self.assertEqual(by_index[0]["variables"]["depth"], 1.0)

            # ---- variables.json 元数据保留 ----
            meta = {v["name"]: v for v in variables_doc["variables"]}
            self.assertEqual(meta["depth"]["dtype"], "int16")
            self.assertEqual(meta["depth"]["dimensions"], ["obs"])
            self.assertEqual(meta["depth"]["fill_value"], -32768)
            self.assertEqual(meta["depth"]["scale_factor"], 0.1)
            self.assertEqual(meta["depth"]["add_offset"], 0.0)
            self.assertEqual(meta["depth"]["units"], "m")
            self.assertEqual(meta["quality"]["dtype"], "int8")
            self.assertEqual(meta["quality"]["fill_value"], -1)
            self.assertEqual(meta["time"]["calendar"], "standard")
            self.assertEqual(
                meta["time"]["time_units"],
                "seconds since 2026-01-01 00:00:00")
            self.assertEqual(meta["station_note"]["level"], "scalar")
            self.assertEqual(meta["station_note"]["ndim"], 0)
            self.assertEqual(variables_doc["unsupported"], [])
            self.assertEqual(variables_doc["layout"], layout)

            # 结构变量的语义属性来自文件核对
            self.assertEqual(
                meta["trajectory_id"]["attributes"]["cf_role"],
                "trajectory_id")
            self.assertEqual(
                meta["profile_id"]["attributes"]["cf_role"], "profile_id")
            self.assertEqual(
                meta["trajectoryIndex"]["attributes"]
                ["instance_dimension"], "trajectory")
            if layout == "contiguous":
                self.assertEqual(
                    meta["rowSize"]["attributes"]["sample_dimension"],
                    "obs")
            else:
                self.assertEqual(
                    meta["profileIndex"]["attributes"]
                    ["instance_dimension"], "profile")

    def test_contiguous(self):
        self._check_fixture("contiguous.nc", "contiguous")

    def test_indexed_interleaved(self):
        self._check_fixture("indexed.nc", "indexed")

    def test_indexed_obs_are_interleaved(self):
        """indexed 的观测必须交错归属，且 P-1 无观测但仍在 profile 表。"""
        nc_path = os.path.join(FIXTURES, "indexed.nc")
        with open_raw(nc_path) as ds:
            prof_of_obs = [int(x) for x in ds.variables["profileIndex"][:]]
        self.assertEqual(prof_of_obs, [2, 0, 2, 0, 2])  # 交错，非连续段
        mapping = load_mapping()
        with tempfile.TemporaryDirectory() as tmp:
            convert_file(nc_path, mapping, tmp)
            obs_rows = read_jsonl(os.path.join(
                tmp, "indexed", "observation.jsonl"))
            self.assertEqual(
                [r["profile_index"] for r in obs_rows], prof_of_obs)
            prof_rows = read_jsonl(os.path.join(
                tmp, "indexed", "profile.jsonl"))
            p1 = next(r for r in prof_rows if r["profile_id"] == "P-1")
            self.assertEqual(p1["obs_count"], 0)


# ---------------------------------------------------------------------------
# bad fixture：发布前整体拒绝
# ---------------------------------------------------------------------------

class BadFixtureTests(unittest.TestCase):
    def _assert_rejected_before_publish(self, nc_name, needle):
        nc_path = os.path.join(FIXTURES, nc_name)
        mapping = load_mapping()
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(ValidationError) as ctx:
                convert_file(nc_path, mapping, tmp)
            message = str(ctx.exception)
            # 诊断含文件/变量/索引信息
            self.assertIn(nc_name, message)
            self.assertIn(needle, message)
            # 没有任何最终关系表发布
            self.assertEqual(os.listdir(tmp), [])

    def test_bad_count(self):
        self._assert_rejected_before_publish(
            "bad-count.nc", "rowSize")

    def test_bad_index(self):
        self._assert_rejected_before_publish(
            "bad-index.nc", "profileIndex")

    def test_failure_keeps_previous_output_intact(self):
        """转换失败不得破坏同目录上一次成功发布的结果。"""
        good = os.path.join(FIXTURES, "contiguous.nc")
        mapping = load_mapping()
        with tempfile.TemporaryDirectory() as tmp:
            convert_file(good, mapping, tmp)
            published = os.path.join(tmp, "contiguous")
            before = sorted(os.listdir(published))
            with self.assertRaises(ValidationError):
                convert_file(os.path.join(FIXTURES, "bad-count.nc"),
                             mapping, tmp)
            self.assertEqual(sorted(os.listdir(published)), before)
            self.assertFalse(
                os.path.exists(os.path.join(tmp, "bad-count.tmp")))


# ---------------------------------------------------------------------------
# 合成文件：覆盖 fixture 之外的语义分支
# ---------------------------------------------------------------------------

def write_synth(path: str, *, layout: str = "contiguous",
                row_sizes=(1, 0, 2), profile_index=(0, 2, 2),
                traj_index=(1, 0, 1),
                traj_ids=("T-a", "T-b"),
                prof_ids=("P-0", "P-1", "P-2"),
                flag_values=(0, -999, 2),
                flag_fill=-999, flag_scale=0.5, flag_offset=1.0,
                campaigns=("alpha", ""),
                temps=(10.0, -9999.0, 20.0),
                include_matrix=False,
                bad_traj_index=None,
                bad_profile_index=None,
                negative_rowsize=False,
                masked_profile_index=False,
                both_layouts=False,
                bad_calendar=False):
    n_obs = len(profile_index) if layout == "indexed" else sum(row_sizes)
    ds = netCDF4.Dataset(path, "w", format="NETCDF4")
    ds.Conventions = "CF-1.10"
    ds.featureType = "trajectoryProfile"
    ds.createDimension("trajectory", len(traj_ids))
    ds.createDimension("profile", len(prof_ids))
    ds.createDimension("obs", n_obs)

    v = ds.createVariable("trajectory_id", str, ("trajectory",))
    v.cf_role = "trajectory_id"
    for i, value in enumerate(traj_ids):
        v[i] = value
    v = ds.createVariable("profile_id", str, ("profile",))
    v.cf_role = "profile_id"
    for i, value in enumerate(prof_ids):
        v[i] = value

    v = ds.createVariable("trajectoryIndex", "i4", ("profile",))
    v.instance_dimension = "trajectory"
    v[:] = bad_traj_index if bad_traj_index is not None else traj_index

    if layout == "contiguous" or both_layouts:
        v = ds.createVariable("rowSize", "i4", ("profile",))
        v.sample_dimension = "obs"
        sizes = list(row_sizes)
        if negative_rowsize:
            sizes[1] = -1
        v[:] = sizes
    if layout == "indexed" or both_layouts:
        v = ds.createVariable("profileIndex", "i4", ("obs",),
                              fill_value=np.int32(-1))
        v.instance_dimension = "profile"
        if masked_profile_index:
            data = np.ma.array(
                list(profile_index),
                mask=[False, True, False])
            v[:] = data
        else:
            v[:] = (bad_profile_index if bad_profile_index is not None
                    else profile_index)

    v = ds.createVariable("campaign", str, ("trajectory",))
    for i, value in enumerate(campaigns):
        v[i] = value

    v = ds.createVariable("time", "i8", ("profile",))
    v.units = "seconds since 2026-01-01 00:00:00"
    v.calendar = "noleap" if bad_calendar else "standard"
    v[:] = [10, 20, 30]

    v = ds.createVariable("temp", "f8", ("obs",))
    v.units = "K"
    v.missing_value = np.float64(-9999.0)
    v[:] = temps

    v = ds.createVariable("flag", "i2", ("obs",),
                          fill_value=np.int16(flag_fill))
    v.scale_factor = np.float64(flag_scale)
    v.add_offset = np.float64(flag_offset)
    # 关闭自动缩放，写入的必须是“存储值”，由转换器按规则解码
    ds.set_auto_scale(False)
    v[:] = flag_values
    ds.set_auto_scale(True)

    v = ds.createVariable("note", str)
    v[()] = "scalar-note"

    if include_matrix:
        ds.createDimension("z", 2)
        v = ds.createVariable("matrix", "f8", ("profile", "z"))
        v[:] = np.zeros((len(prof_ids), 2))

    ds.close()
    return n_obs


SYNTH_MAPPING = {
    "trajectory_dim": "trajectory",
    "profile_dim": "profile",
    "observation_dim": "obs",
    "trajectory_id": "trajectory_id",
    "profile_id": "profile_id",
    "profile_to_trajectory": "trajectoryIndex",
    "observation_layout": {
        "contiguous": "rowSize",
        "indexed": "profileIndex",
    },
    "variables": ["campaign", "time", "temp", "flag", "note"],
}


class SyntheticSemanticsTests(unittest.TestCase):
    def test_contiguous_zero_profile_and_fill_before_scale(self):
        with tempfile.TemporaryDirectory() as tmp:
            nc_path = os.path.join(tmp, "synth.nc")
            write_synth(nc_path, layout="contiguous")
            result = convert_file(nc_path, SYNTH_MAPPING, tmp)
            out = result.output_dir
            prof = read_jsonl(os.path.join(out, "profile.jsonl"))
            obs = read_jsonl(os.path.join(out, "observation.jsonl"))

            # 零观测剖面保留
            self.assertEqual(
                [(r["profile_index"], r["obs_count"]) for r in prof],
                [(0, 1), (1, 0), (2, 2)])

            # flag: 填充值 -999 不参与 scale/offset -> None；
            # 0*0.5+1.0 = 1.0（0 不是缺测）；2*0.5+1.0 = 2.0
            flags = [r["variables"]["flag"] for r in obs]
            self.assertEqual(flags, [1.0, None, 2.0])

            # missing_value 识别；temp 合法 0 场景由质量变量在 fixture 覆盖
            temps = [r["variables"]["temp"] for r in obs]
            self.assertEqual(temps, [10.0, None, 20.0])

            # 空字符串原样保留，不是缺测
            traj = read_jsonl(os.path.join(out, "trajectory.jsonl"))
            self.assertEqual(
                traj[1]["variables"]["campaign"], "")

            # 标量广播到每条观测
            for row in obs:
                self.assertEqual(
                    row["inherited"]["scalar"], {"note": "scalar-note"})

            # 时间 ISO
            self.assertEqual(
                prof[0]["variables"]["time_utc"],
                "2026-01-01T00:00:10Z")

    def test_indexed_synth(self):
        with tempfile.TemporaryDirectory() as tmp:
            nc_path = os.path.join(tmp, "synth.nc")
            write_synth(nc_path, layout="indexed",
                        profile_index=(2, 0, 2))
            result = convert_file(nc_path, SYNTH_MAPPING, tmp)
            obs = read_jsonl(os.path.join(
                result.output_dir, "observation.jsonl"))
            self.assertEqual(
                [r["profile_index"] for r in obs], [2, 0, 2])
            prof = read_jsonl(os.path.join(
                result.output_dir, "profile.jsonl"))
            self.assertEqual(
                {r["profile_index"]: r["obs_count"] for r in prof},
                {0: 1, 1: 0, 2: 2})

    def test_multidim_variable_explicitly_unsupported(self):
        with tempfile.TemporaryDirectory() as tmp:
            nc_path = os.path.join(tmp, "synth.nc")
            write_synth(nc_path, include_matrix=True)
            mapping = dict(SYNTH_MAPPING)
            mapping["variables"] = mapping["variables"] + ["matrix"]
            result = convert_file(nc_path, mapping, tmp)
            self.assertEqual(
                [u["name"] for u in result.unsupported], ["matrix"])
            with open(os.path.join(
                    result.output_dir, "variables.json"),
                    encoding="utf-8") as fh:
                doc = json.load(fh)
            meta = next(v for v in doc["variables"]
                        if v["name"] == "matrix")
            self.assertFalse(meta["supported"])
            self.assertIn("仅支持标量或单维", meta["unsupported_reason"])
            # 不进入任何关系表
            for table in ("trajectory.jsonl", "profile.jsonl",
                          "observation.jsonl"):
                with open(os.path.join(
                        result.output_dir, table),
                        encoding="utf-8") as fh:
                    self.assertNotIn("matrix", fh.read())

    def test_duplicate_profile_id_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            nc_path = os.path.join(tmp, "synth.nc")
            write_synth(nc_path, prof_ids=("P-0", "P-0", "P-2"))
            with self.assertRaises(ValidationError) as ctx:
                convert_file(nc_path, SYNTH_MAPPING, tmp)
            self.assertIn("profile_id", str(ctx.exception))
            self.assertIn("不唯一", str(ctx.exception))
            # 没有任何输出目录发布（tmp 中只有输入 nc）
            self.assertEqual(
                [n for n in os.listdir(tmp) if n.endswith((".jsonl", ".json"))
                 or os.path.isdir(os.path.join(tmp, n))], [])

    def test_out_of_range_trajectory_index_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            nc_path = os.path.join(tmp, "synth.nc")
            write_synth(nc_path, bad_traj_index=(1, 9, 1))
            with self.assertRaises(ValidationError) as ctx:
                convert_file(nc_path, SYNTH_MAPPING, tmp)
            msg = str(ctx.exception)
            self.assertIn("trajectoryIndex", msg)
            self.assertIn("索引 1", msg)
            self.assertIn("越界", msg)

    def test_out_of_range_profile_index_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            nc_path = os.path.join(tmp, "synth.nc")
            write_synth(nc_path, layout="indexed",
                        bad_profile_index=(2, 0, 7))
            with self.assertRaises(ValidationError) as ctx:
                convert_file(nc_path, SYNTH_MAPPING, tmp)
            msg = str(ctx.exception)
            self.assertIn("profileIndex", msg)
            self.assertIn("索引 2", msg)

    def test_masked_profile_index_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            nc_path = os.path.join(tmp, "synth.nc")
            write_synth(nc_path, layout="indexed",
                        masked_profile_index=True)
            with self.assertRaises(ValidationError) as ctx:
                convert_file(nc_path, SYNTH_MAPPING, tmp)
            self.assertIn("profileIndex", str(ctx.exception))

    def test_negative_rowsize_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            nc_path = os.path.join(tmp, "synth.nc")
            write_synth(nc_path, negative_rowsize=True)
            with self.assertRaises(ValidationError) as ctx:
                convert_file(nc_path, SYNTH_MAPPING, tmp)
            self.assertIn("rowSize", str(ctx.exception))
            self.assertIn("负数", str(ctx.exception))

    def test_both_layouts_mutually_exclusive(self):
        with tempfile.TemporaryDirectory() as tmp:
            nc_path = os.path.join(tmp, "synth.nc")
            write_synth(nc_path, both_layouts=True)
            with self.assertRaises(ValidationError) as ctx:
                convert_file(nc_path, SYNTH_MAPPING, tmp)
            self.assertIn("互斥", str(ctx.exception))

    def test_bad_calendar_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            nc_path = os.path.join(tmp, "synth.nc")
            write_synth(nc_path, bad_calendar=True)
            with self.assertRaises(Exception) as ctx:
                convert_file(nc_path, SYNTH_MAPPING, tmp)
            self.assertIn("noleap", str(ctx.exception))

    def test_missing_selected_variable_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            nc_path = os.path.join(tmp, "synth.nc")
            write_synth(nc_path)
            mapping = dict(SYNTH_MAPPING)
            mapping["variables"] = mapping["variables"] + ["ghost"]
            with self.assertRaises(ValidationError) as ctx:
                convert_file(nc_path, mapping, tmp)
            self.assertIn("ghost", str(ctx.exception))

    def test_files_never_merged(self):
        """两个文件各自独立产出，observation 的 source_file 不串。"""
        with tempfile.TemporaryDirectory() as tmp:
            first = os.path.join(tmp, "a.nc")
            second = os.path.join(tmp, "b.nc")
            write_synth(first)
            write_synth(second)
            convert_file(first, SYNTH_MAPPING, tmp)
            convert_file(second, SYNTH_MAPPING, tmp)
            self.assertEqual(
                sorted(n for n in os.listdir(tmp)
                       if os.path.isdir(os.path.join(tmp, n))),
                ["a", "b"])
            for stem in ("a", "b"):
                rows = read_jsonl(os.path.join(
                    tmp, stem, "observation.jsonl"))
                self.assertTrue(all(
                    r["source_file"] == f"{stem}.nc" for r in rows))


class CliTests(unittest.TestCase):
    def test_cli_good_and_bad(self):
        env = dict(os.environ)
        with tempfile.TemporaryDirectory() as tmp:
            cmd = [sys.executable, "-m", "cf_rel.cli",
                   "--mapping", MAPPING_PATH, "--out", tmp,
                   os.path.join(FIXTURES, "contiguous.nc"),
                   os.path.join(FIXTURES, "indexed.nc")]
            proc = subprocess.run(
                cmd, cwd=ROOT, env=env, capture_output=True, text=True)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertTrue(
                os.path.exists(os.path.join(tmp, "contiguous",
                                            "observation.jsonl")))

            cmd = [sys.executable, "-m", "cf_rel.cli",
                   "--mapping", MAPPING_PATH, "--out", tmp,
                   os.path.join(FIXTURES, "bad-count.nc"),
                   os.path.join(FIXTURES, "bad-index.nc")]
            proc = subprocess.run(
                cmd, cwd=ROOT, env=env, capture_output=True, text=True)
            self.assertEqual(proc.returncode, 1)
            self.assertIn("bad-count.nc", proc.stderr)
            self.assertIn("bad-index.nc", proc.stderr)
            self.assertFalse(
                os.path.exists(os.path.join(tmp, "bad-count")))
            self.assertFalse(
                os.path.exists(os.path.join(tmp, "bad-index")))


if __name__ == "__main__":
    unittest.main(verbosity=2)
