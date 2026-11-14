#!/usr/bin/env python3.11
"""
Strict projection of multi-unit text annotations onto BILOU token labels.

Input contract (all provided, no tokenizer / NER model is run):
  * the original text (never normalized -- coordinates would break),
  * tokens: position-ordered, non-overlapping; ``piece_base64`` must decode to
    exactly the original bytes at ``offset``; a token may hold only part of a
    UTF-8 character; special tokens carry ``offset = null``,
  * annotations: each declares ``unit`` in {utf8, codepoint, utf16}, a
    half-open [start, end) range, and the SHA256 of the original UTF-8 text.

Projection rules:
  * every offset is converted to an original UTF-8 byte boundary; utf16 offsets
    may not split a surrogate pair, utf8 offsets may not split a multi-byte
    codepoint; conversion is cross-checked by an independent prefix-encoding
    computation;
  * an annotation is representable iff both ends land on token boundaries, the
    in-span bytes not covered by tokens are all original whitespace, at least
    one token is covered, and no offset=null special token sits between its
    first and last covered token.  Anything else is ``unrepresentable`` --
    never silently expanded to whole tokens;
  * per layer, representable entities that share a token are ALL marked
    ``conflict`` (no input-order winner); invalid / unrepresentable /
    conflicted annotations never displace other entities;
  * labels: U = single-token entity, B/I/L = multi-token entity, O = anything
    else (including special tokens).  Layers are independent.

Output: token table, per-layer label sequences (+ entities recovered back from
the labels and resolved against the original text), per-annotation status with
source mapping (original unit and range preserved), and conflict records.
The result does not depend on the input order of annotations.
"""

from __future__ import annotations

import base64
import bisect
import hashlib
import json
import random
from dataclasses import dataclass

UNITS = ("utf8", "codepoint", "utf16")


# ---------------------------------------------------------------------------
# Coordinate maps: annotation unit -> original UTF-8 byte offset
# ---------------------------------------------------------------------------

class CoordinateError(ValueError):
    """An annotation offset cannot be mapped to an original UTF-8 byte boundary."""


@dataclass(frozen=True)
class CoordinateMaps:
    text: str
    data: bytes                      # text.encode("utf-8")
    cp_to_byte: tuple[int, ...]      # codepoint index -> byte offset (len n_cp + 1)
    u16_to_byte: dict[int, int]      # valid utf16 code-unit offset -> byte offset
    byte_boundaries: frozenset[int]  # byte offsets that start a codepoint
    n_cp: int
    n_u16: int

    @property
    def n_bytes(self) -> int:
        return len(self.data)


def build_coordinate_maps(text: str) -> CoordinateMaps:
    data = text.encode("utf-8")
    cp_to_byte = [0]
    u16_to_byte = {0: 0}
    bpos = upos = 0
    for ch in text:
        bpos += len(ch.encode("utf-8"))
        upos += 2 if ord(ch) > 0xFFFF else 1
        cp_to_byte.append(bpos)
        u16_to_byte[upos] = bpos
    return CoordinateMaps(
        text=text,
        data=data,
        cp_to_byte=tuple(cp_to_byte),
        u16_to_byte=u16_to_byte,
        byte_boundaries=frozenset(cp_to_byte),
        n_cp=len(text),
        n_u16=upos,
    )


def _is_int(x) -> bool:
    return isinstance(x, int) and not isinstance(x, bool)


def convert_offset(unit: str, offset: int, maps: CoordinateMaps) -> int:
    """Convert one annotation offset to an original UTF-8 byte offset."""
    if not _is_int(offset):
        raise CoordinateError("offset_not_integer")
    if offset < 0:
        raise CoordinateError("offset_out_of_range")
    if unit == "utf8":
        if offset > maps.n_bytes:
            raise CoordinateError("offset_out_of_range")
        if offset not in maps.byte_boundaries:
            raise CoordinateError("utf8_offset_mid_codepoint")
        return offset
    if unit == "codepoint":
        if offset > maps.n_cp:
            raise CoordinateError("offset_out_of_range")
        return maps.cp_to_byte[offset]
    if unit == "utf16":
        if offset > maps.n_u16:
            raise CoordinateError("offset_out_of_range")
        try:
            return maps.u16_to_byte[offset]
        except KeyError:
            raise CoordinateError("utf16_offset_mid_surrogate_pair") from None
    raise CoordinateError("unknown_unit")


def independent_unit_check(text: str, unit: str, offset: int, byte_offset: int) -> bool:
    """Re-derive ``byte_offset`` from ``offset`` using only prefix encodings of
    the original text -- shares no state with :func:`build_coordinate_maps`."""
    data = text.encode("utf-8")
    if unit == "utf8":
        if offset != byte_offset or not (0 <= offset <= len(data)):
            return False
        try:
            data[:offset].decode("utf-8")  # strict: a mid-codepoint cut fails
        except UnicodeDecodeError:
            return False
        return True
    if unit == "codepoint":
        if not (0 <= offset <= len(text)):
            return False
        return len(text[:offset].encode("utf-8")) == byte_offset
    if unit == "utf16":
        for i in range(len(text) + 1):
            units = len(text[:i].encode("utf-16-le")) // 2
            if units == offset:
                return len(text[:i].encode("utf-8")) == byte_offset
            if units > offset:
                return False  # offset fell inside a surrogate pair
        return False
    return False


def verify_coordinate_maps_independent(maps: CoordinateMaps) -> None:
    """Whole-map cross-check of every unit via prefix encodings."""
    text, data = maps.text, maps.data
    assert len(data) == maps.n_bytes
    assert len(text.encode("utf-16-le")) // 2 == maps.n_u16
    assert maps.byte_boundaries == frozenset(maps.cp_to_byte)
    for i in range(maps.n_cp + 1):
        assert len(text[:i].encode("utf-8")) == maps.cp_to_byte[i]
        u = len(text[:i].encode("utf-16-le")) // 2
        assert maps.u16_to_byte[u] == maps.cp_to_byte[i]
    for b in maps.byte_boundaries:
        data[:b].decode("utf-8")  # every byte boundary is codepoint-aligned


# ---------------------------------------------------------------------------
# Tokens
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class TokenRec:
    index: int                     # position in the token sequence
    special: bool                  # offset is null
    start: int | None
    end: int | None
    piece_base64: str
    piece: bytes


def validate_tokens(maps: CoordinateMaps, tokens: list[dict]) -> list[TokenRec]:
    recs: list[TokenRec] = []
    for i, tok in enumerate(tokens):
        piece_b64 = tok["piece_base64"]
        try:
            piece = base64.b64decode(piece_b64, validate=True)
        except Exception as exc:
            raise ValueError(f"token[{i}]: piece_base64 is not valid base64") from exc
        off = tok.get("offset", None)
        if off is None:
            recs.append(TokenRec(i, True, None, None, piece_b64, piece))
            continue
        s, e = off
        if not (_is_int(s) and _is_int(e) and 0 <= s < e <= maps.n_bytes):
            raise ValueError(f"token[{i}]: invalid offset {off!r}")
        if maps.data[s:e] != piece:
            raise ValueError(
                f"token[{i}]: piece_base64 does not equal the original bytes at [{s},{e})"
            )
        recs.append(TokenRec(i, False, s, e, piece_b64, piece))
    pos = [r for r in recs if not r.special]
    for a, b in zip(pos, pos[1:]):
        if b.start < a.end:
            raise ValueError(
                "tokens must be position-ordered and non-overlapping: "
                f"token[{a.index}] ends at {a.end}, token[{b.index}] starts at {b.start}"
            )
    return recs


def gap_is_whitespace(maps: CoordinateMaps, gs: int, ge: int) -> bool:
    """True iff every original codepoint touched by the byte gap [gs, ge) is
    whitespace.  Token edges may split a multi-byte character, so the check
    runs against the enclosing codepoints, not the raw gap bytes."""
    if gs >= ge:
        return True
    i = bisect.bisect_right(maps.cp_to_byte, gs) - 1
    while i < maps.n_cp and maps.cp_to_byte[i] < ge:
        if maps.cp_to_byte[i + 1] > gs and not maps.text[i].isspace():
            return False
        i += 1
    return True


# ---------------------------------------------------------------------------
# Projection
# ---------------------------------------------------------------------------

def _locate(pos: list[TokenRec], b: int) -> str:
    """Where byte offset ``b`` sits relative to the positional tokens."""
    starts = [r.start for r in pos]
    i = bisect.bisect_right(starts, b) - 1
    if i >= 0 and pos[i].start <= b < pos[i].end:
        return "inside_token"
    return "in_gap"


def _project_one(maps, pos, edges, tokrecs, ann, sha) -> dict:
    rec = {
        "annotation_id": ann.get("annotation_id"),
        "layer": ann.get("layer"),
        "label": ann.get("label", "ENT"),
        "unit": ann.get("unit"),
        "start": ann.get("start"),
        "end": ann.get("end"),
        "byte_start": None,
        "byte_end": None,
        "token_indices": [],
        "source_text": None,
        "covered_text": None,
        "status": None,
        "reason": None,
        "conflicts_with": [],
    }

    def fail(status: str, reason: str) -> dict:
        rec["status"], rec["reason"] = status, reason
        return rec

    if rec["annotation_id"] is None or rec["layer"] is None:
        return fail("invalid", "missing_field")
    unit, s, e = rec["unit"], rec["start"], rec["end"]
    if unit not in UNITS:
        return fail("invalid", "unknown_unit")
    if not _is_int(s) or not _is_int(e):
        return fail("invalid", "offset_not_integer")
    if s > e:
        return fail("invalid", "start_after_end")
    if ann.get("text_sha256") != sha:
        return fail("invalid", "sha256_mismatch")
    try:
        bs = convert_offset(unit, s, maps)
        be = convert_offset(unit, e, maps)
    except CoordinateError as ce:
        return fail("invalid", str(ce))
    rec["byte_start"], rec["byte_end"] = bs, be
    rec["source_text"] = maps.data[bs:be].decode("utf-8")
    # independent cross-check of the unit conversion (codepoint prefix encoding)
    assert independent_unit_check(maps.text, unit, s, bs), f"unit conversion mismatch: {ann!r}"
    assert independent_unit_check(maps.text, unit, e, be), f"unit conversion mismatch: {ann!r}"

    if bs == be:
        return fail("unrepresentable", "no_tokens_covered")
    if bs not in edges:
        return fail("unrepresentable",
                    "start_inside_token" if _locate(pos, bs) == "inside_token"
                    else "start_not_on_token_boundary")
    if be not in edges:
        return fail("unrepresentable",
                    "end_inside_token" if _locate(pos, be) == "inside_token"
                    else "end_not_on_token_boundary")
    covered = [r for r in pos if bs <= r.start and r.end <= be]
    if not covered:
        return fail("unrepresentable", "no_tokens_covered")
    cursor = bs
    for r in covered:
        if not gap_is_whitespace(maps, cursor, r.start):
            return fail("unrepresentable", "non_whitespace_gap")
        cursor = r.end
    if not gap_is_whitespace(maps, cursor, be):
        return fail("unrepresentable", "non_whitespace_gap")
    first, last = covered[0].index, covered[-1].index
    if any(tokrecs[k].special for k in range(first, last + 1)):
        return fail("unrepresentable", "special_token_inside_span")
    rec["token_indices"] = [r.index for r in covered]
    rec["status"] = "representable"
    return rec


def bilou_decode(labels: list[str]) -> list[tuple[int, int, str]]:
    """Recover (first_token, last_token + 1, label) spans from a BILOU sequence."""
    spans = []
    i, n = 0, len(labels)
    while i < n:
        tag = labels[i]
        if tag == "O":
            i += 1
            continue
        prefix, sep, lab = tag.partition("-")
        if not sep:
            raise ValueError(f"malformed BILOU tag {tag!r} at {i}")
        if prefix == "U":
            spans.append((i, i + 1, lab))
            i += 1
        elif prefix == "B":
            j = i + 1
            while j < n and labels[j] == f"I-{lab}":
                j += 1
            if j >= n or labels[j] != f"L-{lab}":
                raise ValueError(f"B-{lab} at {i} is not closed by L-{lab}")
            spans.append((i, j + 1, lab))
            i = j + 1
        else:
            raise ValueError(f"malformed BILOU tag {tag!r} at {i}")
    return spans


def verify_roundtrip(maps, tokrecs, layers, recs) -> None:
    """Recover every accepted entity from the label sequence alone and resolve
    it back against the original bytes."""
    for layer, labels in layers.items():
        spans = bilou_decode(labels)
        accepted = [r for r in recs if r["layer"] == layer and r["status"] == "accepted"]
        assert len(spans) == len(accepted), f"layer {layer}: label/entity count mismatch"
        span_set = set(spans)
        for r in accepted:
            idx = r["token_indices"]
            key = (idx[0], idx[-1] + 1, r["label"])
            assert key in span_set, f"layer {layer}: {r['annotation_id']} not recoverable"
            first, last = tokrecs[idx[0]], tokrecs[idx[-1]]
            bs, be = r["byte_start"], r["byte_end"]
            assert bs <= first.start and last.end <= be
            # everything the labels skip inside the span is original whitespace
            assert gap_is_whitespace(maps, bs, first.start)
            assert gap_is_whitespace(maps, last.end, be)
            for a, b in zip(idx, idx[1:]):
                assert gap_is_whitespace(maps, tokrecs[a].end, tokrecs[b].start)
            # token pieces are exactly the original bytes at those offsets
            for k in idx:
                assert maps.data[tokrecs[k].start:tokrecs[k].end] == tokrecs[k].piece
        covered = {k for r in accepted for k in r["token_indices"]}
        for t in tokrecs:
            if t.special or t.index not in covered:
                assert labels[t.index] == "O"


def project(text: str, tokens: list[dict], annotations: list[dict]) -> dict:
    maps = build_coordinate_maps(text)
    sha = hashlib.sha256(maps.data).hexdigest()
    tokrecs = validate_tokens(maps, tokens)
    pos = [r for r in tokrecs if not r.special]
    edges = frozenset({r.start for r in pos} | {r.end for r in pos})

    recs = [_project_one(maps, pos, edges, tokrecs, a, sha) for a in annotations]
    # deterministic output order, independent of input annotation order
    recs.sort(key=lambda r: (
        str(r["layer"]), str(r["unit"]),
        r["start"] if _is_int(r["start"]) else -1,
        r["end"] if _is_int(r["end"]) else -1,
        str(r["annotation_id"]),
    ))
    for r in recs:
        if r["token_indices"]:
            f, l = r["token_indices"][0], r["token_indices"][-1]
            r["covered_text"] = maps.data[tokrecs[f].start:tokrecs[l].end].decode(
                "utf-8", errors="replace")

    layers: dict[str, list[str]] = {}
    conflicts: list[dict] = []
    for layer in sorted({r["layer"] for r in recs}):
        labels = ["O"] * len(tokrecs)
        rep = [r for r in recs if r["layer"] == layer and r["status"] == "representable"]
        tok2ids: dict[int, list[str]] = {}
        for r in rep:
            for ti in r["token_indices"]:
                tok2ids.setdefault(ti, []).append(r["annotation_id"])
        conflicted = set()
        for ids in tok2ids.values():
            if len(ids) > 1:
                conflicted.update(ids)
        for r in rep:
            if r["annotation_id"] in conflicted:
                others = sorted({i for ti in r["token_indices"]
                                 for i in tok2ids[ti] if i != r["annotation_id"]})
                r["status"] = "conflict"
                r["reason"] = "overlapping_entities_same_layer"
                r["conflicts_with"] = others
                conflicts.append({
                    "layer": layer,
                    "annotation_id": r["annotation_id"],
                    "unit": r["unit"], "start": r["start"], "end": r["end"],
                    "byte_start": r["byte_start"], "byte_end": r["byte_end"],
                    "token_indices": r["token_indices"],
                    "conflicts_with": others,
                })
        for r in rep:
            if r["status"] != "representable":
                continue
            r["status"] = "accepted"
            idx, lab = r["token_indices"], r["label"]
            if len(idx) == 1:
                labels[idx[0]] = f"U-{lab}"
            else:
                labels[idx[0]] = f"B-{lab}"
                for k in idx[1:-1]:
                    labels[k] = f"I-{lab}"
                labels[idx[-1]] = f"L-{lab}"
        layers[layer] = labels
    conflicts.sort(key=lambda c: (c["layer"], str(c["annotation_id"])))

    verify_roundtrip(maps, tokrecs, layers, recs)

    token_table = [{
        "index": r.index,
        "offset": None if r.special else [r.start, r.end],
        "piece_base64": r.piece_base64,
        "special": r.special,
        "text": None if r.special else r.piece.decode("utf-8", errors="replace"),
    } for r in tokrecs]
    layer_out = {}
    for layer, labels in layers.items():
        entities = [{
            "token_start": s, "token_end": e, "label": lab,
            "token_indices": list(range(s, e)),
            "text": maps.data[tokrecs[s].start:tokrecs[e - 1].end].decode(
                "utf-8", errors="replace"),
        } for s, e, lab in bilou_decode(labels)]
        layer_out[layer] = {"labels": labels, "entities": entities}
    return {
        "text_sha256": sha,
        "tokens": token_table,
        "layers": layer_out,
        "annotations": recs,
        "conflicts": conflicts,
    }


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def _tok(start: int, end: int, piece: bytes) -> dict:
    return {"offset": [start, end],
            "piece_base64": base64.b64encode(piece).decode("ascii")}


def _special(piece: bytes) -> dict:
    return {"offset": None, "piece_base64": base64.b64encode(piece).decode("ascii")}


def _ann(aid, layer, unit, start, end, sha, label="ENT"):
    return {"annotation_id": aid, "layer": layer, "unit": unit,
            "start": start, "end": end, "label": label, "text_sha256": sha}


def _by_id(res):
    return {a["annotation_id"]: a for a in res["annotations"]}


def test_unit_conversion_independent():
    for text in ("ok 😀 yes", "café", "café", "ha ha", "àb😀́"):
        verify_coordinate_maps_independent(build_coordinate_maps(text))
    # emoji: 1 codepoint = 2 utf16 units = 4 utf8 bytes
    assert independent_unit_check("😀", "utf16", 2, 4)
    assert not independent_unit_check("😀", "utf16", 1, 2)   # mid surrogate pair
    assert not independent_unit_check("😀", "utf8", 1, 1)    # mid codepoint
    assert independent_unit_check("😀", "codepoint", 1, 4)
    # precomposed é (2 bytes) vs decomposed e + ́ (1 + 2 bytes): no normalization
    pre, de = "é", "é"
    assert pre != de and len(pre) == 1 and len(de) == 2
    assert independent_unit_check(pre, "codepoint", 1, 2)
    assert independent_unit_check(de, "codepoint", 2, 3)
    assert not independent_unit_check(de, "codepoint", 1, 2)  # would need normalization
    print("ok  test_unit_conversion_independent")


def test_emoji_units_and_conflicts():
    text = "ok 😀 yes"
    data = text.encode("utf-8")
    sha = hashlib.sha256(data).hexdigest()
    # bytes: o0 k1 ' '2 | F0 9F 98 80 (3..6) | ' '7 | y8 e9 s10
    tokens = [_special(b"[CLS]"), _tok(0, 2, b"ok"),
              _tok(3, 5, data[3:5]), _tok(5, 7, data[5:7]),  # emoji split mid-character
              _tok(8, 11, b"yes"), _special(b"[SEP]")]
    anns = [
        _ann("u16", "units", "utf16", 3, 5, sha, "EMOJI"),      # surrogate pair span
        _ann("cp", "units", "codepoint", 3, 4, sha, "EMOJI"),
        _ann("u8", "units", "utf8", 3, 7, sha, "EMOJI"),
        _ann("emo", "emo", "utf16", 3, 5, sha, "EMOJI"),        # cross-token emoji
        _ann("bad16", "word", "utf16", 4, 5, sha),              # mid surrogate pair
        _ann("bad8", "word", "utf8", 4, 7, sha),                # mid codepoint
        _ann("yes", "word", "codepoint", 5, 8, sha, "WORD"),
        _ann("partial", "word", "codepoint", 1, 4, sha),        # cuts token "ok"
        _ann("space", "word", "codepoint", 2, 3, sha),          # covers no token
        _ann("full", "full", "codepoint", 0, 8, sha, "SENT"),   # whitespace gaps only
        _ann("ws", "gap", "codepoint", 2, 5, sha, "EMOJI"),     # " 😀 " incl. spaces
    ]
    res = project(text, tokens, anns)
    by_id = _by_id(res)

    # the three unit systems converge on the same bytes/tokens
    for aid in ("u16", "cp", "u8"):
        assert by_id[aid]["byte_start"] == 3 and by_id[aid]["byte_end"] == 7
        assert by_id[aid]["token_indices"] == [2, 3]
        assert by_id[aid]["status"] == "conflict"               # same layer, all marked
        assert set(by_id[aid]["conflicts_with"]) == {"u16", "cp", "u8"} - {aid}
    assert res["layers"]["units"]["labels"] == ["O"] * 6
    assert len(res["conflicts"]) == 3
    c0 = res["conflicts"][0]
    assert c0["token_indices"] == [2, 3] and c0["byte_start"] == 3 and c0["byte_end"] == 7

    assert by_id["emo"]["status"] == "accepted"
    assert res["layers"]["emo"]["labels"] == ["O", "O", "B-EMOJI", "L-EMOJI", "O", "O"]
    assert by_id["bad16"]["status"] == "invalid"
    assert by_id["bad16"]["reason"] == "utf16_offset_mid_surrogate_pair"
    assert by_id["bad8"]["reason"] == "utf8_offset_mid_codepoint"
    assert by_id["yes"]["status"] == "accepted" and by_id["yes"]["token_indices"] == [4]
    assert res["layers"]["word"]["labels"] == ["O", "O", "O", "O", "U-WORD", "O"]
    assert by_id["partial"]["status"] == "unrepresentable"
    assert by_id["partial"]["reason"] == "start_inside_token"
    assert by_id["space"]["reason"] == "no_tokens_covered"
    assert by_id["full"]["status"] == "accepted"
    assert res["layers"]["full"]["labels"] == \
        ["O", "B-SENT", "I-SENT", "I-SENT", "L-SENT", "O"]
    assert by_id["ws"]["status"] == "accepted"          # whitespace-only gaps are lossless
    assert by_id["ws"]["token_indices"] == [2, 3]
    assert by_id["ws"]["source_text"] == " 😀 " and by_id["ws"]["covered_text"] == "😀"
    # special tokens are always O and token pieces check out against the original
    assert res["tokens"][0]["special"] and res["tokens"][0]["offset"] is None
    assert base64.b64decode(res["tokens"][2]["piece_base64"]) == data[3:5]
    print("ok  test_emoji_units_and_conflicts")


def test_accents_no_normalization_and_gaps():
    # --- precomposed café, é split across two partial-byte tokens
    text = "café"
    data = text.encode("utf-8")           # c a f C3 A9
    sha = hashlib.sha256(data).hexdigest()
    tokens = [_tok(0, 3, b"caf"), _tok(3, 4, data[3:4]), _tok(4, 5, data[4:5])]
    anns = [
        _ann("e_acute", "ner", "codepoint", 3, 4, sha, "ACC"),  # é across 2 tokens
        _ann("caf", "ner", "utf8", 0, 3, sha, "CAF"),
        _ann("mid", "ner", "utf8", 3, 4, sha),                  # byte 4 splits é
        _ann("full", "full", "codepoint", 0, 4, sha, "SENT"),
        _ann("badsha", "ner", "codepoint", 0, 4,
             hashlib.sha256("café".encode("utf-8")).hexdigest()),
        _ann("badunit", "ner", "utf32", 0, 1, sha),
        _ann("reversed", "ner", "codepoint", 3, 1, sha),
        _ann("oor", "ner", "codepoint", 0, 99, sha),
    ]
    res = project(text, tokens, anns)
    by_id = _by_id(res)
    assert by_id["e_acute"]["status"] == "accepted"
    assert by_id["e_acute"]["token_indices"] == [1, 2]
    assert res["layers"]["ner"]["labels"] == ["U-CAF", "B-ACC", "L-ACC"]
    assert by_id["mid"]["reason"] == "utf8_offset_mid_codepoint"
    assert res["layers"]["full"]["labels"] == ["B-SENT", "I-SENT", "L-SENT"]
    assert by_id["badsha"]["reason"] == "sha256_mismatch"
    assert by_id["badunit"]["reason"] == "unknown_unit"
    assert by_id["reversed"]["reason"] == "start_after_end"
    assert by_id["oor"]["reason"] == "offset_out_of_range"

    # --- same text, tokens leave a non-whitespace gap ("a" is untokenized)
    tokens2 = [_tok(0, 1, b"c"), _tok(2, 5, data[2:5])]
    anns2 = [
        _ann("all", "ner2", "codepoint", 0, 4, sha),            # skips "a"
        _ann("c", "ner2", "codepoint", 0, 1, sha, "C"),
        _ann("fe", "ner2", "codepoint", 2, 4, sha, "FE"),
    ]
    res2 = project(text, tokens2, anns2)
    by2 = _by_id(res2)
    assert by2["all"]["status"] == "unrepresentable"
    assert by2["all"]["reason"] == "non_whitespace_gap"
    # the unrepresentable annotation does not displace the valid ones
    assert res2["layers"]["ner2"]["labels"] == ["U-C", "U-FE"]

    # --- decomposed cafe + ́: different bytes, different sha, no normalization
    text3 = "café"
    data3 = text3.encode("utf-8")         # c a f e CC 81
    sha3 = hashlib.sha256(data3).hexdigest()
    assert sha3 != sha
    tokens3 = [_tok(0, 4, b"cafe"), _tok(4, 6, data3[4:6])]
    anns3 = [
        _ann("accent", "d", "codepoint", 4, 5, sha3, "ACC"),    # combining mark alone
        _ann("e_whole", "d", "codepoint", 3, 5, sha3),          # cuts token "cafe"
        _ann("pre_sha", "d", "codepoint", 3, 4, sha),           # bound to the other text
    ]
    res3 = project(text3, tokens3, anns3)
    by3 = _by_id(res3)
    assert by3["accent"]["status"] == "accepted"
    assert res3["layers"]["d"]["labels"] == ["O", "U-ACC"]
    assert by3["e_whole"]["reason"] == "start_inside_token"
    assert by3["pre_sha"]["reason"] == "sha256_mismatch"
    print("ok  test_accents_no_normalization_and_gaps")


def test_positions_special_tokens_and_order_invariance():
    text = "ha ha"
    sha = hashlib.sha256(text.encode("utf-8")).hexdigest()
    tokens = [_special(b"[CLS]"), _tok(0, 2, b"ha"), _special(b"|"),
              _tok(3, 5, b"ha"), _special(b"[SEP]")]
    anns = [
        _ann("second", "ha", "codepoint", 3, 5, sha, "HA"),     # same surface, 2nd pos
        _ann("first", "ha", "codepoint", 0, 2, sha, "HA"),
        _ann("whole", "ha", "codepoint", 0, 5, sha),            # special token inside
        _ann("x", "cf", "codepoint", 0, 2, sha, "A"),
        _ann("y", "cf", "codepoint", 0, 3, sha, "B"),           # overlaps x on token 1
        _ann("u16eq", "ha16", "utf16", 3, 5, sha, "HA"),        # BMP: utf16 == codepoint
    ]
    res = project(text, tokens, anns)
    by_id = _by_id(res)
    # coordinates, not string find: the second "ha" lands on token 3
    assert by_id["second"]["token_indices"] == [3]
    assert by_id["first"]["token_indices"] == [1]
    assert res["layers"]["ha"]["labels"] == ["O", "U-HA", "O", "U-HA", "O"]
    assert by_id["whole"]["status"] == "unrepresentable"
    assert by_id["whole"]["reason"] == "special_token_inside_span"
    assert by_id["x"]["status"] == "conflict" and by_id["y"]["status"] == "conflict"
    assert by_id["x"]["conflicts_with"] == ["y"]
    assert res["layers"]["cf"]["labels"] == ["O"] * 5
    assert by_id["u16eq"]["byte_start"] == by_id["second"]["byte_start"] == 3
    assert by_id["u16eq"]["status"] == "accepted"

    # annotation order must not change anything
    base = json.dumps(res, sort_keys=True)
    for seed in range(20):
        shuffled = anns[:]
        random.Random(seed).shuffle(shuffled)
        assert json.dumps(project(text, tokens, shuffled), sort_keys=True) == base
    print("ok  test_positions_special_tokens_and_order_invariance")


def test_token_contract_and_bilou_decode():
    text = "abc"
    good = _tok(0, 1, b"a")
    bad_piece = {"offset": [0, 1], "piece_base64": base64.b64encode(b"X").decode()}
    try:
        project(text, [bad_piece], [])
        raise AssertionError("piece mismatch not caught")
    except ValueError:
        pass
    overlap = [good, _tok(0, 2, b"ab")]
    try:
        project(text, overlap, [])
        raise AssertionError("overlap not caught")
    except ValueError:
        pass
    for bad in (["I-X"], ["B-X", "O"], ["B-X", "I-Y", "L-X"], ["L-X"]):
        try:
            bilou_decode(bad)
            raise AssertionError(f"malformed BILOU accepted: {bad}")
        except ValueError:
            pass
    assert bilou_decode(["U-A", "O", "B-B", "I-B", "L-B"]) == [(0, 1, "A"), (2, 5, "B")]
    print("ok  test_token_contract_and_bilou_decode")


def main():
    test_unit_conversion_independent()
    test_emoji_units_and_conflicts()
    test_accents_no_normalization_and_gaps()
    test_positions_special_tokens_and_order_invariance()
    test_token_contract_and_bilou_decode()
    print("ALL TESTS PASSED\n")

    # demo: full projection output for the emoji document
    text = "ok 😀 yes"
    data = text.encode("utf-8")
    sha = hashlib.sha256(data).hexdigest()
    tokens = [_special(b"[CLS]"), _tok(0, 2, b"ok"),
              _tok(3, 5, data[3:5]), _tok(5, 7, data[5:7]),
              _tok(8, 11, b"yes"), _special(b"[SEP]")]
    anns = [
        _ann("u16", "units", "utf16", 3, 5, sha, "EMOJI"),
        _ann("cp", "units", "codepoint", 3, 4, sha, "EMOJI"),
        _ann("u8", "units", "utf8", 3, 7, sha, "EMOJI"),
        _ann("emo", "emo", "utf16", 3, 5, sha, "EMOJI"),
        _ann("yes", "word", "codepoint", 5, 8, sha, "WORD"),
        _ann("full", "full", "codepoint", 0, 8, sha, "SENT"),
    ]
    print(json.dumps(project(text, tokens, anns), indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
