#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""本地原始日志采集器（Python 3.11，单进程、单目录）。

rotation_mode 由配置明确指定，不做猜测：

- rename：按 (st_dev, st_ino) 识别已打开文件。active.log 换成新 inode 时，
  旧 inode 继续按自己的 offset 排空（即使已改名、即使已到过一次 EOF），
  新 inode 从偏移 0 另起一代；两者不共用 offset。目录中已知 inode 的另一个
  名字只登记为别名，不作为新流重复导入。外部 stop_file 出现后，排空所有
  已知文件再退出。

- copytruncate：只跟踪 active 路径，复制归档不会被当作新输入。文件变短、
  或已读前缀（前 prefix_check_bytes 字节）的 SHA-256 摘要变化时，结束旧代、
  从偏移 0 建立新代，记录 discontinuity 与旧确认偏移；旧代残尾丢弃但保留
  诊断。这种方式无法证明截断前未读数据没有丢失，报告只陈述实际收集到的行，
  不依据日志业务 id 猜补缺口。

行切割：按 read_chunk_bytes 字节增量读取（块可拆开多字节字符与行尾），
只有以 LF 结束且 UTF-8/JSON 合法的完整行进入记录表；坏行按完整行隔离，
不影响后续行偏移。原始字节（records.jsonl）与规范 JSON（canonical.jsonl）
分别保存，[start_offset, end_offset) 含终止 LF。

子命令：run / status / export。writer 的 truth 文件仅供独立验收，采集器不读取。
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import stat as statmod
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

RECORD = "record"        # 合法完整行，进入记录表
BAD_JSON = "bad_json"    # 完整行，UTF-8 合法但 JSON 非法，隔离
BAD_UTF8 = "bad_utf8"    # 完整行，UTF-8 非法，隔离
TOO_LONG = "too_long"    # 超过 max_line_bytes，隔离
MODES = ("rename", "copytruncate")


@dataclass
class Config:
    rotation_mode: str
    active_name: str
    read_chunk_bytes: int
    poll_seconds: float
    max_line_bytes: int
    prefix_check_bytes: int

    @classmethod
    def load(cls, path: str) -> "Config":
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        mode = data.get("rotation_mode")
        if mode not in MODES:
            raise SystemExit(
                f"配置必须明确 rotation_mode 为 {MODES} 之一，实际为 {mode!r}；不做猜测。"
            )
        try:
            return cls(
                rotation_mode=mode,
                active_name=str(data.get("active_name", "active.log")),
                read_chunk_bytes=int(data["read_chunk_bytes"]),
                poll_seconds=float(data["poll_seconds"]),
                max_line_bytes=int(data["max_line_bytes"]),
                prefix_check_bytes=int(data["prefix_check_bytes"]),
            )
        except KeyError as exc:
            raise SystemExit(f"配置缺少必需项: {exc}") from exc


@dataclass
class Stream:
    """一个字节流：rename 模式下一个已知 inode；copytruncate 模式下 active 路径的一代。"""

    generation: int
    dev: int
    inode: int
    aliases: set[str] = field(default_factory=set)
    offset: int = 0                                # 已确认读取到的绝对偏移
    buffer: bytearray = field(default_factory=bytearray)  # 未完成尾行暂存（内存，少量）
    buffer_start: int = 0                          # buffer[0] 对应的绝对偏移
    prefix_len: int = 0                            # copytruncate：已读前缀长度
    prefix_sha256: str = ""                        # copytruncate：已读前缀摘要
    closed: bool = False                           # copytruncate：旧代结束，不再读取
    records: int = 0
    isolated: int = 0

    @property
    def key(self) -> tuple[int, int]:
        return (self.dev, self.inode)


class Store:
    """采集输出：records/canonical/invalid 三个 JSONL 追加文件 + state.json 快照。

    原始字节与规范 JSON 分别保存；state.json 原子替换，供 status/export 读取。
    """

    def __init__(self, state_dir: Path):
        self.dir = state_dir
        self.dir.mkdir(parents=True, exist_ok=True)
        self._records = (self.dir / "records.jsonl").open("a", encoding="utf-8")
        self._canonical = (self.dir / "canonical.jsonl").open("a", encoding="utf-8")
        self._invalid = (self.dir / "invalid.jsonl").open("a", encoding="utf-8")

    def append_record(self, entry: dict, canonical_text: str) -> None:
        self._records.write(json.dumps(entry, ensure_ascii=False) + "\n")
        self._records.flush()
        self._canonical.write(
            json.dumps({"seq": entry["seq"], "canonical_json": canonical_text},
                       ensure_ascii=False) + "\n")
        self._canonical.flush()

    def append_invalid(self, entry: dict) -> None:
        self._invalid.write(json.dumps(entry, ensure_ascii=False) + "\n")
        self._invalid.flush()

    def write_state(self, state: dict) -> None:
        tmp = self.dir / ".state.json.tmp"
        tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, self.dir / "state.json")

    def close(self) -> None:
        for fh in (self._records, self._canonical, self._invalid):
            fh.close()


class Collector:
    def __init__(self, cfg: Config, log_dir: Path, stop_file: Path, state_dir: Path):
        self.cfg = cfg
        self.log_dir = log_dir
        self.stop_file = stop_file
        self.store = Store(state_dir)
        self.streams: list[Stream] = []                 # 所有出现过的流/代
        self.by_key: dict[tuple[int, int], Stream] = {}  # rename：inode -> 流
        self.active_stream: Stream | None = None         # copytruncate：当前代
        self.next_generation = 0
        self.seq = 0
        self.discontinuities: list[dict] = []
        self.discarded_tails: list[dict] = []
        self.unterminated_tails: list[dict] = []
        self.events: list[dict] = []
        self.stop_seen = False
        self.finished = False
        self._state_dir_real = os.path.realpath(state_dir)
        self._stop_real = os.path.realpath(stop_file)

    # ---------- 主循环 ----------

    def run(self) -> int:
        self.store.write_state(self.snapshot())
        try:
            while True:
                if self.stop_file.exists():
                    # stop 出现才收尾：最后一轮扫描 + 排空所有已知文件
                    self.stop_seen = True
                    self.poll()
                    self.drain_all()
                    break
                self.poll()
                self.store.write_state(self.snapshot())
                time.sleep(self.cfg.poll_seconds)
        except KeyboardInterrupt:
            self.events.append({"event": "interrupted"})
        self.finalize()
        self.store.write_state(self.snapshot())
        self.store.close()
        return 0

    def poll(self) -> None:
        if self.cfg.rotation_mode == "rename":
            self.poll_rename()
        else:
            self.poll_copytruncate()

    def drain_all(self) -> None:
        # writer 已关闭全部句柄；每个流读到 EOF 即无新数据，多读一轮兜底
        for _ in range(3):
            progress = sum(self.read_stream(s) for s in self.streams)
            if progress == 0:
                break

    def finalize(self) -> None:
        for stream in self.streams:
            if stream.buffer:
                self.unterminated_tails.append({
                    "generation": stream.generation,
                    "dev": stream.dev, "inode": stream.inode,
                    "paths": sorted(stream.aliases),
                    "buffer_start": stream.buffer_start,
                    "pending_bytes": len(stream.buffer),
                    "raw_base64": base64.b64encode(bytes(stream.buffer)).decode("ascii"),
                    "note": "stop 后仍无 LF 的残尾，不进入记录表（仅诊断）",
                })
        self.finished = True

    # ---------- rename 模式 ----------

    def poll_rename(self) -> None:
        try:
            entries = list(os.scandir(self.log_dir))
        except FileNotFoundError:
            entries = []  # 目录或 active 文件可能尚未创建，等待出现
        for entry in entries:
            if self._skip_path(entry.name):
                continue
            try:
                st = entry.stat()
            except FileNotFoundError:
                continue
            if not statmod.S_ISREG(st.st_mode):
                continue
            key = (st.st_dev, st.st_ino)
            stream = self.by_key.get(key)
            if stream is None:
                stream = self._new_stream(st, {entry.name})
                self.events.append({
                    "event": "stream_opened", "generation": stream.generation,
                    "dev": st.st_dev, "inode": st.st_ino, "path": entry.name,
                })
            elif entry.name not in stream.aliases:
                stream.aliases.add(entry.name)
                self.events.append({
                    "event": "alias_not_reimported", "generation": stream.generation,
                    "dev": st.st_dev, "inode": st.st_ino, "path": entry.name,
                    "note": "已知 inode 的另一个名字，不作为新流重复导入",
                })
        for stream in self.streams:
            self.read_stream(stream)

    # ---------- copytruncate 模式 ----------

    def poll_copytruncate(self) -> None:
        path = self.log_dir / self.cfg.active_name
        try:
            st = path.stat()
        except FileNotFoundError:
            return  # active 文件可能尚未创建，等待出现
        stream = self.active_stream
        if stream is None:
            stream = self._new_stream(st, {self.cfg.active_name})
            self.active_stream = stream
            self.events.append({
                "event": "generation_opened", "generation": stream.generation,
                "reason": "initial", "dev": st.st_dev, "inode": st.st_ino,
                "path": self.cfg.active_name,
            })
        else:
            reason = None
            if (st.st_dev, st.st_ino) != stream.key:
                reason = "inode_changed"
            elif st.st_size < stream.offset:
                reason = "size_shrink"
            elif stream.prefix_len:
                with path.open("rb") as fh:
                    head = fh.read(stream.prefix_len)
                if hashlib.sha256(head).hexdigest() != stream.prefix_sha256:
                    reason = "prefix_checksum_changed"
            if reason:
                stream = self._rotate_copytruncate(stream, reason, st)
        self.read_stream(stream)
        self._refresh_prefix(stream, path)

    def _rotate_copytruncate(self, old: Stream, reason: str, st) -> Stream:
        if old.buffer:
            self.discarded_tails.append({
                "generation": old.generation,
                "paths": sorted(old.aliases),
                "buffer_start": old.buffer_start,
                "pending_bytes": len(old.buffer),
                "raw_base64": base64.b64encode(bytes(old.buffer)).decode("ascii"),
                "note": "copytruncate 分代，旧代未完成尾行丢弃（仅诊断，不进入记录表）",
            })
        old.closed = True
        new = self._new_stream(st, {self.cfg.active_name})
        self.discontinuities.append({
            "from_generation": old.generation,
            "to_generation": new.generation,
            "reason": reason,
            "old_confirmed_offset": old.offset,
            "size_at_detection": st.st_size,
            "continuity": "unprovable",
            "note": "旧代确认偏移之后、截断/覆写之前的未读数据可能已丢失；"
                    "copytruncate 无法证明无丢失，也不按业务 id 猜补。",
        })
        self.events.append({
            "event": "generation_rotated", "from": old.generation,
            "to": new.generation, "reason": reason,
        })
        self.active_stream = new
        return new

    def _refresh_prefix(self, stream: Stream, path: Path) -> None:
        want = min(self.cfg.prefix_check_bytes, stream.offset)
        if want <= stream.prefix_len:
            return
        with path.open("rb") as fh:
            head = fh.read(want)
        if len(head) != want:
            return  # 文件在读取间隙又变化，下一轮再试
        stream.prefix_len = want
        stream.prefix_sha256 = hashlib.sha256(head).hexdigest()

    # ---------- 读取与行切割 ----------

    def _new_stream(self, st, aliases: set[str]) -> Stream:
        stream = Stream(generation=self.next_generation,
                        dev=st.st_dev, inode=st.st_ino, aliases=set(aliases))
        self.next_generation += 1
        self.streams.append(stream)
        self.by_key.setdefault(stream.key, stream)
        return stream

    def _skip_path(self, name: str) -> bool:
        real = os.path.realpath(self.log_dir / name)
        if real == self._stop_real:
            return True
        return real == self._state_dir_real or real.startswith(self._state_dir_real + os.sep)

    def _current_path(self, stream: Stream) -> Path | None:
        for name in sorted(stream.aliases):
            p = self.log_dir / name
            try:
                st = p.stat()
            except FileNotFoundError:
                continue
            if (st.st_dev, st.st_ino) == stream.key:
                return p
        return None

    def read_stream(self, stream: Stream) -> int:
        if stream.closed:
            return 0
        path = self._current_path(stream)
        if path is None:
            return 0
        total = 0
        with path.open("rb") as fh:
            fst = os.fstat(fh.fileno())
            if (fst.st_dev, fst.st_ino) != stream.key:
                return 0  # 打开瞬间路径已指向别的 inode，本轮放弃
            fh.seek(stream.offset)
            while True:
                chunk = fh.read(self.cfg.read_chunk_bytes)
                if not chunk:
                    break
                stream.buffer += chunk
                stream.offset += len(chunk)
                total += len(chunk)
        if total:
            self.carve(stream)
        return total

    def carve(self, stream: Stream) -> None:
        while True:
            idx = stream.buffer.find(b"\n")
            if idx == -1:
                if len(stream.buffer) > self.cfg.max_line_bytes:
                    raw = bytes(stream.buffer)
                    stream.buffer.clear()
                    self.isolate(stream, raw, stream.buffer_start, TOO_LONG,
                                 lf_terminated=False)
                    stream.buffer_start = stream.offset
                return
            raw = bytes(stream.buffer[: idx + 1])
            del stream.buffer[: idx + 1]
            start = stream.buffer_start
            stream.buffer_start += len(raw)
            self.handle_line(stream, raw, start)

    def handle_line(self, stream: Stream, raw: bytes, start: int) -> None:
        if len(raw) > self.cfg.max_line_bytes:
            self.isolate(stream, raw, start, TOO_LONG, lf_terminated=True)
            return
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            self.isolate(stream, raw, start, BAD_UTF8, lf_terminated=True)
            return
        try:
            obj = json.loads(text)
        except json.JSONDecodeError:
            self.isolate(stream, raw, start, BAD_JSON, lf_terminated=True)
            return
        canonical = json.dumps(obj, ensure_ascii=False, sort_keys=True,
                               separators=(",", ":"))
        self.publish(stream, raw, start, canonical)

    def publish(self, stream: Stream, raw: bytes, start: int, canonical: str) -> None:
        entry = {
            "seq": self.seq,
            "generation": stream.generation,
            "dev": stream.dev, "inode": stream.inode,
            "paths": sorted(stream.aliases),
            "start_offset": start,
            "end_offset": start + len(raw),  # 含终止 LF
            "sha256": hashlib.sha256(raw).hexdigest(),
            "raw_base64": base64.b64encode(raw).decode("ascii"),
        }
        self.store.append_record(entry, canonical)
        stream.records += 1
        self.seq += 1

    def isolate(self, stream: Stream, raw: bytes, start: int, reason: str,
                lf_terminated: bool) -> None:
        self.store.append_invalid({
            "generation": stream.generation,
            "dev": stream.dev, "inode": stream.inode,
            "paths": sorted(stream.aliases),
            "start_offset": start,
            "end_offset": start + len(raw),
            "reason": reason,
            "lf_terminated": lf_terminated,
            "raw_base64": base64.b64encode(raw).decode("ascii"),
            "note": "坏行按完整行隔离，不影响后续行偏移",
        })
        stream.isolated += 1

    # ---------- 状态与报告 ----------

    def continuity_statement(self) -> str:
        if self.cfg.rotation_mode == "rename":
            return ("rename 模式：按设备号/inode 跟踪，每个流自偏移 0 连续采集，"
                    "stop_file 出现后已排空全部已知文件；各流字节范围 "
                    "[0, confirmed_offset) 连续无缺口，跨流记录按到达顺序交错编号。")
        return ("copytruncate 模式：每一代内部自偏移 0 连续采集；"
                "代际边界（见 discontinuities）无法证明连续——旧代确认偏移之后、"
                "截断/覆写之前可能存在的未读数据不可恢复也不可枚举；"
                "即使最终记录看似齐全，也不构成无丢失证明。")

    def snapshot(self) -> dict:
        return {
            "rotation_mode": self.cfg.rotation_mode,
            "log_dir": str(self.log_dir),
            "stop_seen": self.stop_seen,
            "finished": self.finished,
            "records": sum(s.records for s in self.streams),
            "isolated_lines": sum(s.isolated for s in self.streams),
            "streams": [{
                "generation": s.generation,
                "dev": s.dev, "inode": s.inode,
                "paths": sorted(s.aliases),
                "confirmed_offset": s.offset,
                "pending_tail_bytes": len(s.buffer),
                "pending_tail_preview_base64": base64.b64encode(
                    bytes(s.buffer[:32])).decode("ascii"),
                "closed": s.closed,
                "records": s.records,
                "isolated_lines": s.isolated,
            } for s in self.streams],
            "discontinuities": self.discontinuities,
            "discarded_tails": self.discarded_tails,
            "unterminated_tails": self.unterminated_tails,
            "events": self.events[-200:],
            "continuity_statement": self.continuity_statement(),
            "gap_policy": "记录表只含实际采集到的完整行；不依据日志业务 id 推断、"
                          "去重或填补缺口。",
        }


# ---------- 子命令 ----------


def cmd_run(args: argparse.Namespace) -> int:
    cfg = Config.load(args.config)
    state_dir = Path(args.state_dir)
    if (state_dir / "state.json").exists() or (state_dir / "records.jsonl").exists():
        raise SystemExit(f"状态目录 {state_dir} 非空；本采集器不做崩溃恢复，请使用全新目录。")
    collector = Collector(cfg, Path(args.log_dir), Path(args.stop_file), state_dir)
    rc = collector.run()
    print(json.dumps({
        "finished": collector.finished,
        "stop_seen": collector.stop_seen,
        "records": sum(s.records for s in collector.streams),
        "isolated_lines": sum(s.isolated for s in collector.streams),
        "generations": len(collector.streams),
        "discontinuities": len(collector.discontinuities),
        "unterminated_tails": len(collector.unterminated_tails),
    }, ensure_ascii=False))
    return rc


def cmd_status(args: argparse.Namespace) -> int:
    state_path = Path(args.state_dir) / "state.json"
    if not state_path.exists():
        raise SystemExit(f"{state_path} 不存在：采集器尚未运行或状态目录错误。")
    print(state_path.read_text(encoding="utf-8"))
    return 0


def cmd_export(args: argparse.Namespace) -> int:
    state_dir = Path(args.state_dir)
    state_path = state_dir / "state.json"
    if not state_path.exists():
        raise SystemExit(f"{state_path} 不存在：采集器尚未运行或状态目录错误。")
    state = json.loads(state_path.read_text(encoding="utf-8"))
    prefix = Path(args.out_prefix)
    prefix.parent.mkdir(parents=True, exist_ok=True)

    counts = {}
    for name in ("records", "canonical", "invalid"):
        src = state_dir / f"{name}.jsonl"
        text = src.read_text(encoding="utf-8") if src.exists() else ""
        counts[name] = len([ln for ln in text.splitlines() if ln.strip()])
        Path(str(prefix) + f".{name}.jsonl").write_text(text, encoding="utf-8")

    report = {
        "rotation_mode": state["rotation_mode"],
        "finished": state["finished"],
        "collected_records": counts["records"],
        "isolated_lines": counts["invalid"],
        "streams": state["streams"],
        "discontinuities": state["discontinuities"],
        "discarded_tails": state["discarded_tails"],
        "unterminated_tails": state["unterminated_tails"],
        "continuity_statement": state["continuity_statement"],
        "gap_policy": state["gap_policy"],
        "note": "records/canonical/invalid 三个文件按 seq/范围对应；raw_base64 为含终止 LF "
                "的原始字节，[start_offset, end_offset) 含终止 LF；canonical_json 为规范 "
                "JSON 文本，与原始字节分别保存。",
    }
    Path(str(prefix) + ".report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"exported": counts, "report": str(prefix) + ".report.json"},
                     ensure_ascii=False))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    p_run = sub.add_parser("run", help="单进程采集，直到 stop_file 出现后排空退出")
    p_run.add_argument("--config", required=True, help="含 rotation_mode 的配置 JSON")
    p_run.add_argument("--log-dir", required=True)
    p_run.add_argument("--stop-file", required=True)
    p_run.add_argument("--state-dir", required=True, help="采集输出目录（须为全新目录）")
    p_run.set_defaults(func=cmd_run)

    p_status = sub.add_parser("status", help="打印当前采集状态（JSON）")
    p_status.add_argument("--state-dir", required=True)
    p_status.set_defaults(func=cmd_status)

    p_export = sub.add_parser("export", help="导出记录、规范 JSON、隔离行与报告")
    p_export.add_argument("--state-dir", required=True)
    p_export.add_argument("--out-prefix", required=True)
    p_export.set_defaults(func=cmd_export)

    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
