#!/usr/bin/env python3
"""Checks for microdata_reader.py against the frozen fixtures plus edge cases.

Run with::

    python3 -m unittest -v test_microdata_reader.py
"""

from __future__ import annotations

import json
import unittest
from pathlib import Path

import microdata_reader as reader

HERE = Path(__file__).resolve().parent
FIXTURES = HERE / "fixtures"


def read_fixtures() -> dict[str, dict]:
    scope = reader.ScopeRules.load(FIXTURES / "scope.json")
    pages = json.loads((FIXTURES / "pages.json").read_text(encoding="utf-8"))
    results = reader.read_documents(
        sorted(FIXTURES.glob("*.html")), pages, scope
    )
    return {Path(r["source_document"]).name: r for r in results}


def read_html(html: str, document_id: str = "t", url: str = "https://t.example/p") -> dict:
    scope = reader.ScopeRules(None)  # defaults mirror fixtures/scope.json
    document = reader.Document(document_id, url, html.encode("utf-8"), f"<{document_id}>")
    return reader.MicrodataReader(scope).read(document)


def entity(result: dict, identity: str) -> dict:
    for candidate in result["entities"]:
        if candidate["id"] == identity:
            return candidate
    raise AssertionError(f"missing entity {identity!r} in {[e['id'] for e in result['entities']]}")

class FrozenFixtureTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.docs = read_fixtures()
        cls.catalog = cls.docs["catalog.html"]
        cls.variant = cls.docs["variant.html"]

    def test_base_and_url_resolution(self):
        self.assertEqual(self.catalog["base_iri"], "https://catalog.example.test/base/")
        product = entity(self.catalog, "https://catalog.example.test/product/1")
        urls = [p for p in product["properties"] if p["name"] == "url"]
        self.assertEqual(len(urls), 1)
        self.assertEqual(urls[0]["value"], "https://catalog.example.test/base/item?id=1")
        self.assertEqual(urls[0]["value_kind"], "url")
        self.assertEqual(urls[0]["attribute"], "href")

    def test_shared_itemid_merges_identity_but_keeps_every_description(self):
        product = entity(self.catalog, "https://catalog.example.test/product/1")
        self.assertEqual(product["identity_kind"], "itemid")
        self.assertEqual(
            [d["source_node_id"] for d in product["descriptions"]],
            ["p1", "p1second"],
        )
        # Both the nested meta and the second description carry "A&B"; the
        # repeated value is kept twice with distinct source nodes.
        names = [p for p in product["properties"] if p["name"] == "name"]
        self.assertEqual(sorted(p["value"] for p in names), ["A&B", "A&B"])
        self.assertEqual(
            sorted(p["source_node_id"] for p in names), ["n1", "same-value"]
        )

    def test_same_iri_org_merges_nested_and_standalone_descriptions(self):
        org = entity(self.catalog, "https://catalog.example.test/org/1")
        self.assertEqual(
            sorted(p["value"] for p in org["properties"]),
            ["合成供应商", "合成供应商另称"],
        )
        self.assertEqual(len(org["descriptions"]), 2)

    def test_nested_entity_isolated_and_used_as_property_value(self):
        product = entity(self.catalog, "https://catalog.example.test/product/1")
        brand_values = [p for p in product["properties"] if p["name"] == "brand"]
        self.assertEqual(len(brand_values), 1)
        self.assertEqual(brand_values[0]["value_kind"], "entity")
        self.assertEqual(
            brand_values[0]["target_entity"], "https://catalog.example.test/org/1"
        )
        # The nested org's own name must not leak into the product.
        self.assertNotIn("合成供应商另称", [p["value"] for p in product["properties"]])

    def test_scope_without_itemprop_is_registered_but_not_a_property(self):
        unknown = [
            e for e in self.catalog["entities"] if e["identity_kind"] == "anonymous"
        ]
        self.assertTrue(unknown)
        self.assertTrue(
            any(e["properties"][0]["value"] == "独立对象" for e in unknown)
        )
        # No product property may point at the anonymous nested scope.
        product = entity(self.catalog, "https://catalog.example.test/product/1")
        self.assertFalse(
            [p for p in product["properties"] if p["value_kind"] == "entity"
             and "free" in p["target_entity"]]
        )

    def test_shared_itemref_visited_once_and_repeat_reported(self):
        refs = [r for r in self.catalog["references"] if r["token"] == "shared"]
        self.assertEqual([r["status"] for r in refs], ["ok", "already_visited"])
        product = entity(self.catalog, "https://catalog.example.test/product/1")
        skus = [p for p in product["properties"] if p["name"] == "sku"]
        self.assertEqual(len(skus), 1)
        self.assertEqual(skus[0]["path"], "itemref")
        self.assertEqual(skus[0]["via"], ["shared"])

    def test_itemref_through_non_scope_node(self):
        product = entity(self.catalog, "https://catalog.example.test/product/1")
        description = [p for p in product["properties"] if p["name"] == "description"]
        self.assertEqual(len(description), 1)
        self.assertEqual(description[0]["value"], "外部补充")
        self.assertEqual(description[0]["via"], ["loop"])

    def test_bad_references_preserve_known_properties(self):
        bad = [
            e for e in self.catalog["entities"]
            if any(d["code"] == "invalid_itemref" for d in e["diagnostics"])
        ]
        self.assertEqual(len(bad), 1)
        self.assertFalse(bad[0]["complete"])
        self.assertEqual([p["value"] for p in bad[0]["properties"]], ["有坏引用的对象"])
        codes = sorted(d["code"] for d in bad[0]["diagnostics"])
        self.assertEqual(codes, ["cross_document_id", "invalid_itemref"])

    def test_value_rules(self):
        product = entity(self.catalog, "https://catalog.example.test/product/1")
        by_name = {p["name"]: p for p in product["properties"]}
        # meta -> content, HTML entities decoded, two itemprop names split.
        self.assertEqual(by_name["name"]["value"], "A&B")
        self.assertEqual(by_name["name"]["value_kind"], "text")
        self.assertEqual(by_name["alternateName"]["value"], "A&B")
        # data -> value attribute, not the display text.
        self.assertEqual(by_name["sku"]["value"], "001")
        self.assertEqual(by_name["sku"]["attribute"], "value")
        # time prefers datetime.
        self.assertEqual(by_name["releaseDate"]["value"], "2026-11-15")
        self.assertEqual(by_name["releaseDate"]["attribute"], "datetime")

    def test_variant_is_semantically_equal(self):
        left = reader.comparison_signature(self.catalog)
        right = reader.comparison_signature(self.variant)
        self.assertEqual(left, right)

    def test_anonymous_identity_is_not_merged_by_equal_text(self):
        result = read_html(
            '<div itemscope><span itemprop="v">same</span></div>'
            '<div itemscope><span itemprop="v">same</span></div>'
        )
        anonymous = [e for e in result["entities"] if e["identity_kind"] == "anonymous"]
        self.assertEqual(len(anonymous), 2)
        self.assertNotEqual(anonymous[0]["id"], anonymous[1]["id"])
        self.assertEqual(
            [e["value_multiset"] for e in anonymous],
            [[["v", "same", "text"]], [["v", "same", "text"]]],
        )


class EdgeCaseTests(unittest.TestCase):
    def test_first_usable_base_wins_and_falls_back_to_page_url(self):
        result = read_html(
            '<base><base href=""><base href="https://b.example/x/">'
            '<div itemscope itemid="i"><a itemprop="u" href="y">y</a></div>'
        )
        self.assertEqual(result["base_iri"], "https://b.example/x/")
        self.assertEqual(result["base_source"], "base_href")
        self.assertEqual(result["diagnostics"][0]["code"], "base_missing_href")
        self.assertEqual(
            entity(result, "https://b.example/x/i")["properties"][0]["value"],
            "https://b.example/x/y",
        )
        fallback = read_html('<div itemscope itemid="j"></div>', url="https://p.example/q")
        self.assertEqual(fallback["base_iri"], "https://p.example/q")
        self.assertEqual(fallback["base_source"], "page_url")

    def test_time_falls_back_to_text_and_data_requires_value(self):
        result = read_html(
            '<div itemscope itemid="i">'
            '<time itemprop="t">when</time><data itemprop="d">hide</data></div>'
        )
        self.assertEqual(result["entities"][0]["properties"], [
            {
                "name": "t",
                "itemprop": "t",
                "value": "when",
                "value_kind": "text",
                "source_document": "t",
                "source_node_id": "html/body[0]/div[0]/time[0]",
                "source_path": "html/body[0]/div[0]/time[0]",
                "path": "direct",
                "via": [],
                "element": "time",
            },
            {
                "name": "d",
                "itemprop": "d",
                "value": None,
                "value_kind": "missing",
                "source_document": "t",
                "source_node_id": "html/body[0]/div[0]/data[0]",
                "source_path": "html/body[0]/div[0]/data[0]",
                "path": "direct",
                "via": [],
                "element": "data",
                "attribute": "value",
            },
        ])
        self.assertFalse(result["entities"][0]["complete"])
        self.assertEqual(
            result["entities"][0]["diagnostics"][0]["code"],
            "missing_required_attribute",
        )

    def test_unsupported_element_is_diagnosed_without_guessing(self):
        result = read_html(
            '<div itemscope itemid="i"><section itemprop="s">x</section>'
            '<span itemprop="ok">y</span></div>'
        )
        entity_record = result["entities"][0]
        self.assertFalse(entity_record["complete"])
        self.assertEqual(entity_record["diagnostics"][0]["code"], "unsupported_element")
        unsupported = [
            p for p in entity_record["properties"] if p["name"] == "s"
        ]
        self.assertEqual(unsupported[0]["value"], None)
        self.assertEqual(unsupported[0]["value_kind"], "unsupported")

    def test_ancestor_and_self_itemrefs_do_not_loop(self):
        result = read_html(
            '<div id="wrap"><div id="self" itemscope itemid="i" itemref="self wrap">'
            '<span itemprop="n">n</span></div></div>'
        )
        statuses = [r["status"] for r in result["references"]]
        self.assertEqual(statuses, ["already_visited", "cyclic_itemref"])
        self.assertEqual(len(result["entities"][0]["properties"]), 1)

    def test_nested_scope_internal_names_do_not_leak_via_itemref(self):
        result = read_html(
            '<div id="outer" itemscope itemid="outer" itemref="deco">'
            '<div id="inner" itemprop="child" itemscope itemid="inner">'
            '<span id="secret" itemprop="name">inner</span></div></div>'
            '<div id="deco"><span itemprop="decoration">d</span></div>'
        )
        outer = entity(result, "https://t.example/outer")
        self.assertEqual(
            sorted(p["name"] for p in outer["properties"]),
            ["child", "decoration"],
        )
        self.assertNotIn("inner", [p["value"] for p in outer["properties"]])
        inner = entity(result, "https://t.example/inner")
        self.assertEqual([p["value"] for p in inner["properties"]], ["inner"])

    def test_cross_document_itemref_and_duplicate_id(self):
        result = read_html(
            '<div id="dup"></div><div id="dup"></div>'
            '<div itemscope itemid="i" itemref="dup nope other.html#x"></div>'
        )
        self.assertEqual(result["entities"][0]["identity_kind"], "itemid")
        codes = sorted(d["code"] for d in result["entities"][0]["diagnostics"])
        self.assertEqual(codes, ["cross_document_id", "invalid_itemref"])
        self.assertIn("duplicate_id", [d["code"] for d in result["diagnostics"]])

    def test_multiple_property_names_each_get_their_own_occurrence(self):
        result = read_html(
            '<div itemscope itemid="i">'
            '<meta itemprop="one two" content="v"></div>'
        )
        names = [p["name"] for p in result["entities"][0]["properties"]]
        self.assertEqual(names, ["one", "two"])
        for prop in result["entities"][0]["properties"]:
            self.assertEqual(prop["itemprop"], "one two")

    def test_whitespace_text_collapses(self):
        result = read_html(
            '<div itemscope itemid="i"><p itemprop="p">  a \n\t b  </p></div>'
        )
        self.assertEqual(result["entities"][0]["properties"][0]["value"], "a b")

    def test_plain_div_itemref_is_not_interpreted(self):
        result = read_html(
            '<div id="ext"><span itemprop="x">x</span></div>'
            '<div id="plain" itemref="ext"></div>'
        )
        self.assertEqual(result["entities"], [])
        self.assertEqual(result["references"], [])


class BoundaryAndCatalogTests(unittest.TestCase):
    def test_explicit_itemref_into_nested_scope_does_not_leak(self):
        result = read_html(
            '<div id="outer" itemscope itemid="o" itemref="secret">'
            '<div id="inner" itemprop="c" itemscope itemid="i">'
            '<span id="secret" itemprop="name">leak</span></div></div>'
        )
        outer = entity(result, "https://t.example/o")
        self.assertEqual([p["name"] for p in outer["properties"]], ["c"])
        statuses = [r["status"] for r in result["references"]]
        self.assertEqual(statuses, ["nested_boundary"])

    def test_cross_document_same_itemid_is_merged_into_catalog(self):
        scope = reader.ScopeRules(None)
        left = reader.MicrodataReader(scope).read(
            reader.Document(
                "a", "https://t.example/a", b'<div itemscope itemid="shared"><span itemprop="n">one</span></div>', "<a>"
            )
        )
        right = reader.MicrodataReader(scope).read(
            reader.Document(
                "b", "https://t.example/b", b'<div itemscope itemid="https://t.example/shared"><span itemprop="n">two</span></div>', "<b>"
            )
        )
        catalog = reader.merge_across_documents([left, right])
        self.assertEqual(len(catalog["entities"]), 1)
        merged = catalog["entities"][0]
        self.assertEqual(merged["documents"], ["a", "b"])
        self.assertEqual(len(merged["descriptions"]), 2)
        self.assertEqual(
            sorted(p["value"] for p in merged["properties"]), ["one", "two"]
        )
        self.assertEqual(
            merged["value_multiset"],
            [["n", "one", "text"], ["n", "two", "text"]],
        )


if __name__ == "__main__":
    unittest.main()
