"""命令行入口：read、resume、audit。

输出为 JSONL，每行一条样本记录：消费序号 seq、样本身份 sample_id、
各部件摘要（part/size/sha256/偏移）。不输出原始字节，不训练模型。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .audit import audit_checkpoint, audit_merge, audit_sequence
from .checkpoint import (
    build_checkpoint,
    load_checkpoint,
    resume_runner,
    save_checkpoint,
)
from .errors import WDSStreamError
from .manifest import load_config, load_manifest
from .shuffle import WorkerRunner


def _write_record(fh, record: dict[str, object]) -> None:
    fh.write(json.dumps(record, ensure_ascii=False, sort_keys=True))
    fh.write("\n")


def _run_worker(
    runner: WorkerRunner,
    output_path: Path,
    *,
    append: bool,
    stop_after: int | None,
    checkpoint_path: Path | None,
    manifest,
    config,
    worker_id: int,
) -> int:
    """驱动 runner；在输出第 N 条后保存 checkpoint 并正常退出。"""
    mode = "a" if append else "w"
    emitted_here = 0
    finished = True
    output_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with output_path.open(mode, encoding="utf-8") as out:
            for record in runner.run():
                _write_record(out, record)
                emitted_here += 1
                if stop_after is not None and runner.consumed >= stop_after:
                    if checkpoint_path is not None:
                        checkpoint = build_checkpoint(runner, manifest, config, worker_id)
                        save_checkpoint(checkpoint_path, checkpoint)
                    finished = False
                    break
    finally:
        runner.close()
    return emitted_here, finished


def cmd_read(args: argparse.Namespace) -> int:
    manifest = load_manifest(args.manifest)
    config = load_config(args.config)
    if args.worker_id >= config.worker_count:
        raise WDSStreamError(
            f"worker_id {args.worker_id} 超出 worker_count={config.worker_count}"
        )
    runner = WorkerRunner.fresh(manifest, config, args.worker_id)
    emitted, finished = _run_worker(
        runner,
        Path(args.output),
        append=False,
        stop_after=args.stop_after,
        checkpoint_path=Path(args.checkpoint) if args.checkpoint else None,
        manifest=manifest,
        config=config,
        worker_id=args.worker_id,
    )
    print(
        json.dumps(
            {
                "command": "read",
                "worker_id": args.worker_id,
                "emitted": emitted,
                "consumed_total": runner.consumed,
                "finished": finished,
                "checkpoint": args.checkpoint,
                "output": args.output,
            },
            ensure_ascii=False,
        )
    )
    return 0


def cmd_resume(args: argparse.Namespace) -> int:
    manifest = load_manifest(args.manifest)
    config = load_config(args.config)
    checkpoint = load_checkpoint(args.checkpoint)  # 先做自哈希校验
    runner = resume_runner(checkpoint, manifest, config, args.worker_id)
    previous_consumed = runner.consumed
    emitted, finished = _run_worker(
        runner,
        Path(args.output),
        append=args.append,
        stop_after=None,  # 续跑一直到源与缓冲全部清空
        checkpoint_path=None,
        manifest=manifest,
        config=config,
        worker_id=args.worker_id,
    )
    print(
        json.dumps(
            {
                "command": "resume",
                "worker_id": args.worker_id,
                "resumed_from_consumed": previous_consumed,
                "emitted": emitted,
                "consumed_total": runner.consumed,
                "finished": finished,
                "output": args.output,
            },
            ensure_ascii=False,
        )
    )
    return 0


def cmd_audit(args: argparse.Namespace) -> int:
    if args.audit_kind == "merge":
        manifest = load_manifest(args.manifest)
        config = load_config(args.config)
        result = audit_merge(manifest, config, args.outputs)
    elif args.audit_kind == "sequence":
        result = audit_sequence(args.full, args.resumed)
    elif args.audit_kind == "checkpoint":
        manifest = load_manifest(args.manifest)
        config = load_config(args.config)
        result = audit_checkpoint(args.checkpoint, manifest, config, args.worker_id)
    else:  # pragma: no cover - argparse 已限制取值
        raise WDSStreamError(f"未知 audit 类型 {args.audit_kind}")
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if result.get("ok") else 2


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="wds_stream",
        description="WebDataset tar 分片流式读取、有界洗牌与可恢复续跑（不训练模型）",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_read = sub.add_parser("read", help="从头运行单个 worker，可选在第 N 条后保存 checkpoint")
    p_read.add_argument("--config", required=True)
    p_read.add_argument("--manifest", required=True)
    p_read.add_argument("--worker-id", type=int, required=True)
    p_read.add_argument("--output", required=True)
    p_read.add_argument("--checkpoint", default=None)
    p_read.add_argument(
        "--stop-after",
        type=int,
        default=None,
        metavar="N",
        help="输出第 N 个样本后保存 checkpoint（需同时给 --checkpoint）并正常退出",
    )
    p_read.set_defaults(func=cmd_read)

    p_resume = sub.add_parser("resume", help="从 checkpoint 恢复 worker 并跑到结束")
    p_resume.add_argument("--config", required=True)
    p_resume.add_argument("--manifest", required=True)
    p_resume.add_argument("--worker-id", type=int, required=True)
    p_resume.add_argument("--checkpoint", required=True)
    p_resume.add_argument("--output", required=True, help="续跑输出文件（默认追加）")
    p_resume.add_argument("--no-append", dest="append", action="store_false")
    p_resume.set_defaults(func=cmd_resume, append=True)

    p_audit = sub.add_parser("audit", help="只读审计")
    audit_sub = p_audit.add_subparsers(dest="audit_kind", required=True)

    a_merge = audit_sub.add_parser("merge", help="合并所有 worker 输出，校验全覆盖且无重复")
    a_merge.add_argument("--config", required=True)
    a_merge.add_argument("--manifest", required=True)
    a_merge.add_argument("--outputs", nargs="+", required=True)

    a_seq = audit_sub.add_parser(
        "sequence", help="不中断序列与保存后恢复序列逐项一致性"
    )
    a_seq.add_argument("--full", required=True)
    a_seq.add_argument("--resumed", required=True)

    a_ckpt = audit_sub.add_parser("checkpoint", help="checkpoint 自哈希与绑定核验")
    a_ckpt.add_argument("--config", required=True)
    a_ckpt.add_argument("--manifest", required=True)
    a_ckpt.add_argument("--checkpoint", required=True)
    a_ckpt.add_argument("--worker-id", type=int, required=True)

    for p in (a_merge, a_seq, a_ckpt):
        p.set_defaults(func=cmd_audit)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except WDSStreamError as exc:
        print(f"ERROR[{type(exc).__name__}]: {exc}", file=sys.stderr)
        return 1
    except FileNotFoundError as exc:
        print(f"ERROR[FileNotFoundError]: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
