#!/usr/bin/env python3
"""Read frozen HTML pages for Microdata entities and emit catalog records.

The recognised semantics are the narrow ones given in ``fixtures/scope.json``:

* only ``itemscope`` nodes are entities (a stray ``itemref`` on a non-scope
  node is ignored);
* an item's properties are collected from its descendants *and* from the
  same-document elements named by its ``itemref``; a DOM node is visited at
  most once per item, which also stops shared/repeated refs and ancestor
  cycles;
* a nested ``itemscope`` is an entity boundary.  It is handed to the outer
  item as a property value only when it carries ``itemprop``; its internals
  never leak outward, and a scope without ``itemprop`` is still registered;
* ``itemid`` is resolved against the first usable ``base href`` (falling back
  to the page URL); entities without ``itemid`` get a local identity from the
  document id plus a stable DOM path and are never merged by equal text;
* values follow the per-element rules in ``scope.json`` and keep a
  ``value_kind`` so URL values are never conflated with plain text;
* bad ``itemref`` targets, cross-document fragments and unsupported elements
  produce diagnostics instead of guessed values.

No schema.org business rules are inferred, no RDF normalisation, graph
isomorphism or entity similarity matching is performed, and no network access
is made.  The HTML tree is built with the standard library, so no third-party
parser is needed.

Usage::

    python3 microdata_reader.py fixtures
    python3 microdata_reader.py fixtures --json --pretty
    python3 microdata_reader.py fixtures --catalog --pretty --out out/catalog.json
    python3 microdata_reader.py fixtures --compare catalog.html variant.html
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter
from dataclasses import dataclass, field
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urljoin, urlsplit

# A tiny element tree on top of the standard library parser.  It keeps the
# small surface this reader needs (tag/attrs/children/text, parent links) so
# no third-party parser is required to run the tool offline.
_HEAD_TAGS = {
    "base", "basefont", "bgsound", "link", "meta", "title", "style", "script",
    "noscript", "template",
}
_VOID_TAGS = {
    "area", "base", "br", "col", "embed", "hr", "img", "input",
    "link", "meta", "param", "source", "track", "wbr",
}


class Element:
    __slots__ = ("tag", "attrib", "text", "tail", "parent", "children")

    def __init__(self, tag: str, attrib: dict | None = None):
        self.tag = tag
        self.attrib = dict(attrib or {})
        self.text: str | None = None
        self.tail: str | None = None
        self.parent: Element | None = None
        self.children: list[Element] = []

    def get(self, name: str, default=None):
        return self.attrib.get(name, default)

    def append(self, child: "Element") -> None:
        child.parent = self
        self.children.append(child)

    def __iter__(self):
        return iter(self.children)

    def getparent(self):
        return self.parent

    def iter(self):
        yield self
        for child in self.children:
            yield from child.iter()

    def iterdescendants(self):
        for child in self.children:
            yield from child.iter()


class _TreeBuilder(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.root = Element("html")
        self.head = Element("head")
        self.body = Element("body")
        self.root.append(self.head)
        self.root.append(self.body)
        self.stack: list[Element] = [self.root]
        self.phase = "head"

    # -- helpers -----------------------------------------------------

    def _current(self) -> Element:
        if len(self.stack) == 1:
            return self.head if self.phase == "head" else self.body
        return self.stack[-1]

    def _append_text(self, data: str) -> None:
        if not data:
            return
        parent = self._current()
        if parent.children:
            last = parent.children[-1]
            last.tail = (last.tail or "") + data
        else:
            parent.text = (parent.text or "") + data

    # -- parser callbacks --------------------------------------------

    def handle_starttag(self, tag: str, attrs) -> None:
        tag = tag.lower()
        attributes: dict[str, str] = {}
        for name, value in attrs:
            # The first occurrence of a duplicate attribute wins.
            if name not in attributes:
                attributes[name] = value if value is not None else ""

        if tag == "html":
            return
        if tag == "head":
            self.phase = "head"
            self.stack = [self.root, self.head]
            return
        if tag == "body":
            self.phase = "body"
            self.stack = [self.root, self.body]
            return

        if self.phase == "head" and tag in _HEAD_TAGS:
            parent = self.head
        else:
            if self.phase == "head":
                self.phase = "body"
                self.stack = [self.root, self.body]
            parent = self.stack[-1]

        element = Element(tag, attributes)
        parent.append(element)
        if tag not in _VOID_TAGS:
            self.stack.append(element)

    def handle_startendtag(self, tag: str, attrs) -> None:
        # Self-closing syntax: create the element but never descend into it.
        self.handle_starttag(tag, attrs)
        tag = tag.lower()
        if tag not in _VOID_TAGS and self.stack and self.stack[-1].tag == tag:
            self.stack.pop()

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag in _VOID_TAGS:
            return
        if tag == "html":
            self.stack = [self.root]
            return
        if tag == "head":
            self.phase = "body"
            self.stack = [self.root]
            return
        if tag == "body":
            self.stack = [self.root]
            return
        for index in range(len(self.stack) - 1, 0, -1):
            if self.stack[index].tag == tag:
                del self.stack[index:]
                return

    def handle_data(self, data: str) -> None:
        self._append_text(data)

    def handle_comment(self, data: str) -> None:
        # Comments are not document text and carry no Microdata.
        return

    def handle_decl(self, decl: str) -> None:
        return

    def unknown_decl(self, data: str) -> None:
        return

    def handle_pi(self, data: str) -> None:
        return


def parse_html(html: bytes | str) -> Element:
    """Parse a whole HTML document into an ``html`` element tree."""
    if isinstance(html, bytes):
        # Frozen pages are UTF-8; strip a byte-order mark if present.
        text = html.decode("utf-8-sig", errors="replace")
    else:
        text = html
    builder = _TreeBuilder()
    builder.feed(text)
    builder.close()
    return builder.root


DEFAULT_VALUE_ELEMENTS = {
    "meta": "content",
    "a": "href URL",
    "link": "href URL",
    "img": "src URL",
    "time": "datetime else text",
    "data": "value",
    "span": "text",
    "div": "text",
    "p": "text",
}

# HTML whitespace is space, tab, line feed, form feed and carriage return.
# Python's \s also matches non-breaking spaces, which are not HTML whitespace.
HTML_WS = " \t\n\r\f"
WS_RE = re.compile(rf"[{HTML_WS}]+")


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------


def local_name(element) -> str | None:
    """Lowercase tag name of a real element."""
    name = element.tag
    if not isinstance(name, str):  # defensive: non-element nodes
        return None
    return name.lower()


def attr(element, name: str) -> str | None:
    return element.get(name)


def collapse_text(text: str) -> str:
    """Collapse HTML whitespace runs and trim outer HTML whitespace."""
    return WS_RE.sub(" ", text).strip()


def split_tokens(value: str | None) -> list[str]:
    """Split an attribute on HTML whitespace (not all Unicode whitespace)."""
    if value is None:
        return []
    return [token for token in WS_RE.split(value) if token]


def element_text(element) -> str:
    """Join descendant text nodes in tree order (comments excluded)."""
    parts: list[str] = []

    def walk(node) -> None:
        if node.text:
            parts.append(node.text)
        for child in node:
            walk(child)
            if child.tail:
                parts.append(child.tail)

    walk(element)
    return collapse_text("".join(parts))


def resolve_iri(raw: str | None, base: str) -> tuple[str | None, str | None]:
    """Resolve ``raw`` against ``base``; return (iri, error_code)."""
    if raw is None:
        return None, "missing"
    value = raw.strip()
    if not value:
        return None, "empty"
    try:
        resolved = urljoin(base, value)
    except ValueError:
        return None, "invalid"
    if not urlsplit(resolved).scheme:
        return None, "no_scheme"
    return resolved, None


def canonical_value(value) -> str:
    if isinstance(value, str):
        return value
    return json.dumps(value, sort_keys=True, ensure_ascii=False)


# ---------------------------------------------------------------------------
# Scope configuration
# ---------------------------------------------------------------------------


class ScopeRules:
    """The subset of Microdata behaviour declared in ``scope.json``."""

    def __init__(self, raw: dict | None = None):
        raw = raw or {}
        value_elements = raw.get("value_elements") or DEFAULT_VALUE_ELEMENTS
        self.value_elements = {k.lower(): v for k, v in value_elements.items()}
        self.text = raw.get("text", "")
        self.itemref = raw.get("itemref", "")
        self.walk = raw.get("walk", "")
        self.anonymous_identity = raw.get("anonymous_identity", "")
        self.iri = raw.get("IRI", "")
        self.bad_references = raw.get("bad_references", "")

    @classmethod
    def load(cls, path: Path | None) -> "ScopeRules":
        if path is None or not path.exists():
            return cls()
        with path.open("r", encoding="utf-8") as handle:
            return cls(json.load(handle))


# ---------------------------------------------------------------------------
# Document index
# ---------------------------------------------------------------------------


class Document:
    def __init__(self, document_id: str, url: str, html: bytes, source_path: str):
        self.document_id = document_id
        self.url = url
        self.source_path = source_path
        self.doc = parse_html(html)

        self.base, self.base_source, self.base_diagnostics = self._first_base()
        self.id_map: dict[str, object] = {}
        self.duplicate_ids: list[str] = []
        self.nodes: list[tuple[object, str, int]] = []
        self.path_by_node: dict[int, str] = {}
        self.order_by_node: dict[int, int] = {}
        self.index_nodes()

        for element in self.doc.iter():
            element_id = attr(element, "id")
            if element_id is None:
                continue
            if element_id in self.id_map:
                if element_id not in self.duplicate_ids:
                    self.duplicate_ids.append(element_id)
            else:
                self.id_map[element_id] = element

    # -- base ---------------------------------------------------------

    def _first_base(self) -> tuple[str, str, list[dict]]:
        diagnostics: list[dict] = []
        bases = [e for e in self.doc.iter() if local_name(e) == "base"]
        page_url = self.url
        for index, base in enumerate(bases):
            raw = attr(base, "href")
            if raw is None or not raw.strip():
                diagnostics.append(
                    {
                        "code": "base_missing_href",
                        "message": "base element has no usable href",
                        "source": {"element": "base", "index": index},
                    }
                )
                continue
            resolved = urljoin(page_url, raw.strip()) if page_url else raw.strip()
            if not urlsplit(resolved).scheme:
                diagnostics.append(
                    {
                        "code": "base_unusable",
                        "message": "base href does not resolve to an absolute IRI",
                        "href": raw,
                        "resolved": resolved,
                    }
                )
                continue
            return resolved, "base_href", diagnostics
        return page_url, "page_url", diagnostics

    # -- paths --------------------------------------------------------

    def index_nodes(self) -> None:
        # Structural paths are built from tag names and per-tag sibling
        # positions, so reordering HTML attributes or itemref tokens does not
        # change them.
        root = self.doc

        def visit(element, path: str) -> None:
            self.nodes.append((element, path, len(self.nodes)))
            self.path_by_node[id(element)] = path
            self.order_by_node[id(element)] = len(self.nodes) - 1
            child_counters: Counter[str] = Counter()
            for child in element:
                name = local_name(child)
                if name is None:
                    continue
                slot = child_counters[name]
                child_counters[name] += 1
                visit(child, f"{path}/{name}[{slot}]")

        visit(root, "html")

    def path_of(self, element) -> str:
        return self.path_by_node.get(id(element), "?")

    def source_node_id(self, element) -> str:
        element_id = attr(element, "id")
        return element_id if element_id else self.path_of(element)

    def is_ancestor(self, candidate, node) -> bool:
        parent = node.getparent()
        while parent is not None:
            if parent is candidate:
                return True
            parent = parent.getparent()
        return False

    # -- same-document fragment lookup --------------------------------

    def lookup_fragment(self, token: str) -> tuple[object | None, str | None]:
        if token.startswith("#"):
            fragment = token[1:]
        elif "#" in token:
            return None, "cross_document_id"
        else:
            fragment = token
        if not fragment:
            return None, "invalid_itemref"
        element = self.id_map.get(fragment)
        if element is None or id(element) not in self.order_by_node:
            return None, "invalid_itemref"
        return element, None


# ---------------------------------------------------------------------------
# Records
# ---------------------------------------------------------------------------


@dataclass
class Occurrence:
    name: str
    value: object
    value_kind: str
    element: str
    source_document: str
    source_node_id: str
    source_path: str
    path: str  # "direct" or "itemref"
    via: list[str]
    itemprop: str
    attribute: str | None = None
    target_entity: str | None = None

    def to_dict(self) -> dict:
        record = {
            "name": self.name,
            "itemprop": self.itemprop,
            "value": self.value,
            "value_kind": self.value_kind,
            "source_document": self.source_document,
            "source_node_id": self.source_node_id,
            "source_path": self.source_path,
            "path": self.path,
            "via": self.via,
            "element": self.element,
        }
        if self.attribute is not None:
            record["attribute"] = self.attribute
        if self.target_entity is not None:
            record["target_entity"] = self.target_entity
        return record


@dataclass
class Scope:
    """One ``itemscope`` node, before equal-IRI descriptions are merged."""

    identity: str
    identity_kind: str
    identity_source: str
    itemid_raw: str | None
    itemid_resolved: str | None
    itemtypes: list[str]
    outer_names: list[str]
    document_id: str
    node: object
    path: str
    dom_order: int
    properties: list[Occurrence] = field(default_factory=list)
    diagnostics: list[dict] = field(default_factory=list)
    complete: bool = True


# ---------------------------------------------------------------------------
# Reader
# ---------------------------------------------------------------------------


class MicrodataReader:
    def __init__(self, rules: ScopeRules):
        self.rules = rules

    # -- public -------------------------------------------------------

    def read(self, document: Document) -> dict:
        scopes = self._find_scopes(document)
        references: list[dict] = []
        for scope in scopes:
            self._collect_item(document, scope, references)

        entities = self._merge_scopes(scopes)
        diagnostics = list(document.base_diagnostics)
        for duplicate in document.duplicate_ids:
            diagnostics.append(
                {
                    "code": "duplicate_id",
                    "message": "id is used by more than one element; first wins",
                    "id": duplicate,
                }
            )
        # Surface reference problems (bad targets, cross-document fragments,
        # cycles) and local entity problems (unsupported element, missing
        # required attribute, unresolvable itemid) in one document-level list
        # while keeping the same facts on the owning entity.
        for reference in references:
            if reference["status"] in ("ok", "already_visited"):
                continue
            diagnostics.append(
                {
                    "code": reference["status"],
                    "message": reference["message"],
                    "entity": reference["owner_entity"],
                    "token": reference["token"],
                }
            )
        for entity in entities:
            for diagnostic in entity["diagnostics"]:
                entry = {
                    "code": diagnostic["code"],
                    "message": diagnostic["message"],
                    "entity": entity["id"],
                }
                for key in ("element", "attribute", "token"):
                    if key in diagnostic:
                        entry[key] = diagnostic[key]
                if entry not in diagnostics:
                    diagnostics.append(entry)
        return {
            "document_id": document.document_id,
            "source_document": document.source_path,
            "url": document.url,
            "base_iri": document.base,
            "base_source": document.base_source,
            "entities": entities,
            "references": references,
            "diagnostics": diagnostics,
        }

    # -- scope discovery ---------------------------------------------

    def _find_scopes(self, document: Document) -> list[Scope]:
        scopes: list[Scope] = []
        for element, path, order in document.nodes:
            if local_name(element) is None:
                continue
            if attr(element, "itemscope") is None:
                continue
            if local_name(element) == "html":
                # An itemscope on the document element would enclose every
                # other scope and could only create cycles.
                continue
            scopes.append(self._make_scope(document, element, path, order))
        return scopes

    def _identity_for(self, document: Document, node) -> tuple[str, str, str | None, str | None]:
        raw = attr(node, "itemid")
        if raw is not None and raw.strip():
            resolved, _error = resolve_iri(raw, document.base)
            if resolved is not None:
                return resolved, "itemid", "itemid", raw
            return (
                f"{document.document_id}#{document.path_of(node)}",
                "anonymous",
                "document_id+dom_path",
                raw,
            )
        # An absent or whitespace-only itemid gives no global identifier.
        return (
            f"{document.document_id}#{document.path_of(node)}",
            "anonymous",
            "document_id+dom_path",
            raw if raw is not None and raw.strip() else None,
        )

    def _make_scope(self, document, element, path, order) -> Scope:
        identity, kind, source, itemid_raw = self._identity_for(document, element)
        itemtype_raw = attr(element, "itemtype") or ""
        scope = Scope(
            identity=identity,
            identity_kind=kind,
            identity_source=source,
            itemid_raw=itemid_raw,
            itemid_resolved=identity if kind == "itemid" else None,
            itemtypes=split_tokens(itemtype_raw),
            outer_names=split_tokens(attr(element, "itemprop")),
            document_id=document.document_id,
            node=element,
            path=path,
            dom_order=order,
        )
        if itemid_raw is not None and kind != "itemid":
            scope.diagnostics.append(
                {
                    "code": "unresolvable_itemid",
                    "message": "itemid does not resolve to an absolute IRI; "
                    "a local document identity is used instead",
                    "token": itemid_raw,
                }
            )
            scope.complete = False
        return scope

    # -- item traversal ----------------------------------------------

    def _collect_item(
        self, document: Document, scope: Scope, references: list[dict]
    ) -> None:
        visited: set[int] = {id(scope.node)}  # stops self/ancestor cycles
        boundaries: set[int] = set()  # descendants inside nested scopes

        self._walk_children(
            document, scope, scope.node, "direct", [], visited, boundaries
        )
        self._process_itemrefs(document, scope, visited, boundaries, references)

    def _walk_children(
        self,
        document: Document,
        scope: Scope,
        parent,
        path: str,
        via: list[str],
        visited: set[int],
        boundaries: set[int],
    ) -> None:
        for child in parent:
            if local_name(child) is None:
                continue
            if id(child) in visited:
                continue
            visited.add(id(child))
            if attr(child, "itemscope") is not None:
                # Entity boundary: expose it as a value when it is a
                # property, never walk inside it for the outer item.  Its
                # whole subtree is fenced off so an explicit itemref cannot
                # leak the nested entity's internals outward either.
                self._emit(document, scope, child, path, via)
                self._fence_subtree(child, visited, boundaries)
                continue
            self._emit(document, scope, child, path, via)
            self._walk_children(
                document, scope, child, path, via, visited, boundaries
            )

    @staticmethod
    def _fence_subtree(node, visited: set[int], boundaries: set[int]) -> None:
        for descendant in node.iterdescendants():
            if local_name(descendant) is None:
                continue
            visited.add(id(descendant))
            boundaries.add(id(descendant))

    def _process_itemrefs(
        self,
        document: Document,
        scope: Scope,
        visited: set[int],
        boundaries: set[int],
        references: list[dict],
    ) -> None:
        raw = attr(scope.node, "itemref")
        if raw is None:
            return
        for token in split_tokens(raw):
            target, code = document.lookup_fragment(token)
            if code is not None:
                scope.complete = False
                diagnostic = {
                    "code": code,
                    "message": self._ref_message(code),
                    "token": token,
                }
                scope.diagnostics.append(diagnostic)
                references.append(
                    {
                        "owner_entity": scope.identity,
                        "owner_path": scope.path,
                        "token": token,
                        "status": code,
                        "message": diagnostic["message"],
                    }
                )
                continue
            if id(target) in boundaries:
                # Explicitly pointing inside a nested entity must not leak the
                # nested entity's internals to the outer item.
                message = (
                    "itemref target lies inside a nested entity; its "
                    "properties belong to that entity, not to this item"
                )
                scope.diagnostics.append(
                    {"code": "nested_boundary", "message": message, "token": token}
                )
                references.append(
                    {
                        "owner_entity": scope.identity,
                        "owner_path": scope.path,
                        "token": token,
                        "status": "nested_boundary",
                        "target": document.source_node_id(target),
                        "message": message,
                    }
                )
                continue
            if id(target) in visited:
                references.append(
                    {
                        "owner_entity": scope.identity,
                        "owner_path": scope.path,
                        "token": token,
                        "status": "already_visited",
                        "target": document.source_node_id(target),
                        "message": "target was already visited in this item; "
                        "it contributes no second property",
                    }
                )
                continue
            if document.is_ancestor(target, scope.node):
                scope.complete = False
                message = "itemref target is an ancestor of the item; skipped to avoid a cycle"
                scope.diagnostics.append(
                    {"code": "cyclic_itemref", "message": message, "token": token}
                )
                references.append(
                    {
                        "owner_entity": scope.identity,
                        "owner_path": scope.path,
                        "token": token,
                        "status": "cyclic_itemref",
                        "target": document.source_node_id(target),
                        "message": message,
                    }
                )
                continue
            visited.add(id(target))
            references.append(
                {
                    "owner_entity": scope.identity,
                    "owner_path": scope.path,
                    "token": token,
                    "status": "ok",
                    "target": document.source_node_id(target),
                    "message": "resolved",
                }
            )
            self._emit(document, scope, target, "itemref", [token])
            if attr(target, "itemscope") is None:
                # A referenced nested scope is a boundary; anything else is
                # walked so its descendants contribute properties.
                self._walk_children(
                    document, scope, target, "itemref", [token], visited, boundaries
                )
            else:
                self._fence_subtree(target, visited, boundaries)

    @staticmethod
    def _ref_message(code: str) -> str:
        if code == "cross_document_id":
            return "itemref points at a different document; not interpreted"
        if code == "invalid_itemref":
            return "itemref target does not exist in this document"
        return "itemref could not be resolved"

    # -- property extraction -----------------------------------------

    def _emit(
        self,
        document: Document,
        scope: Scope,
        node,
        path: str,
        via: list[str],
    ) -> None:
        names = split_tokens(attr(node, "itemprop"))
        if not names:
            return
        names = sorted(set(names))
        tag = local_name(node) or "?"
        value, value_kind, attribute, target_entity = self._value(document, node, tag)
        if value_kind in ("unsupported", "missing", "unresolvable"):
            scope.complete = False
            if value_kind == "unsupported":
                code = "unsupported_element"
            elif value_kind == "unresolvable":
                code = "unresolvable_url"
            else:
                code = "missing_required_attribute"
            scope.diagnostics.append(
                {
                    "code": code,
                    "message": "no value could be read without guessing",
                    "token": names[0],
                    "element": tag,
                    "attribute": attribute,
                }
            )
        # One attribute occurrence per itemprop name: a property value is
        # emitted once for each name in ``itemprop``.
        for name in names:
            scope.properties.append(
                Occurrence(
                    name=name,
                    value=value,
                    value_kind=value_kind,
                    element=tag,
                    source_document=document.document_id,
                    source_node_id=document.source_node_id(node),
                    source_path=document.path_of(node),
                    path=path,
                    via=list(via),
                    itemprop=" ".join(names),
                    attribute=attribute,
                    target_entity=target_entity,
                )
            )

    def _value(self, document: Document, node, tag: str):
        """Return (value, value_kind, attribute, target_entity)."""
        if attr(node, "itemscope") is not None:
            identity, _, _, _ = self._identity_for(document, node)
            return {"entity": identity}, "entity", None, identity

        rule = self.rules.value_elements.get(tag)
        if rule is None:
            return None, "unsupported", None, None
        rule = rule.strip().lower()

        if tag == "meta":
            return self._attribute_value(node, "content", "text", "content")
        if tag in ("a", "link"):
            return self._url_value(document, node, "href")
        if tag == "img":
            return self._url_value(document, node, "src")
        if tag == "time":
            datetime_value = attr(node, "datetime")
            if datetime_value is not None and datetime_value.strip():
                return datetime_value.strip(), "text", "datetime", None
            return element_text(node), "text", None, None
        if tag == "data":
            return self._attribute_value(node, "value", "text", "value")
        # span/div/p and any other element declared as plain text.
        return element_text(node), "text", None, None

    @staticmethod
    def _attribute_value(node, name: str, kind: str, attribute: str):
        raw = attr(node, name)
        if raw is None:
            return None, "missing", attribute, None
        return raw, kind, attribute, None

    def _url_value(self, document: Document, node, attribute: str):
        raw = attr(node, attribute)
        if raw is None:
            return None, "missing", attribute, None
        resolved, error = resolve_iri(raw, document.base)
        if resolved is None:
            return None, "unresolvable" if error else "missing", attribute, None
        return resolved, "url", attribute, None

    # -- merging ------------------------------------------------------

    def _merge_scopes(self, scopes: list[Scope]) -> list[dict]:
        groups: dict[str, dict] = {}
        order: list[str] = []
        for scope in scopes:
            group = groups.get(scope.identity)
            if group is None:
                group = {
                    "id": scope.identity,
                    "identity_kind": scope.identity_kind,
                    "identity_source": scope.identity_source,
                    "itemid": scope.itemid_resolved,
                    "itemid_raw": scope.itemid_raw,
                    "itemtype": [],
                    "documents": [],
                    "descriptions": [],
                    "properties": [],
                    "diagnostics": [],
                    "complete": True,
                }
                groups[scope.identity] = group
                order.append(scope.identity)
            for itemtype in scope.itemtypes:
                if itemtype not in group["itemtype"]:
                    group["itemtype"].append(itemtype)
            if scope.document_id not in group["documents"]:
                group["documents"].append(scope.document_id)
            group["descriptions"].append(
                {
                    "document_id": scope.document_id,
                    "source_node_id": self._source_id_for_scope(scope),
                    "source_path": scope.path,
                    "itemid_raw": scope.itemid_raw,
                    "itemtype": list(scope.itemtypes),
                    "outer_itemprop": list(scope.outer_names),
                    "complete": scope.complete,
                }
            )
            group["properties"].extend(scope.properties)
            for diagnostic in scope.diagnostics:
                if diagnostic not in group["diagnostics"]:
                    group["diagnostics"].append(diagnostic)
            group["complete"] = group["complete"] and scope.complete

        result: list[dict] = []
        for identity in order:
            group = groups[identity]
            properties = [occurrence.to_dict() for occurrence in group["properties"]]
            value_multiset = sorted(
                (
                    [occurrence.name, canonical_value(occurrence.value), occurrence.value_kind]
                    for occurrence in group["properties"]
                ),
                key=lambda row: (row[0], row[1], row[2]),
            )
            group["properties"] = properties
            group["value_multiset"] = value_multiset
            result.append(group)
        return result

    @staticmethod
    def _source_id_for_scope(scope: Scope) -> str:
        element_id = attr(scope.node, "id")
        return element_id if element_id else scope.path


# ---------------------------------------------------------------------------
# Input handling
# ---------------------------------------------------------------------------


def read_documents(
    html_paths: list[Path],
    pages: list[dict] | None,
    scope: ScopeRules,
) -> list[dict]:
    page_by_name = {}
    if pages:
        for page in pages:
            page_by_name[Path(page["path"]).name] = page
    reader = MicrodataReader(scope)
    results = []
    for html_path in html_paths:
        page = page_by_name.get(html_path.name, {})
        document_id = page.get("document_id") or html_path.stem
        url = page.get("url")
        if not url:
            url = html_path.resolve().as_uri()
        elif "://" not in url and not url.startswith("file:"):
            url = html_path.resolve().as_uri()
        document = Document(document_id, url, html_path.read_bytes(), str(html_path))
        results.append(reader.read(document))
    return results


def comparison_signature(result: dict) -> dict:
    """Semantic view: entity identity, type and value multiset.

    Entities identified by ``itemid`` are keyed by their resolved IRI.
    Anonymous entities have no global identity, so they are compared as a
    multiset of ``(itemtypes, values)`` — this ignores source positions, which
    the task explicitly allows to move with the DOM.
    """
    identified: dict[str, dict] = {}
    anonymous: list[tuple[tuple[str, ...], tuple]] = []
    for entity in result["entities"]:
        values = tuple(
            (row[0], row[1], row[2]) for row in entity["value_multiset"]
        )
        types = tuple(sorted(entity["itemtype"]))
        if entity["identity_kind"] == "itemid":
            identified[entity["id"]] = {"itemtype": list(types), "values": values}
        else:
            anonymous.append((types, values))
    anonymous.sort()
    return {"itemid": identified, "anonymous": anonymous}


def merge_across_documents(results: list[dict]) -> dict:
    """Merge same-``itemid`` entities across the processed documents.

    Identity is the resolved IRI.  Every description, property occurrence,
    source document and diagnostic is retained; no single value is chosen.
    Anonymous entities already carry their document id in the local identity,
    so they stay separate.
    """
    groups: dict[str, dict] = {}
    order: list[str] = []
    for result in results:
        for entity in result["entities"]:
            group = groups.get(entity["id"])
            if group is None:
                group = {
                    "id": entity["id"],
                    "identity_kind": entity["identity_kind"],
                    "identity_source": entity["identity_source"],
                    "itemid": entity["itemid"],
                    "itemid_raw": entity["itemid_raw"],
                    "itemtype": [],
                    "documents": [],
                    "descriptions": [],
                    "properties": [],
                    "diagnostics": [],
                    "complete": True,
                }
                groups[entity["id"]] = group
                order.append(entity["id"])
            for itemtype in entity["itemtype"]:
                if itemtype not in group["itemtype"]:
                    group["itemtype"].append(itemtype)
            for document_id in entity["documents"]:
                if document_id not in group["documents"]:
                    group["documents"].append(document_id)
            group["descriptions"].extend(entity["descriptions"])
            group["properties"].extend(entity["properties"])
            for diagnostic in entity["diagnostics"]:
                if diagnostic not in group["diagnostics"]:
                    group["diagnostics"].append(diagnostic)
            group["complete"] = group["complete"] and entity["complete"]

    entities = []
    for identity in order:
        group = groups[identity]
        group["value_multiset"] = sorted(
            (
                [p["name"], canonical_value(p["value"]), p["value_kind"]]
                for p in group["properties"]
            ),
            key=lambda row: (row[0], row[1], row[2]),
        )
        entities.append(group)
    return {
        "documents": [
            {
                "document_id": result["document_id"],
                "source_document": result["source_document"],
                "url": result["url"],
                "base_iri": result["base_iri"],
                "base_source": result["base_source"],
            }
            for result in results
        ],
        "entities": entities,
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def parse_inputs(inputs: list[Path]):
    root = None
    html_paths: list[Path] = []
    if len(inputs) == 1 and inputs[0].is_dir():
        root = inputs[0]
        html_paths = sorted(root.glob("*.html"))
    else:
        for item in inputs:
            if item.is_dir():
                root = item
                html_paths.extend(sorted(item.glob("*.html")))
            else:
                html_paths.append(item)
    return root, html_paths


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Extract Microdata entities from frozen HTML pages."
    )
    parser.add_argument(
        "inputs",
        nargs="+",
        help="fixtures directory or one or more HTML files",
    )
    parser.add_argument("--scope", help="path to scope.json")
    parser.add_argument("--pages", help="path to pages.json")
    parser.add_argument("--json", action="store_true", help="emit full JSON records")
    parser.add_argument("--pretty", action="store_true", help="indent JSON")
    parser.add_argument(
        "--catalog",
        action="store_true",
        help="emit a cross-document catalog with same-itemid entities merged",
    )
    parser.add_argument(
        "--out",
        help="write the requested JSON/records to this file instead of stdout",
    )
    parser.add_argument(
        "--compare",
        nargs=2,
        metavar=("A.html", "B.html"),
        help="compare two pages' entity/value multisets ignoring source paths",
    )
    args = parser.parse_args(argv)

    inputs = [Path(p) for p in args.inputs]
    root, html_paths = parse_inputs(inputs)

    scope_path = Path(args.scope) if args.scope else None
    pages_path = Path(args.pages) if args.pages else None
    if root is not None:
        if scope_path is None and (root / "scope.json").exists():
            scope_path = root / "scope.json"
        if pages_path is None and (root / "pages.json").exists():
            pages_path = root / "pages.json"

    scope = ScopeRules.load(scope_path)
    pages = None
    if pages_path is not None and pages_path.exists():
        with pages_path.open("r", encoding="utf-8") as handle:
            pages = json.load(handle)

    results = read_documents(html_paths, pages, scope)

    if args.compare:
        by_name = {Path(r["source_document"]).name: r for r in results}
        left = by_name.get(Path(args.compare[0]).name)
        right = by_name.get(Path(args.compare[1]).name)
        if left is None or right is None:
            missing = [
                name
                for name, record in zip(args.compare, (left, right))
                if record is None
            ]
            print(json.dumps({"equal": False, "missing": missing}, ensure_ascii=False))
            return 2
        signature = comparison_signature(left)
        other = comparison_signature(right)
        equal = signature == other
        payload = {
            "equal": equal,
            "left": args.compare[0],
            "right": args.compare[1],
        }
        if not equal:
            left_ids = set(signature["itemid"])
            right_ids = set(other["itemid"])
            payload["only_left"] = sorted(left_ids - right_ids)
            payload["only_right"] = sorted(right_ids - left_ids)
            payload["differing"] = sorted(
                key
                for key in left_ids & right_ids
                if signature["itemid"][key] != other["itemid"][key]
            )
            if signature["anonymous"] != other["anonymous"]:
                payload["anonymous_multiset_equal"] = False
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0 if equal else 1

    if args.json or args.catalog:
        payload = merge_across_documents(results) if args.catalog else results
        text = json.dumps(
            payload, ensure_ascii=False, indent=2 if args.pretty else None
        )
        if args.out:
            Path(args.out).write_text(text + "\n", encoding="utf-8")
        else:
            print(text)
        return 0

    lines = []
    for result in results:
        properties = sum(len(e["properties"]) for e in result["entities"])
        lines.append(
            f"{result['document_id']}: {len(result['entities'])} entities, "
            f"{properties} property occurrences, "
            f"{len(result['diagnostics'])} diagnostics"
        )
        lines.append(f"  base: {result['base_iri']} ({result['base_source']})")
        for entity in result["entities"]:
            types = ", ".join(entity["itemtype"]) or "(no itemtype)"
            lines.append(
                f"  entity {entity['id']} [{entity['identity_kind']}] {types}"
            )
            for occurrence in entity["properties"]:
                name = occurrence["name"]
                value = json.dumps(occurrence["value"], ensure_ascii=False)
                via = " > ".join(occurrence["via"]) if occurrence["via"] else "-"
                lines.append(
                    f"    - {name} = {value} "
                    f"[{occurrence['value_kind']}, {occurrence['path']}, "
                    f"{occurrence['source_node_id']}, via {via}]"
                )
            if not entity["complete"]:
                lines.append("    ! incomplete")
            for diagnostic in entity["diagnostics"]:
                lines.append(f"    ! {diagnostic['code']}: {diagnostic.get('token')}")
        for reference in result["references"]:
            lines.append(
                f"  ref {reference['token']} -> {reference['status']} "
                f"({reference['owner_entity']})"
            )
    text = "\n".join(lines)
    if args.out:
        Path(args.out).write_text(text + "\n", encoding="utf-8")
    else:
        print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
