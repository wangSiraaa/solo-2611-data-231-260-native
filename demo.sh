#!/usr/bin/env bash
# 端到端演示：B=3、worker_count=2、长短部件、同 key 跨 shard、
# 缺件/重复部件失败、checkpoint 续跑等价、绑定失效拒绝。
#
# 固定正常输入（fixtures/）只读不改；故障与绑定演示全部在 output/demo 的副本里进行。
set -u -o pipefail

ROOT="$(cd "$(dirname "$0")" && pwd)"
cd "$ROOT"
PY=python3
CFG=fixtures/config.json
MAN=fixtures/manifest.json
OUT=output/demo
rm -rf "$OUT"
mkdir -p "$OUT/bad" "$OUT/bind"

banner() { printf '\n========== %s ==========\n' "$1"; }
jkeys() { python3 -c 'import json,sys; [print(json.loads(l)["sample_id"], "seq="+str(json.loads(l)["seq"])) for l in open(sys.argv[1]) if l.strip()]' "$1"; }

banner "1. 配置与派发：B=3, worker_count=2, shard 序号取模"
python3 - <<'PY'
from wds_stream.manifest import load_manifest, load_config, worker_seed
m = load_manifest("fixtures/manifest.json")
c = load_config("fixtures/config.json")
for w in range(c.worker_count):
    shards = m.shards_for_worker(w, c.worker_count)
    print(f"worker {w}: shards={[s.shard_id for s in shards]} "
          f"derived_seed={worker_seed(c.seed, c.epoch, w)}")
print("规定部件:", list(c.parts), "| 单成员上限:", c.max_member_bytes)
PY

banner "2. 不中断全量运行（两个 worker 各自独立进程，互不读取对方 shard）"
$PY -m wds_stream read --config "$CFG" --manifest "$MAN" --worker-id 0 \
    --output "$OUT/full_w0.jsonl"
$PY -m wds_stream read --config "$CFG" --manifest "$MAN" --worker-id 1 \
    --output "$OUT/full_w1.jsonl"
echo "-- worker0 消费次序（注意 0/shared 与 2/shared 是两个身份，未被 key 合并）--"
jkeys "$OUT/full_w0.jsonl"
echo "-- worker1 消费次序 --"
jkeys "$OUT/full_w1.jsonl"

banner "3. 样本身份与部件摘要（长短部件：png=76B, json=45B, txt=18/20B）"
python3 - <<'PY'
import json
seen = set()
for path in ["output/demo/full_w0.jsonl", "output/demo/full_w1.jsonl"]:
    for line in open(path, encoding="utf-8"):
        r = json.loads(line)
        if r["sample_id"] in seen:
            continue
        seen.add(r["sample_id"])
        if r["key"] == "shared" and r["shard_id"] in ("0", "1"):
            print(json.dumps(r, ensure_ascii=False, indent=2))
print("同 key=shared 的不同身份:", sorted(x for x in seen if x.endswith("/shared")))
PY

banner "4. 在第 N=3 条后保存 checkpoint 并正常退出，再 resume 续跑"
$PY -m wds_stream read --config "$CFG" --manifest "$MAN" --worker-id 0 \
    --output "$OUT/part_w0.jsonl" --checkpoint "$OUT/w0.n3.ckpt" --stop-after 3
echo "-- checkpoint 绑定与运行态 --"
python3 - <<'PY'
import json
ck = json.load(open("output/demo/w0.n3.ckpt", encoding="utf-8"))
print("binding:", json.dumps(ck["binding"], ensure_ascii=False, indent=2))
st = ck["worker_state"]
print("consumed:", st["consumed"], "| 下一源位置:", st["position"],
      "| 缓冲样本:", [b["sample_id"] for b in st["buffer"]])
PY
$PY -m wds_stream resume --config "$CFG" --manifest "$MAN" --worker-id 0 \
    --checkpoint "$OUT/w0.n3.ckpt" --output "$OUT/part_w0.jsonl"
echo "-- 中断+续跑拼接后的消费次序 --"
jkeys "$OUT/part_w0.jsonl"

banner "5. 审计：续跑序列与不中断序列逐项完全相同"
$PY -m wds_stream audit sequence --full "$OUT/full_w0.jsonl" --resumed "$OUT/part_w0.jsonl"

banner "6. 审计：独立合并两个 worker 输出，恰好覆盖全部 12 个合法样本且各一次"
$PY -m wds_stream audit merge --config "$CFG" --manifest "$MAN" \
    --outputs "$OUT/part_w0.jsonl" "$OUT/full_w1.jsonl"

# ---------- 故障输入：为独立故障 tar 建单项临时 manifest ----------
make_bad_manifest() {  # $1=tar 名
  python3 - "$1" <<'PY'
import hashlib, json, sys, pathlib
name = sys.argv[1]
src = pathlib.Path("fixtures") / name
dst = pathlib.Path("output/demo/bad") / name
dst.write_bytes(src.read_bytes())
doc = [{"shard_id": "bad", "path": name,
        "sha256": hashlib.sha256(dst.read_bytes()).hexdigest()}]
pathlib.Path("output/demo/bad/manifest.json").write_text(
    json.dumps(doc, ensure_ascii=False), encoding="utf-8")
PY
  cp "$CFG" "$OUT/bad/config.json"
}

banner "7a. 缺件失败（missing.tar：bad 样本只有 png/json，缺 txt）"
make_bad_manifest missing.tar
$PY -m wds_stream read --config "$OUT/bad/config.json" \
    --manifest "$OUT/bad/manifest.json" --worker-id 0 \
    --output "$OUT/bad/missing.out.jsonl" || echo "[已按预期失败，退出码=$?]"

banner "7b. 重复部件失败（duplicate.tar：bad.png 出现两次）"
make_bad_manifest duplicate.tar
$PY -m wds_stream read --config "$OUT/bad/config.json" \
    --manifest "$OUT/bad/manifest.json" --worker-id 0 \
    --output "$OUT/bad/duplicate.out.jsonl" || echo "[已按预期失败，退出码=$?]"

banner "7c. 非法路径 / 超大小失败（现场合成小 shard，不碰固定输入）"
python3 - <<'PY'
import hashlib, io, json, pathlib, tarfile
out = pathlib.Path("output/demo/bad")
def write_tar(name, members):
    with tarfile.open(out / name, "w", format=tarfile.USTAR_FORMAT) as tf:
        for n, d in members:
            ti = tarfile.TarInfo(n); ti.size = len(d)
            tf.addfile(ti, io.BytesIO(d))
# 非法路径：含目录分隔
write_tar("illegal.tar", [("sub/k.png", b"x"), ("k.json", b"{}"), ("k.txt", b"t")])
# 超大小：10B 成员，上限 5B
write_tar("big.tar", [("k.png", b"x" * 10), ("k.json", b"{}"), ("k.txt", b"t")])
for name in ("illegal", "big"):
    p = out / f"{name}.tar"
    doc = [{"shard_id": "bad", "path": p.name,
            "sha256": hashlib.sha256(p.read_bytes()).hexdigest()}]
    (out / f"manifest_{name}.json").write_text(json.dumps(doc), encoding="utf-8")
cfg = json.loads(pathlib.Path("fixtures/config.json").read_text(encoding="utf-8"))
pathlib.Path(out / "config_tiny.json").write_text(
    json.dumps({**cfg, "max_member_bytes": 5}), encoding="utf-8")
PY
$PY -m wds_stream read --config "$OUT/bad/config.json" \
    --manifest "$OUT/bad/manifest_illegal.json" --worker-id 0 \
    --output "$OUT/bad/illegal.out.jsonl" || echo "[已按预期失败，退出码=$?]"
$PY -m wds_stream read --config "$OUT/bad/config_tiny.json" \
    --manifest "$OUT/bad/manifest_big.json" --worker-id 0 \
    --output "$OUT/bad/big.out.jsonl" || echo "[已按预期失败，退出码=$?]"

# ---------- checkpoint 绑定失效：全部在副本上操作 ----------
banner "8. checkpoint 绑定失效演示（config 变动 / 源替换 / 自哈希损坏）"
cp -r fixtures "$OUT/bind/fixtures"
BIND_CFG="$OUT/bind/fixtures/config.json"
BIND_MAN="$OUT/bind/fixtures/manifest.json"
$PY -m wds_stream read --config "$BIND_CFG" --manifest "$BIND_MAN" --worker-id 0 \
    --output "$OUT/bind/w0.jsonl" --checkpoint "$OUT/bind/w0.ckpt" --stop-after 3 >/dev/null
echo "-- 8a. 原始 checkpoint 审计通过 --"
$PY -m wds_stream audit checkpoint --config "$BIND_CFG" --manifest "$BIND_MAN" \
    --checkpoint "$OUT/bind/w0.ckpt" --worker-id 0

echo "-- 8b. 修改 config（seed 42 -> 7）后拒绝沿用 --"
python3 - <<'PY'
import json, pathlib
p = pathlib.Path("output/demo/bind/fixtures/config.json")
cfg = json.loads(p.read_text(encoding="utf-8")); cfg["seed"] = 7
p.write_text(json.dumps(cfg), encoding="utf-8")
PY
$PY -m wds_stream resume --config "$BIND_CFG" --manifest "$BIND_MAN" --worker-id 0 \
    --checkpoint "$OUT/bind/w0.ckpt" --output "$OUT/bind/x.jsonl" \
    || echo "[已按预期拒绝，退出码=$?]"

echo "-- 8c. 源 shard 被替换（同步更新 manifest 哈希）后拒绝沿用 --"
python3 - <<'PY'
import hashlib, io, json, pathlib, tarfile
base = pathlib.Path("output/demo/bind/fixtures")
p = base / "shards/0.tar"
with tarfile.open(p, "a") as tf:
    ti = tarfile.TarInfo("tampered.txt"); ti.size = 1
    tf.addfile(ti, io.BytesIO(b"z"))
man = json.loads((base / "manifest.json").read_text(encoding="utf-8"))
man[0]["sha256"] = hashlib.sha256(p.read_bytes()).hexdigest()
(base / "manifest.json").write_text(json.dumps(man), encoding="utf-8")
# 恢复 config 以便隔离变量：只剩源变动
cfg = json.loads((base / "config.json").read_text(encoding="utf-8")); cfg["seed"] = 42
(base / "config.json").write_text(json.dumps(cfg), encoding="utf-8")
PY
$PY -m wds_stream resume --config "$BIND_CFG" --manifest "$BIND_MAN" --worker-id 0 \
    --checkpoint "$OUT/bind/w0.ckpt" --output "$OUT/bind/x2.jsonl" \
    || echo "[已按预期拒绝，退出码=$?]"

echo "-- 8d. checkpoint 文件被直接篡改（自哈希不匹配）后拒绝 --"
python3 - <<'PY'
import json, pathlib
p = pathlib.Path("output/demo/bind/w0.ckpt")
doc = json.loads(p.read_text(encoding="utf-8"))
doc["worker_state"]["consumed"] = 999
p.write_text(json.dumps(doc), encoding="utf-8")
PY
$PY -m wds_stream audit checkpoint --config "$CFG" --manifest "$MAN" \
    --checkpoint "$OUT/bind/w0.ckpt" --worker-id 0 \
    || echo "[已按预期拒绝，退出码=$?]"

banner "全部演示完成"
