"""端到端测试：仅用标准库 unittest，直接驱动包内 API 与 CLI。

覆盖：
* worker 按 shard 序号取模分片、身份含 shard、同 key 跨 shard 不合并；
* B=3 有界替换式洗牌与独立参考实现逐项相同；
* 任意 N 处 checkpoint -> resume 与不中断序列逐项完全相同；
* 缺件 / 重复部件 / 非法路径 / 超大小均显式失败；
* checkpoint 绑定（config/manifest/源/worker/epoch）变动即拒绝；
* 内存完整样本数除当前组装项外不超过 B。
"""

from __future__ import annotations

import copy
import io
import json
import os
import random
import shutil
import subprocess
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from wds_stream.checkpoint import (  # noqa: E402
    build_checkpoint,
    load_checkpoint,
    resume_runner,
    save_checkpoint,
)
from wds_stream.errors import CheckpointError, DataAssemblyError  # noqa: E402
from wds_stream.manifest import (  # noqa: E402
    load_config,
    load_manifest,
    make_rng,
    worker_seed,
)
from wds_stream.shuffle import WorkerRunner  # noqa: E402
from wds_stream.tarstream import Sample, SampleStream  # noqa: E402

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures"
CONFIG_PATH = FIXTURES / "config.json"
MANIFEST_PATH = FIXTURES / "manifest.json"


def _records(runner):
    try:
        return list(runner.run())
    finally:
        runner.close()


class ManifestAndShardingTests(unittest.TestCase):
    def setUp(self):
        self.manifest = load_manifest(MANIFEST_PATH)
        self.config = load_config(CONFIG_PATH)

    def test_worker_sharding_by_modulo(self):
        w0 = self.manifest.shards_for_worker(0, 2)
        w1 = self.manifest.shards_for_worker(1, 2)
        self.assertEqual([s.shard_id for s in w0], ["0", "2"])
        self.assertEqual([s.shard_id for s in w1], ["1"])
        # 互不读取对方 shard
        self.assertEqual({s.index for s in w0} & {s.index for s in w1}, set())

    def test_sample_identity_includes_shard_and_shared_key_kept_apart(self):
        with SampleStream(self.manifest.shards, self.config.parts,
                          self.config.max_member_bytes) as stream:
            ids = [s.sample_id for s in stream]
        # shared 出现在三个 shard，必须是三个不同身份
        shared = [i for i in ids if i.endswith("/shared")]
        self.assertEqual(sorted(shared), ["0/shared", "1/shared", "2/shared"])
        self.assertEqual(len(ids), 12)
        self.assertEqual(len(set(ids)), 12)  # 仅用 key 会合并 -> 身份集合会变小

    def test_seed_derivation_matches_config_formula(self):
        seed_int = worker_seed(42, 0, 0)
        payload = json.dumps([42, 0, 0], separators=(",", ":")).encode("utf-8")
        import hashlib

        self.assertEqual(
            seed_int, int.from_bytes(hashlib.sha256(payload).digest()[:8], "big")
        )
        # 不同 worker 派生不同 seed
        self.assertNotEqual(worker_seed(42, 0, 0), worker_seed(42, 0, 1))


class ShuffleSemanticsTests(unittest.TestCase):
    def setUp(self):
        self.manifest = load_manifest(MANIFEST_PATH)
        self.config = load_config(CONFIG_PATH)

    def _reference_shuffle(self, worker_id):
        """完全按 config 文字写的独立参考实现，与生产代码对照。"""
        shards = self.manifest.shards_for_worker(worker_id, self.config.worker_count)
        rng = make_rng(self.config.seed, self.config.epoch, worker_id)
        B = self.config.buffer_samples
        buffer, out = [], []
        with SampleStream(shards, self.config.parts, self.config.max_member_bytes) as stream:
            for sample in stream:
                if len(buffer) < B:
                    buffer.append(sample.sample_id)
                    continue
                i = rng.randrange(len(buffer))
                out.append(buffer[i])
                buffer[i] = sample.sample_id
        # 源结束后随机弹出直到缓冲清空。
        while buffer:
            i = rng.randrange(len(buffer))
            out.append(buffer.pop(i))
        return out

    def test_matches_independent_reference(self):
        for worker_id in (0, 1):
            runner = WorkerRunner.fresh(self.manifest, self.config, worker_id)
            got = [r["sample_id"] for r in _records(runner)]
            self.assertEqual(got, self._reference_shuffle(worker_id))

    def test_buffer_never_exceeds_B_plus_assembly_slot(self):
        """监视运行过程：缓冲完整样本数 <= B，源里最多还有一个在组装的样本。"""
        max_seen = 0
        runner = WorkerRunner.fresh(self.manifest, self.config, 0)
        gen = runner.run()
        for _ in gen:
            max_seen = max(max_seen, len(runner.buffer))
            self.assertLessEqual(len(runner.buffer), self.config.buffer_samples)
        self.assertEqual(max_seen, self.config.buffer_samples)

    def test_deterministic_across_runs(self):
        a = _records(WorkerRunner.fresh(self.manifest, self.config, 0))
        b = _records(WorkerRunner.fresh(self.manifest, self.config, 0))
        self.assertEqual(a, b)

    def test_part_digest_order_and_content(self):
        runner = WorkerRunner.fresh(self.manifest, self.config, 1)
        try:
            record = next(iter(runner.run()))
        finally:
            runner.close()
        self.assertEqual([p["part"] for p in record["parts"]],
                         list(self.config.parts))
        for part in record["parts"]:
            self.assertEqual(set(part), {"part", "size", "sha256",
                                         "header_offset", "data_offset"})


class CheckpointResumeTests(unittest.TestCase):
    def setUp(self):
        self.manifest = load_manifest(MANIFEST_PATH)
        self.config = load_config(CONFIG_PATH)
        self.tmp = Path(tempfile.mkdtemp())

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _full(self, worker_id):
        return _records(WorkerRunner.fresh(self.manifest, self.config, worker_id))

    def test_resume_at_every_position_matches_uninterrupted(self):
        for worker_id in (0, 1):
            full = self._full(worker_id)
            total = len(full)
            for n in range(1, total):  # 每个可能的保存点都验证
                runner = WorkerRunner.fresh(self.manifest, self.config, worker_id)
                prefix = []
                ckpt_path = self.tmp / f"w{worker_id}_n{n}.json"
                for record in runner.run():
                    prefix.append(record)
                    if runner.consumed == n:
                        save_checkpoint(
                            ckpt_path,
                            build_checkpoint(runner, self.manifest, self.config, worker_id),
                        )
                        break
                runner.close()
                self.assertEqual([r["seq"] for r in prefix], list(range(1, n + 1)))

                ckpt = load_checkpoint(ckpt_path)
                resumed = resume_runner(ckpt, self.manifest, self.config, worker_id)
                suffix = _records(resumed)
                # seq 连续、无重复无缺口
                self.assertEqual([r["seq"] for r in suffix], list(range(n + 1, total + 1)))
                # 逐项完全相同（身份 + 部件摘要 + 次序）
                self.assertEqual(prefix + suffix, full)

    def test_checkpoint_holds_buffer_position_rng_count(self):
        runner = WorkerRunner.fresh(self.manifest, self.config, 0)
        prefix_ids = []
        for record in runner.run():
            prefix_ids.append(record["sample_id"])
            if runner.consumed == 3:
                ckpt = build_checkpoint(runner, self.manifest, self.config, 0)
                break
        runner.close()
        state = ckpt["worker_state"]
        self.assertEqual(state["consumed"], 3)
        self.assertLessEqual(len(state["buffer"]), self.config.buffer_samples)
        self.assertEqual(len(state["buffer"]), 3)
        self.assertIn("rng_state", state)
        self.assertEqual(set(state["position"]), {"shard_index", "offset"})
        # 缓冲中样本与已输出前缀互不重叠，且位置指向尚未消费的成员头
        buffered_ids = {b["sample_id"] for b in state["buffer"]}
        self.assertEqual(buffered_ids & set(prefix_ids), set())
        self.assertGreaterEqual(state["position"]["offset"], 0)

    def test_resume_does_not_replay_consumed_samples(self):
        runner = WorkerRunner.fresh(self.manifest, self.config, 0)
        prefix_ids = []
        for record in runner.run():
            prefix_ids.append(record["sample_id"])
            if runner.consumed == 4:
                ckpt = build_checkpoint(runner, self.manifest, self.config, 0)
                break
        runner.close()
        resumed = resume_runner(load_checkpoint(self._dump(ckpt)),
                                self.manifest, self.config, 0)
        suffix_ids = [r["sample_id"] for r in _records(resumed)]
        self.assertEqual(set(prefix_ids) & set(suffix_ids), set())
        self.assertEqual(sorted(prefix_ids + suffix_ids),
                         sorted(r["sample_id"] for r in self._full(0)))

    def test_resume_seeks_forward_and_never_rereads_consumed_offsets(self):
        """恢复点必须是前向字节偏移；监视底层打开参数，确认 seek 到保存点而非 0。"""
        from wds_stream.tarstream import SampleStream

        runner = WorkerRunner.fresh(self.manifest, self.config, 0)
        for _ in runner.run():
            if runner.consumed == 3:
                ckpt = build_checkpoint(runner, self.manifest, self.config, 0)
                break
        runner.close()
        state = ckpt["worker_state"]
        saved_shard = state["position"]["shard_index"]
        saved_offset = state["position"]["offset"]
        self.assertGreater(saved_offset, 0)  # 不是 shard 起点

        seen_calls = []
        orig_open = SampleStream._open_current_shard

        def spy_open(self):
            seen_calls.append((self._shards[self._next_shard_pos].shard_id,
                               self._next_offset))
            return orig_open(self)

        SampleStream._open_current_shard = spy_open
        try:
            resumed = resume_runner(load_checkpoint(self._dump(ckpt)),
                                    self.manifest, self.config, 0)
            _records(resumed)
        finally:
            SampleStream._open_current_shard = orig_open
        # 第一次打开续读 shard 时的偏移必须等于 checkpoint 保存的前向偏移
        first = seen_calls[0]
        self.assertEqual(first[1], saved_offset)
        # 所有打开偏移都不小于保存点（不会回头重读已消费成员）
        for shard_id, offset in seen_calls:
            if shard_id == str(saved_shard):
                self.assertGreaterEqual(offset, saved_offset)

    def _dump(self, obj) -> Path:
        p = self.tmp / "ckpt.json"
        save_checkpoint(p, obj)
        return p

    def test_resume_rebuilds_buffer_from_offsets_and_verifies(self):
        runner = WorkerRunner.fresh(self.manifest, self.config, 0)
        for record in runner.run():
            if runner.consumed == 2:
                ckpt_path = self._dump(
                    build_checkpoint(runner, self.manifest, self.config, 0)
                )
                break
        runner.close()
        ckpt = load_checkpoint(ckpt_path)
        # 篡改缓冲条目的 sha256 -> 重建核验必须拒绝
        tampered = copy.deepcopy(ckpt)
        tampered["worker_state"]["buffer"][0]["parts"][0]["sha256"] = "0" * 64
        tampered["self_sha256"] = "1" * 64  # 同时先绕过自哈希层（模拟分层校验）
        with self.assertRaises(CheckpointError):
            resume_runner(tampered, self.manifest, self.config, 0)


class AssemblyFailureTests(unittest.TestCase):
    """缺件、重复部件、非法路径、超大小 -> worker 明确失败。"""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.base_config = load_config(CONFIG_PATH).raw

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _make_shard(self, name, members):
        """members: list[(member_name, bytes)]，写 USTAR。"""
        path = self.tmp / name
        with tarfile.open(path, "w", format=tarfile.USTAR_FORMAT) as tf:
            for member_name, data in members:
                info = tarfile.TarInfo(member_name)
                info.size = len(data)
                tf.addfile(info, io.BytesIO(data))
        return path

    def _manifest(self, shard_path):
        import hashlib

        doc = [
            {
                "shard_id": "x",
                "path": shard_path.name,
                "sha256": hashlib.sha256(shard_path.read_bytes()).hexdigest(),
            }
        ]
        mp = self.tmp / "manifest.json"
        mp.write_text(json.dumps(doc), encoding="utf-8")
        return mp

    def _config(self, **overrides):
        raw = dict(self.base_config)
        raw.update(overrides)
        cp = self.tmp / "config.json"
        cp.write_text(json.dumps(raw), encoding="utf-8")
        return cp

    def _run(self, config_path, manifest_path):
        manifest = load_manifest(manifest_path)
        config = load_config(config_path)
        runner = WorkerRunner.fresh(manifest, config, 0)
        return list(runner.run())

    def test_missing_part_fails(self):
        path = self._make_shard(
            "missing.tar", [("bad.png", b"x"), ("bad.json", b"{}")]
        )
        with self.assertRaises(DataAssemblyError) as ctx:
            self._run(self._config(), self._manifest(path))
        self.assertIn("缺少部件", str(ctx.exception))

    def test_duplicate_part_fails(self):
        path = self._make_shard(
            "dup.tar",
            [("bad.png", b"x"), ("bad.png", b"y"), ("bad.json", b"{}"),
             ("bad.txt", b"z")],
        )
        with self.assertRaises(DataAssemblyError) as ctx:
            self._run(self._config(), self._manifest(path))
        self.assertIn("重复", str(ctx.exception))

    def test_illegal_path_fails(self):
        path = self._make_shard(
            "illegal.tar",
            [("sub/dir.png", b"x"), ("k.json", b"{}"), ("k.txt", b"t"),
             ("k.png", b"p")],
        )
        with self.assertRaises(DataAssemblyError) as ctx:
            self._run(self._config(), self._manifest(path))
        self.assertIn("非法", str(ctx.exception))

    def test_bad_key_charset_fails(self):
        path = self._make_shard(
            "badkey.tar",
            [("a-b.png", b"x"), ("a-b.json", b"{}"), ("a-b.txt", b"t")],
        )
        with self.assertRaises(DataAssemblyError):
            self._run(self._config(), self._manifest(path))

    def test_oversize_member_fails(self):
        path = self._make_shard(
            "big.tar",
            [("k.png", b"x" * 10), ("k.json", b"{}"), ("k.txt", b"t")],
        )
        with self.assertRaises(DataAssemblyError) as ctx:
            self._run(self._config(max_member_bytes=5), self._manifest(path))
        self.assertIn("超过上限", str(ctx.exception))

    def test_next_key_never_backfills_incomplete_sample(self):
        # 第一个样本缺 txt，下一个 key 的部件已经在流里，也不允许补救。
        path = self._make_shard(
            "gap.tar",
            [("a.png", b"1"), ("a.json", b"{}"),
             ("b.png", b"2"), ("b.json", b"{}"), ("b.txt", b"t")],
        )
        with self.assertRaises(DataAssemblyError) as ctx:
            self._run(self._config(), self._manifest(path))
        self.assertIn("a", str(ctx.exception))
        self.assertIn("缺少部件", str(ctx.exception))

    def test_fixture_failure_tars(self):
        """直接使用仓库提供的 missing.tar / duplicate.tar。"""
        import hashlib

        for tar_name, needle in (("missing.tar", "缺少部件"),
                                 ("duplicate.tar", "重复")):
            src = FIXTURES / tar_name
            dst = self.tmp / tar_name
            shutil.copy(src, dst)
            doc = [{
                "shard_id": "x",
                "path": tar_name,
                "sha256": hashlib.sha256(dst.read_bytes()).hexdigest(),
            }]
            mp = self.tmp / f"manifest_{tar_name}.json"
            mp.write_text(json.dumps(doc), encoding="utf-8")
            with self.assertRaises(DataAssemblyError) as ctx:
                self._run(self._config(), mp)
            self.assertIn(needle, str(ctx.exception))


class BindingInvalidationTests(unittest.TestCase):
    def setUp(self):
        self.manifest = load_manifest(MANIFEST_PATH)
        self.config = load_config(CONFIG_PATH)
        self.tmp = Path(tempfile.mkdtemp())
        runner = WorkerRunner.fresh(self.manifest, self.config, 0)
        self.ckpt_path = self.tmp / "ckpt.json"
        for _ in runner.run():
            if runner.consumed == 3:
                save_checkpoint(
                    self.ckpt_path,
                    build_checkpoint(runner, self.manifest, self.config, 0),
                )
                break
        runner.close()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _changed_config(self, **overrides):
        raw = dict(self.config.raw)
        raw.update(overrides)
        p = self.tmp / "config.json"
        p.write_text(json.dumps(raw), encoding="utf-8")
        return load_config(p)

    def test_seed_change_rejected(self):
        changed = self._changed_config(seed=999)
        with self.assertRaises(CheckpointError) as ctx:
            resume_runner(load_checkpoint(self.ckpt_path), self.manifest, changed, 0)
        self.assertIn("seed", str(ctx.exception))

    def test_epoch_change_rejected(self):
        changed = self._changed_config(epoch=1)
        with self.assertRaises(CheckpointError):
            resume_runner(load_checkpoint(self.ckpt_path), self.manifest, changed, 0)

    def test_worker_topology_change_rejected(self):
        changed = self._changed_config(worker_count=4)
        with self.assertRaises(CheckpointError):
            resume_runner(load_checkpoint(self.ckpt_path), self.manifest, changed, 0)

    def test_buffer_size_change_rejected(self):
        changed = self._changed_config(buffer_samples=2)
        with self.assertRaises(CheckpointError):
            resume_runner(load_checkpoint(self.ckpt_path), self.manifest, changed, 0)

    def test_wrong_worker_id_rejected(self):
        with self.assertRaises(CheckpointError):
            resume_runner(load_checkpoint(self.ckpt_path), self.manifest,
                          self.config, 1)

    def test_manifest_change_rejected(self):
        # 复制 fixtures 到临时目录并改写 manifest 路径 -> manifest_id/路径均变化
        alt_dir = self.tmp / "alt"
        shutil.copytree(FIXTURES, alt_dir)
        alt_manifest = load_manifest(alt_dir / "manifest.json")
        with self.assertRaises(CheckpointError):
            resume_runner(load_checkpoint(self.ckpt_path), alt_manifest,
                          self.config, 0)

    def test_source_replacement_rejected(self):
        """shard 内容被替换 -> source_digest 变化，拒绝续跑。"""
        alt_dir = self.tmp / "src"
        shutil.copytree(FIXTURES, alt_dir)
        shard0 = alt_dir / "shards" / "0.tar"
        with tarfile.open(shard0, "a") as tf:  # 追加内容改变 sha256
            info = tarfile.TarInfo("tampered.txt")
            info.size = 1
            tf.addfile(info, io.BytesIO(b"z"))
        # 用实际新哈希重建 manifest（模拟“源被替换但 manifest 同步更新”）
        import hashlib

        doc = json.loads((alt_dir / "manifest.json").read_text())
        doc[0]["sha256"] = hashlib.sha256(shard0.read_bytes()).hexdigest()
        (alt_dir / "manifest.json").write_text(json.dumps(doc), encoding="utf-8")
        alt_manifest = load_manifest(alt_dir / "manifest.json")
        with self.assertRaises(CheckpointError) as ctx:
            resume_runner(load_checkpoint(self.ckpt_path), alt_manifest,
                          self.config, 0)
        self.assertIn("source_digest", str(ctx.exception))

    def test_corrupted_checkpoint_self_hash_rejected(self):
        doc = json.loads(self.ckpt_path.read_text())
        doc["worker_state"]["consumed"] = 999
        self.ckpt_path.write_text(json.dumps(doc), encoding="utf-8")
        with self.assertRaises(CheckpointError) as ctx:
            load_checkpoint(self.ckpt_path)
        self.assertIn("自校验哈希", str(ctx.exception))


class CLITests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _run_cli(self, *args, expect=0):
        proc = subprocess.run(
            [sys.executable, "-m", "wds_stream", *args],
            cwd=Path(__file__).resolve().parent.parent,
            capture_output=True, text=True,
        )
        self.assertEqual(proc.returncode, expect,
                         msg=f"stdout={proc.stdout}\nstderr={proc.stderr}")
        return proc

    def test_cli_read_resume_audit(self):
        out = self.tmp / "w0.jsonl"
        ckpt = self.tmp / "w0.ckpt"
        self._run_cli("read", "--config", str(CONFIG_PATH),
                      "--manifest", str(MANIFEST_PATH),
                      "--worker-id", "0", "--output", str(out),
                      "--checkpoint", str(ckpt), "--stop-after", "3")
        self.assertEqual(len(out.read_text().strip().splitlines()), 3)
        self._run_cli("resume", "--config", str(CONFIG_PATH),
                      "--manifest", str(MANIFEST_PATH),
                      "--worker-id", "0", "--checkpoint", str(ckpt),
                      "--output", str(out))
        self.assertEqual(len(out.read_text().strip().splitlines()), 8)
        proc = self._run_cli("audit", "checkpoint", "--config", str(CONFIG_PATH),
                             "--manifest", str(MANIFEST_PATH),
                             "--checkpoint", str(ckpt), "--worker-id", "0")
        self.assertIn('"ok": true', proc.stdout)

    def test_cli_failure_exit_code(self):
        import hashlib

        bad = self.tmp / "missing.tar"
        shutil.copy(FIXTURES / "missing.tar", bad)
        manifest = self.tmp / "manifest.json"
        manifest.write_text(json.dumps([{
            "shard_id": "x", "path": "missing.tar",
            "sha256": hashlib.sha256(bad.read_bytes()).hexdigest(),
        }]), encoding="utf-8")
        config = self.tmp / "config.json"
        config.write_text(Path(CONFIG_PATH).read_text(), encoding="utf-8")
        proc = self._run_cli("read", "--config", str(config),
                             "--manifest", str(manifest),
                             "--worker-id", "0",
                             "--output", str(self.tmp / "o.jsonl"),
                             expect=1)
        self.assertIn("DataAssemblyError", proc.stderr)


if __name__ == "__main__":
    unittest.main(verbosity=2)
