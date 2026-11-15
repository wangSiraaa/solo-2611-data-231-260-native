#!/usr/bin/env python3
"""ldifdir.py — offline LDIF directory export.

Materializes base.ldif + changes.ldif into queryable snapshots:
three JSONL tables (entries, attributes, member_edges), per-record
results, and intermediate snapshots selectable by record number.

Subcommands: load, apply, verify.  Python 3.11, stdlib only.

Design notes:
  * entryUUID is the immutable identity of an entry; the DN is a mutable
    position.  DNs are parsed into structured form (tuple of RDNs, leaf
    first; each RDN a sorted tuple of (type, value) AVAs) and all
    hierarchy questions (equality, parent, ancestors, subtree) are
    answered on that structure, never by string endswith.
  * Every change record is applied to a private deep copy of the
    directory and committed only when the whole record succeeds, which
    makes each record atomic.
  * member attributes are never rewritten; every snapshot re-resolves
    the stored DN strings against the current directory.
"""
from __future__ import annotations

import argparse
import base64
import copy
import hashlib
import json
import sys
from dataclasses import dataclass
from pathlib import Path

# ======================================================================
# Structured DNs (RFC 4514 subset)
# ======================================================================
# A DN is a tuple of RDNs, leaf first.  An RDN is a tuple of
# (attribute_type_lower, value) AVAs sorted into canonical order, so
# multi-AVA RDNs compare order-insensitively.  Attribute types are
# case-insensitive (stored lowercased); values are exact Unicode
# strings — no casefold, no normalization.

_HEXDIGITS = frozenset("0123456789abcdefABCDEF")


class DNError(ValueError):
    """A DN string could not be parsed."""


def _parse_ava_value(s: str, i: int, stops: str) -> tuple[str, int]:
    """Parse one AVA value starting at s[i] up to an unescaped stop char."""
    chars: list[tuple[str, bool]] = []  # (char, was_escaped)
    hexbuf = bytearray()

    def flush_hex() -> None:
        nonlocal hexbuf
        if hexbuf:
            try:
                decoded = bytes(hexbuf).decode("utf-8")
            except UnicodeDecodeError as exc:
                raise DNError(f"invalid UTF-8 in hex escape: {exc}") from exc
            chars.extend((c, True) for c in decoded)
            hexbuf = bytearray()

    n = len(s)
    while i < n:
        c = s[i]
        if c == "\\":
            if i + 1 >= n:
                raise DNError("trailing backslash in value")
            nxt = s[i + 1]
            if nxt in _HEXDIGITS and i + 2 < n and s[i + 2] in _HEXDIGITS:
                hexbuf.append(int(s[i + 1 : i + 3], 16))  # \XX -> UTF-8 byte
                i += 3
                continue
            flush_hex()
            chars.append((nxt, True))  # \<char> -> literal char
            i += 2
            continue
        if c in stops:
            break
        flush_hex()
        chars.append((c, False))
        i += 1
    flush_hex()
    # RFC 4514: unescaped leading/trailing spaces are insignificant.
    while chars and chars[0] == (" ", False):
        chars.pop(0)
    while chars and chars[-1] == (" ", False):
        chars.pop()
    return "".join(c for c, _ in chars), i


def _parse_rdn(s: str, i: int) -> tuple[tuple, int]:
    """Parse one RDN (one or more AVAs joined by unescaped '+')."""
    avas: list[tuple[str, str]] = []
    n = len(s)
    while True:
        while i < n and s[i] == " ":
            i += 1
        eq = s.find("=", i)
        if eq == -1:
            raise DNError(f"missing '=' in RDN near {s[i:]!r}")
        atype = s[i:eq].strip()
        if not atype or any(c in atype for c in "\\,+"):
            raise DNError(f"invalid attribute type {atype!r}")
        value, i = _parse_ava_value(s, eq + 1, stops="+,")
        avas.append((atype.lower(), value))
        if i < n and s[i] == "+":
            i += 1
            continue
        return tuple(sorted(avas)), i


def parse_dn(s: str) -> tuple:
    """Parse a DN string into structured form (tuple of RDNs, leaf first).

    Hierarchy splitting happens here on unescaped commas only, so escaped
    '\\,' and '\\+' never leak into the structure as separators.
    """
    s = s.strip()
    if not s:
        raise DNError("empty DN")
    rdns: list[tuple] = []
    i, n = 0, len(s)
    while True:
        rdn, i = _parse_rdn(s, i)
        rdns.append(rdn)
        if i < n and s[i] == ",":
            i += 1
            continue
        if i != n:
            raise DNError(f"unexpected character at position {i} in {s!r}")
        return tuple(rdns)


_VALUE_ESCAPES = frozenset('",+;<>\\=')


def escape_ava_value(value: str) -> str:
    out = []
    last = len(value) - 1
    for idx, ch in enumerate(value):
        if ch in _VALUE_ESCAPES or (idx == 0 and ch in (" ", "#")) or (idx == last and ch == " "):
            out.append("\\" + ch)
        else:
            out.append(ch)
    return "".join(out)


def rdn_to_str(rdn: tuple) -> str:
    return "+".join(f"{t}={escape_ava_value(v)}" for t, v in rdn)


def dn_to_str(rdns: tuple) -> str:
    """Canonical string form of a structured DN (deterministic)."""
    return ",".join(rdn_to_str(r) for r in rdns)


# ======================================================================
# Profile
# ======================================================================

class ProfileError(ValueError):
    pass


@dataclass(frozen=True)
class Profile:
    root_rdns: tuple
    dn_attribute_types: frozenset  # attribute types allowed inside DNs
    attribute_types: frozenset     # declared text attributes (lower case)
    member_attributes: frozenset   # text attributes holding DN references

    @classmethod
    def load(cls, path: Path) -> "Profile":
        data = json.loads(path.read_text(encoding="utf-8"))
        try:
            root_rdns = parse_dn(data["root_dn"])
        except (KeyError, DNError) as exc:
            raise ProfileError(f"profile: bad root_dn: {exc}") from exc
        try:
            dn_types = frozenset(t.lower() for t in data["dn_attribute_types"])
            attr_types = frozenset(t.lower() for t in data["attribute_types"])
        except KeyError as exc:
            raise ProfileError(f"profile: missing key {exc}") from exc
        member_attrs = frozenset(t.lower() for t in data.get("member_attributes", ["member"]))
        if "entryuuid" not in attr_types:
            raise ProfileError("profile attribute_types must include entryUUID")
        if not member_attrs <= attr_types:
            raise ProfileError("member attributes must be declared text attributes")
        for rdn in root_rdns:
            for atype, _ in rdn:
                if atype not in dn_types:
                    raise ProfileError(f"root_dn uses undeclared DN attribute type {atype!r}")
        return cls(root_rdns, dn_types, attr_types, member_attrs)

    def checked_dn(self, text: str, what: str = "DN") -> tuple:
        """Parse a DN and validate its AVA types against the profile."""
        rdns = parse_dn(text)
        for rdn in rdns:
            for atype, _ in rdn:
                if atype not in self.dn_attribute_types:
                    raise DNError(f"{what} uses attribute type {atype!r} "
                                  "not declared in dn_attribute_types")
        return rdns


# ======================================================================
# LDIF parsing: folding, '#' comments, '::' base64 UTF-8, '-' separators
# ======================================================================

class LdifError(ValueError):
    pass


@dataclass
class LdifLine:
    attr: str
    value: str | None  # None only for the '-' modify-block separator
    lineno: int


def _parse_ldif_line(line: str, path: Path, lineno: int) -> LdifLine:
    if line == "-":
        return LdifLine("-", None, lineno)
    colon = line.find(":")
    if colon <= 0:
        raise LdifError(f"{path}:{lineno}: malformed line {line!r}")
    attr = line[:colon]
    rest = line[colon + 1 :]
    if rest.startswith(":"):
        payload = rest[1:].strip()
        try:
            value = base64.b64decode(payload, validate=True).decode("utf-8")
        except Exception as exc:
            raise LdifError(f"{path}:{lineno}: invalid base64 UTF-8 for {attr!r}: {exc}") from exc
    elif rest.startswith("<"):
        raise LdifError(f"{path}:{lineno}: URL values ('<') are not supported")
    elif rest.startswith(" "):
        value = rest[1:]
    else:
        value = rest
    return LdifLine(attr, value, lineno)


def read_ldif(path: Path) -> list[list[LdifLine]]:
    """Split an LDIF file into records of logical (unfolded) lines."""
    records: list[list[LdifLine]] = []
    current: list[LdifLine] = []
    pending: LdifLine | None = None

    def flush() -> None:
        nonlocal current
        if current:
            records.append(current)
            current = []

    with open(path, encoding="utf-8") as fh:
        for lineno, raw in enumerate(fh, 1):
            line = raw.rstrip("\r\n")
            if not line:
                if pending is not None:
                    current.append(pending)
                    pending = None
                flush()
                continue
            if line.startswith("#"):
                continue
            if line.startswith(" "):  # LDIF folding: continuation of previous line
                if pending is None or pending.value is None:
                    raise LdifError(f"{path}:{lineno}: continuation without a previous line")
                pending.value += line[1:]
                continue
            if pending is not None:
                current.append(pending)
            pending = _parse_ldif_line(line, path, lineno)
    if pending is not None:
        current.append(pending)
    flush()
    return records


# ======================================================================
# Directory state
# ======================================================================

class ChangeError(Exception):
    """A change record failed; its private working copy is discarded."""


@dataclass
class Entry:
    uuid: str
    rdns: tuple
    attrs: dict  # attribute type (lower) -> set of exact Unicode values

    @property
    def dn(self) -> str:
        return dn_to_str(self.rdns)


class Directory:
    def __init__(self, profile: Profile):
        self.profile = profile
        self.entries: dict[str, Entry] = {}
        self.dn_index: dict[tuple, str] = {}  # structured DN -> entryUUID
        self.root_uuid: str | None = None

    # ---- structured hierarchy queries (never string matching) ----
    def lookup(self, rdns: tuple) -> Entry | None:
        uuid = self.dn_index.get(rdns)
        return self.entries.get(uuid) if uuid is not None else None

    def children_of(self, rdns: tuple) -> list[Entry]:
        return [e for e in self.entries.values()
                if len(e.rdns) == len(rdns) + 1 and e.rdns[1:] == rdns]

    def subtree_of(self, rdns: tuple) -> list[Entry]:
        """The entry itself plus all descendants (tuple suffix match)."""
        n = len(rdns)
        return [e for e in self.entries.values()
                if len(e.rdns) >= n and e.rdns[len(e.rdns) - n :] == rdns]

    def insert(self, entry: Entry) -> None:
        self.entries[entry.uuid] = entry
        self.dn_index[entry.rdns] = entry.uuid

    def remove(self, entry: Entry) -> None:
        del self.entries[entry.uuid]
        del self.dn_index[entry.rdns]

    # ---- invariants ----
    @staticmethod
    def check_rdn_attributes(entry: Entry) -> None:
        """The entry must carry every AVA of its own (leaf) RDN."""
        for atype, value in entry.rdns[0]:
            if value not in entry.attrs.get(atype, set()):
                raise ChangeError(
                    f"entry would lack RDN attribute {atype}={value!r} "
                    f"required by its name {entry.dn!r}"
                )

    def check_text_attribute(self, atype: str) -> None:
        if atype not in self.profile.attribute_types:
            raise ChangeError(f"attribute {atype!r} is not a declared text attribute")


# ======================================================================
# load: base.ldif -> Directory
# ======================================================================

def _entry_from_pairs(directory: Directory, pairs: list[LdifLine], rdns: tuple,
                      path: Path) -> Entry:
    attrs: dict[str, set[str]] = {}
    for line in pairs:
        atype = line.attr.lower()
        if atype not in directory.profile.attribute_types:
            raise LdifError(f"{path}:{line.lineno}: attribute {line.attr!r} "
                            "is not a declared text attribute")
        values = attrs.setdefault(atype, set())
        if line.value in values:
            raise LdifError(f"{path}:{line.lineno}: duplicate value for {line.attr!r}")
        values.add(line.value)
    uuids = attrs.get("entryuuid", set())
    if len(uuids) != 1:
        raise LdifError(f"{path}:{pairs[0].lineno if pairs else '?'}: "
                        "entry must carry exactly one entryUUID value")
    entry = Entry(uuid=next(iter(uuids)), rdns=rdns, attrs=attrs)
    try:
        Directory.check_rdn_attributes(entry)
    except ChangeError as exc:
        raise LdifError(f"{path}: {exc}") from exc
    return entry


def load_base(profile: Profile, path: Path) -> Directory:
    directory = Directory(profile)
    for record in read_ldif(path):
        if record[0].attr.lower() == "version":
            continue
        if record[0].attr.lower() != "dn":
            raise LdifError(f"{path}:{record[0].lineno}: record must start with 'dn'")
        try:
            rdns = profile.checked_dn(record[0].value)
        except DNError as exc:
            raise LdifError(f"{path}:{record[0].lineno}: bad DN: {exc}") from exc
        body = record[1:]
        if any(line.attr.lower() == "changetype" for line in body):
            raise LdifError(f"{path}:{record[0].lineno}: base records must not carry changetype")
        entry = _entry_from_pairs(directory, body, rdns, path)
        if entry.uuid in directory.entries:
            raise LdifError(f"{path}:{record[0].lineno}: duplicate entryUUID {entry.uuid!r}")
        if entry.rdns in directory.dn_index:
            raise LdifError(f"{path}:{record[0].lineno}: duplicate DN {entry.dn!r}")
        directory.insert(entry)
    # The root DN from the profile is the only entry allowed to lack a
    # local parent; every other entry must have its parent present.
    root = directory.lookup(profile.root_rdns)
    if root is None:
        raise LdifError(f"{path}: root DN {dn_to_str(profile.root_rdns)!r} not found")
    orphans = [e for e in directory.entries.values()
               if e.rdns[1:] not in directory.dn_index]
    if orphans != [root]:
        names = [e.dn for e in orphans]
        raise LdifError(f"{path}: entries without a local parent other than the root: {names}")
    directory.root_uuid = root.uuid
    return directory


# ======================================================================
# Change records
# ======================================================================

@dataclass
class Change:
    seq: int
    lineno: int
    dn: str
    changetype: str
    pairs: list  # add: attribute lines
    blocks: list  # modify: [(op, attr_lower, [values])]
    newrdn: str | None = None
    deleteoldrdn: str | None = None
    newsuperior: str | None = None


def _parse_modify_blocks(lines: list[LdifLine], path: Path) -> list[tuple]:
    blocks = []
    i, n = 0, len(lines)
    while i < n:
        head = lines[i]
        op = head.attr.lower()
        if op not in ("add", "delete", "replace"):
            raise LdifError(f"{path}:{head.lineno}: expected add/delete/replace, "
                            f"got {head.attr!r}")
        attr = (head.value or "").strip().lower()
        if not attr:
            raise LdifError(f"{path}:{head.lineno}: empty attribute in '{op}:'")
        i += 1
        values = []
        while i < n and lines[i].attr != "-":
            line = lines[i]
            if line.attr.lower() != attr:
                raise LdifError(f"{path}:{line.lineno}: expected values for {attr!r}, "
                                f"got {line.attr!r}")
            values.append(line.value)
            i += 1
        if i >= n:
            raise LdifError(f"{path}: modify block for {attr!r} not terminated by '-'")
        i += 1  # consume '-'
        blocks.append((op, attr, values))
    if not blocks:
        raise LdifError(f"{path}: modify record has no modification blocks")
    return blocks


def parse_change_records(profile: Profile, paths: list[Path]) -> list[Change]:
    changes: list[Change] = []
    for path in paths:
        for record in read_ldif(path):
            if record[0].attr.lower() == "version":
                continue
            seq = len(changes) + 1
            first = record[0]
            if first.attr.lower() != "dn":
                raise LdifError(f"{path}:{first.lineno}: change record must start with 'dn'")
            rest = record[1:]
            ct = [l for l in rest if l.attr.lower() == "changetype"]
            if len(ct) != 1:
                raise LdifError(f"{path}:{first.lineno}: record needs exactly one changetype")
            changetype = ct[0].value.strip().lower()
            body = [l for l in rest if l.attr.lower() != "changetype"]
            change = Change(seq=seq, lineno=first.lineno, dn=first.value,
                            changetype=changetype, pairs=body, blocks=[])
            if changetype == "add":
                pass  # body pairs are the entry's attributes
            elif changetype == "modify":
                change.blocks = _parse_modify_blocks(body, path)
                change.pairs = []
            elif changetype == "delete":
                if body:
                    raise LdifError(f"{path}:{first.lineno}: delete record carries attributes")
            elif changetype == "modrdn":
                fields: dict[str, str] = {}
                for line in body:
                    key = line.attr.lower()
                    if key not in ("newrdn", "deleteoldrdn", "newsuperior"):
                        raise LdifError(f"{path}:{line.lineno}: unexpected modrdn field {key!r}")
                    if key in fields:
                        raise LdifError(f"{path}:{line.lineno}: duplicate modrdn field {key!r}")
                    fields[key] = line.value
                if "newrdn" not in fields:
                    raise LdifError(f"{path}:{first.lineno}: modrdn requires newrdn")
                change.newrdn = fields["newrdn"]
                change.deleteoldrdn = fields.get("deleteoldrdn", "0")
                change.newsuperior = fields.get("newsuperior")
                change.pairs = []
            else:
                raise LdifError(f"{path}:{ct[0].lineno}: unsupported changetype {changetype!r}")
            changes.append(change)
    return changes


# ======================================================================
# apply: one atomic record at a time
# ======================================================================

def _do_add(directory: Directory, change: Change, rdns: tuple) -> list[str]:
    if rdns in directory.dn_index:
        raise ChangeError(f"DN {dn_to_str(rdns)!r} already exists")
    parent = rdns[1:]
    if parent not in directory.dn_index:
        raise ChangeError(f"parent {dn_to_str(parent)!r} does not exist "
                          "(only the root entry may lack a local parent)")
    attrs: dict[str, set[str]] = {}
    for line in change.pairs:
        atype = line.attr.lower()
        directory.check_text_attribute(atype)
        values = attrs.setdefault(atype, set())
        if line.value in values:
            raise ChangeError(f"duplicate value for attribute {atype!r} in add")
        values.add(line.value)
    uuids = attrs.get("entryuuid", set())
    if len(uuids) != 1:
        raise ChangeError("added entry must carry exactly one entryUUID value")
    uuid = next(iter(uuids))
    if uuid in directory.entries:
        raise ChangeError(f"entryUUID {uuid!r} is already in use")
    entry = Entry(uuid=uuid, rdns=rdns, attrs=attrs)
    directory.check_rdn_attributes(entry)
    directory.insert(entry)
    return [uuid]


def _do_delete(directory: Directory, change: Change, rdns: tuple) -> list[str]:
    entry = directory.lookup(rdns)
    if entry is None:
        raise ChangeError("target entry does not exist")
    if entry.uuid == directory.root_uuid:
        raise ChangeError("the root entry cannot be deleted")
    if directory.children_of(rdns):
        raise ChangeError("cannot delete a non-leaf entry")
    directory.remove(entry)
    return [entry.uuid]


def _do_modify(directory: Directory, change: Change, rdns: tuple) -> list[str]:
    entry = directory.lookup(rdns)
    if entry is None:
        raise ChangeError("target entry does not exist")
    # Blocks execute in order; values are treated as sets.  Any failure
    # aborts the record and the private working copy is thrown away.
    for op, attr, values in change.blocks:
        if attr == "entryuuid":
            raise ChangeError("entryUUID is immutable")
        directory.check_text_attribute(attr)
        current = entry.attrs.get(attr, set())
        if op == "add":
            for value in values:
                if value in current:
                    raise ChangeError(f"add of duplicate value {value!r} to {attr!r}")
                current.add(value)
            if current:
                entry.attrs[attr] = current
        elif op == "delete":
            if values:
                for value in values:
                    if value not in current:
                        raise ChangeError(f"value {value!r} of {attr!r} does not exist")
                    current.discard(value)
                if not current:
                    entry.attrs.pop(attr, None)
            else:
                if attr not in entry.attrs:
                    raise ChangeError(f"attribute {attr!r} does not exist")
                del entry.attrs[attr]
        elif op == "replace":
            if values:
                if len(set(values)) != len(values):
                    raise ChangeError(f"replace set for {attr!r} contains duplicates")
                entry.attrs[attr] = set(values)
            else:
                entry.attrs.pop(attr, None)  # replace without values deletes the attribute
    directory.check_rdn_attributes(entry)
    if len(entry.attrs.get("entryuuid", set())) != 1:
        raise ChangeError("entry must keep exactly one entryUUID value")
    return [entry.uuid]


def _do_modrdn(directory: Directory, change: Change, rdns: tuple) -> list[str]:
    entry = directory.lookup(rdns)
    if entry is None:
        raise ChangeError("target entry does not exist")
    if entry.uuid == directory.root_uuid:
        raise ChangeError("the root entry cannot be renamed or moved")
    if change.deleteoldrdn not in ("0", "1"):
        raise ChangeError(f"deleteoldrdn must be 0 or 1, got {change.deleteoldrdn!r}")
    deleteold = change.deleteoldrdn == "1"
    try:
        new_rdn_parts = directory.profile.checked_dn(change.newrdn, "newrdn")
    except DNError as exc:
        raise ChangeError(f"bad newrdn: {exc}") from exc
    if len(new_rdn_parts) != 1:
        raise ChangeError("newrdn must be a single RDN")
    new_rdn = new_rdn_parts[0]
    if change.newsuperior is not None:
        try:
            superior = directory.profile.checked_dn(change.newsuperior, "newsuperior")
        except DNError as exc:
            raise ChangeError(f"bad newsuperior: {exc}") from exc
        if superior not in directory.dn_index:
            raise ChangeError(f"new superior {dn_to_str(superior)!r} does not exist")
        # Structured subtree test: the new superior must not be the entry
        # itself or one of its descendants.
        if len(superior) >= len(rdns) and superior[len(superior) - len(rdns) :] == rdns:
            raise ChangeError("cannot move an entry into its own subtree")
        parent = superior
    else:
        parent = rdns[1:]
    new_rdns = (new_rdn,) + parent
    if new_rdns == rdns:
        raise ChangeError("new DN is identical to the old DN")
    # Relocate the whole subtree; fail if any produced DN collides with
    # an entry that is not itself moving.
    subtree = directory.subtree_of(rdns)
    moving = {e.uuid for e in subtree}
    depth = len(rdns)
    relocations = []
    for e in subtree:
        shifted = e.rdns[: len(e.rdns) - depth] + new_rdns
        holder = directory.dn_index.get(shifted)
        if holder is not None and holder not in moving:
            raise ChangeError(f"naming conflict: {dn_to_str(shifted)!r} already exists")
        relocations.append((e, shifted))
    # deleteoldrdn=1: drop old-RDN AVA values absent from the new RDN;
    # then, in both modes, add the values the new RDN needs.
    if deleteold:
        for atype, value in rdns[0]:
            if (atype, value) not in new_rdn:
                remaining = entry.attrs.get(atype, set())
                remaining.discard(value)
                if not remaining:
                    entry.attrs.pop(atype, None)
    for atype, value in new_rdn:
        entry.attrs.setdefault(atype, set()).add(value)
    for e, _ in relocations:
        del directory.dn_index[e.rdns]
    for e, shifted in relocations:
        e.rdns = shifted
        directory.dn_index[shifted] = e.uuid
    directory.check_rdn_attributes(entry)
    return sorted(moving)


def apply_change(directory: Directory, change: Change) -> list[str]:
    """Apply one record to a private working copy; return affected UUIDs."""
    try:
        rdns = directory.profile.checked_dn(change.dn, "target DN")
    except DNError as exc:
        raise ChangeError(f"bad target DN: {exc}") from exc
    handler = {
        "add": _do_add,
        "delete": _do_delete,
        "modify": _do_modify,
        "modrdn": _do_modrdn,
    }[change.changetype]
    return handler(directory, change, rdns)


# ======================================================================
# Snapshot tables
# ======================================================================

def member_edges(directory: Directory) -> list[dict]:
    """Resolve every member DN string against the *current* directory.

    Stored strings are never rewritten; a reference whose DN no longer
    exists simply comes out dangling until an explicit member change
    repairs it.
    """
    edges = []
    for entry in sorted(directory.entries.values(), key=lambda e: e.uuid):
        for attr in sorted(entry.attrs):
            if attr not in directory.profile.member_attributes:
                continue
            for value in sorted(entry.attrs[attr]):
                target = None
                try:
                    ref = directory.profile.checked_dn(value, "member DN")
                    target = directory.dn_index.get(ref)
                except DNError:
                    pass
                edges.append({
                    "src_uuid": entry.uuid,
                    "attribute": attr,
                    "member_dn": value,
                    "target_uuid": target,
                    "status": "resolved" if target is not None else "dangling",
                })
    return edges


def build_tables(directory: Directory) -> tuple[list[dict], list[dict], list[dict]]:
    entries = []
    attributes = []
    for e in sorted(directory.entries.values(), key=lambda e: e.uuid):
        entries.append({
            "uuid": e.uuid,
            "dn": e.dn,
            "rdn": rdn_to_str(e.rdns[0]),
            "parent_dn": dn_to_str(e.rdns[1:]) if len(e.rdns) > 1 else None,
        })
        for attr in sorted(e.attrs):
            for value in sorted(e.attrs[attr]):
                attributes.append({"uuid": e.uuid, "attribute": attr, "value": value})
    return entries, attributes, member_edges(directory)


def summarize(directory: Directory) -> dict:
    edges = member_edges(directory)
    return {
        "entries": len(directory.entries),
        "attribute_values": sum(len(v) for e in directory.entries.values() for v in e.attrs.values()),
        "member_edges": len(edges),
        "dangling_edges": sum(1 for edge in edges if edge["status"] != "resolved"),
    }


def state_hash(directory: Directory) -> str:
    canonical = [
        [e.uuid, e.dn, [[a, sorted(v)] for a, v in sorted(e.attrs.items())]]
        for e in sorted(directory.entries.values(), key=lambda e: e.uuid)
    ]
    blob = json.dumps(canonical, ensure_ascii=False, sort_keys=True).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()[:16]


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")


def write_tables(directory: Directory, out_dir: Path) -> None:
    entries, attributes, edges = build_tables(directory)
    _write_jsonl(out_dir / "entries.jsonl", entries)
    _write_jsonl(out_dir / "attributes.jsonl", attributes)
    _write_jsonl(out_dir / "member_edges.jsonl", edges)


def write_snapshot(directory: Directory, out_dir: Path) -> None:
    write_tables(directory, out_dir)
    _write_jsonl(out_dir / "summary.jsonl", [summarize(directory)])


# ======================================================================
# verify
# ======================================================================

def _read_jsonl(path: Path) -> list[dict]:
    with open(path, encoding="utf-8") as fh:
        return [json.loads(line) for line in fh if line.strip()]


def verify_out(out_dir: Path) -> list[str]:
    """Re-check an output directory; return a list of problems (empty = OK)."""
    problems: list[str] = []

    def check(cond: bool, msg: str) -> None:
        if not cond:
            problems.append(msg)

    entries = _read_jsonl(out_dir / "entries.jsonl")
    attributes = _read_jsonl(out_dir / "attributes.jsonl")
    edges = _read_jsonl(out_dir / "member_edges.jsonl")
    results_path = out_dir / "results.jsonl"
    results = _read_jsonl(results_path) if results_path.exists() else []

    # --- UUID population ---
    uuids = [e["uuid"] for e in entries]
    check(len(uuids) == len(set(uuids)), "duplicate UUIDs in entries")
    dns = [e["dn"] for e in entries]
    check(len(dns) == len(set(dns)), "duplicate DNs in entries")

    attr_by_uuid: dict[str, dict[str, set[str]]] = {}
    for row in attributes:
        check(row["uuid"] in set(uuids), f"attributes row for unknown UUID {row['uuid']}")
        values = attr_by_uuid.setdefault(row["uuid"], {}).setdefault(row["attribute"], set())
        check(row["value"] not in values,
              f"duplicate attribute value {row['attribute']}={row['value']!r} for {row['uuid']}")
        values.add(row["value"])

    # entryUUID: exactly one value per entry, matching the table key.
    for e in entries:
        vals = attr_by_uuid.get(e["uuid"], {}).get("entryuuid", set())
        check(vals == {e["uuid"]},
              f"entry {e['uuid']}: entryUUID attribute {sorted(vals)} != table key")

    # --- parent/child structure on parsed DNs ---
    dn_to_uuid = {}
    parsed = {}
    for e in entries:
        try:
            rdns = parse_dn(e["dn"])
        except DNError as exc:
            problems.append(f"unparseable DN {e['dn']!r}: {exc}")
            continue
        parsed[e["uuid"]] = rdns
        dn_to_uuid[rdns] = e["uuid"]
    orphans = [u for u, r in parsed.items() if r[1:] not in dn_to_uuid]
    check(len(orphans) == 1, f"expected exactly one root entry, found {len(orphans)}")
    if orphans:
        root = orphans[0]
        for uuid, rdns in parsed.items():
            # walking parents strictly shortens the DN, so this terminates
            cursor = rdns
            while cursor[1:] in dn_to_uuid:
                cursor = cursor[1:]
            check(dn_to_uuid.get(cursor) == root,
                  f"entry {uuid} does not descend from the root")
    # entries.jsonl parent_dn column agrees with the parsed structure
    for e in entries:
        rdns = parsed.get(e["uuid"])
        if rdns is None:
            continue
        expect_parent = dn_to_str(rdns[1:]) if len(rdns) > 1 else None
        check(e["parent_dn"] == expect_parent,
              f"entry {e['uuid']}: parent_dn column {e['parent_dn']!r} != {expect_parent!r}")

    # --- RDN attributes ---
    for e in entries:
        rdns = parsed.get(e["uuid"])
        if rdns is None:
            continue
        for atype, value in rdns[0]:
            check(value in attr_by_uuid.get(e["uuid"], {}).get(atype, set()),
                  f"entry {e['uuid']} lacks RDN attribute {atype}={value!r}")

    # --- reference resolution: re-resolve every member edge from scratch ---
    for edge in edges:
        check(edge["src_uuid"] in set(uuids), f"member edge from unknown UUID {edge['src_uuid']}")
        stored = attr_by_uuid.get(edge["src_uuid"], {}).get(edge["attribute"], set())
        check(edge["member_dn"] in stored,
              f"member edge value not present in attributes table: {edge}")
        try:
            ref = parse_dn(edge["member_dn"])
            expect_target = dn_to_uuid.get(ref)
        except DNError:
            expect_target = None
        expect_status = "resolved" if expect_target is not None else "dangling"
        check(edge["target_uuid"] == expect_target and edge["status"] == expect_status,
              f"member edge {edge['member_dn']!r}: expected {expect_status} -> "
              f"{expect_target}, got {edge['status']} -> {edge['target_uuid']}")

    # --- per-record results ---
    if results:
        check([r["seq"] for r in results] == list(range(1, len(results) + 1)),
              "record sequence numbers are not contiguous from 1")
        for r in results:
            if not r["ok"]:
                check(r["hash_before"] == r["hash_after"],
                      f"record {r['seq']}: failed but state hash changed")
                check(r["affected_uuids"] == [],
                      f"record {r['seq']}: failed but reports affected UUIDs")
            for uuid in r["affected_uuids"]:
                check(isinstance(uuid, str) and uuid, f"record {r['seq']}: bad affected UUID")
        # UUID population arithmetic: base + successful adds - successful deletes
        load_snapshot = out_dir / "snapshots" / "000_load" / "entries.jsonl"
        if load_snapshot.exists():
            base_count = len(_read_jsonl(load_snapshot))
            adds = sum(1 for r in results if r["ok"] and r["changetype"] == "add")
            deletes = sum(1 for r in results if r["ok"] and r["changetype"] == "delete")
            check(len(entries) == base_count + adds - deletes,
                  f"UUID total {len(entries)} != base {base_count} + adds {adds} - deletes {deletes}")
        # final summary in the last record matches the final tables
        final_summary = {
            "entries": len(entries),
            "attribute_values": len(attributes),
            "member_edges": len(edges),
            "dangling_edges": sum(1 for edge in edges if edge["status"] != "resolved"),
        }
        check(results[-1]["summary"] == final_summary,
              f"final summary {results[-1]['summary']} != recomputed {final_summary}")

    # --- exported intermediate snapshots are consistent with the results ---
    snapshots_dir = out_dir / "snapshots"
    if snapshots_dir.exists():
        for snap in sorted(snapshots_dir.iterdir()):
            if not snap.is_dir():
                continue
            snap_entries = _read_jsonl(snap / "entries.jsonl")
            snap_attributes = _read_jsonl(snap / "attributes.jsonl")
            snap_edges = _read_jsonl(snap / "member_edges.jsonl")
            recomputed = {
                "entries": len(snap_entries),
                "attribute_values": len(snap_attributes),
                "member_edges": len(snap_edges),
                "dangling_edges": sum(1 for edge in snap_edges if edge["status"] != "resolved"),
            }
            recorded = _read_jsonl(snap / "summary.jsonl")[0]
            check(recomputed == recorded,
                  f"snapshot {snap.name}: tables {recomputed} != summary {recorded}")
            if snap.name != "000_load" and results:
                seq = int(snap.name.split("_", 1)[0])
                check(results[seq - 1]["summary"] == recorded,
                      f"snapshot {snap.name}: summary does not match record {seq}")
    return problems


# ======================================================================
# CLI
# ======================================================================

def cmd_load(args: argparse.Namespace) -> int:
    profile = Profile.load(args.profile)
    directory = load_base(profile, args.base)
    write_tables(directory, args.out)
    summary = summarize(directory)
    print(f"loaded {summary['entries']} entries, "
          f"{summary['attribute_values']} attribute values, "
          f"{summary['member_edges']} member edges "
          f"({summary['dangling_edges']} dangling) -> {args.out}")
    return 0


def cmd_apply(args: argparse.Namespace) -> int:
    profile = Profile.load(args.profile)
    directory = load_base(profile, args.base)
    changes = parse_change_records(profile, args.changes)
    snapshot_after = {int(x) for x in args.snapshot_after.split(",") if x.strip()}

    args.out.mkdir(parents=True, exist_ok=True)
    write_snapshot(directory, args.out / "snapshots" / "000_load")

    results = []
    for change in changes:
        hash_before = state_hash(directory)
        work = copy.deepcopy(directory)  # private copy: the record is atomic
        try:
            affected = apply_change(work, change)
            ok, reason = True, None
            directory = work
        except ChangeError as exc:
            affected, ok, reason = [], False, str(exc)
        hash_after = state_hash(directory)
        if not ok:
            assert hash_before == hash_after, "failed record must leave state unchanged"
        summary = summarize(directory)
        results.append({
            "seq": change.seq,
            "changetype": change.changetype,
            "dn": change.dn,
            "ok": ok,
            "reason": reason,
            "affected_uuids": sorted(affected),
            "summary": summary,
            "hash_before": hash_before,
            "hash_after": hash_after,
        })
        status = "OK  " if ok else "FAIL"
        print(f"[{change.seq:03d}] {status} {change.changetype:<7} {change.dn}"
              + ("" if ok else f"  -- {reason}"))
        if change.seq in snapshot_after:
            write_snapshot(directory, args.out / "snapshots"
                           / f"{change.seq:03d}_{change.changetype}")

    write_tables(directory, args.out)
    _write_jsonl(args.out / "results.jsonl", results)
    ok_count = sum(1 for r in results if r["ok"])
    summary = summarize(directory)
    print(f"applied {ok_count}/{len(results)} records; final: "
          f"{summary['entries']} entries, {summary['attribute_values']} attribute values, "
          f"{summary['member_edges']} member edges ({summary['dangling_edges']} dangling)")
    return 0


def cmd_verify(args: argparse.Namespace) -> int:
    problems = verify_out(args.out)
    if problems:
        print(f"VERIFY FAILED: {len(problems)} problem(s)")
        for problem in problems:
            print(f"  - {problem}")
        return 1
    print("VERIFY OK: UUID totals, parent/child structure, RDN attributes, "
          "member resolution, and failed-record state hashes all consistent")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="ldifdir", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)

    p_load = sub.add_parser("load", help="materialize base.ldif into JSONL tables")
    p_load.add_argument("--base", type=Path, required=True)
    p_load.add_argument("--profile", type=Path, required=True)
    p_load.add_argument("--out", type=Path, required=True)
    p_load.set_defaults(func=cmd_load)

    p_apply = sub.add_parser("apply", help="load base, then apply change records atomically")
    p_apply.add_argument("--base", type=Path, required=True)
    p_apply.add_argument("--changes", type=Path, required=True, action="append",
                         help="changes LDIF file; may be given several times, "
                              "records are numbered continuously in order")
    p_apply.add_argument("--profile", type=Path, required=True)
    p_apply.add_argument("--out", type=Path, required=True)
    p_apply.add_argument("--snapshot-after", default="", metavar="N[,M...]",
                         help="record numbers after which to export an intermediate "
                              "snapshot (0-load snapshot is always exported)")
    p_apply.set_defaults(func=cmd_apply)

    p_verify = sub.add_parser("verify", help="re-check an output directory for consistency")
    p_verify.add_argument("--out", type=Path, required=True)
    p_verify.set_defaults(func=cmd_verify)

    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except (LdifError, ProfileError, ChangeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
