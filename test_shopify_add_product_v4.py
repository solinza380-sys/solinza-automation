"""Tests hors ligne de shopify_add_product_v4.py (aucun appel réseau).
Lancer :  python -m unittest -v test_shopify_add_product_v4
Aucun fichier V3 n'est importé, lu ou modifié par ces tests.
"""
import io
import json
import os
import contextlib
import unittest
from pathlib import Path
from unittest import mock

import shopify_add_product_v4 as v4

FULL_SCOPES = {"read_products", "write_products", "read_inventory", "write_inventory", "read_locations"}
LOCATION = {"id": "gid://shopify/Location/1", "name": "Shop location", "isActive": True}


def variant(i, name, cost=10.94, weight=None):
    return {"vid": f"vid{i}", "variantNameEn": name, "variantSku": f"SKU{i:02d}",
            "variantSellPrice": cost, "variantWeight": weight}


CANDIDATE = {"pid": "P1", "productNameEn": "Women's Cowboy Long-Sleeve Fashion Dress",
             "categoryName": "Lady Dresses", "productImage": "https://img/1.jpg", "sellPrice": "10.94",
             "keyword": "flowy dress women"}
COMPLETE = {**CANDIDATE, "pid": "P3", "productNameEn": "Women Long Sleeve Dress"}


# ----------------------------------------------------------------------
# Règles métier pures
# ----------------------------------------------------------------------
class CostTest(unittest.TestCase):
    def test_real_2xl_total_cost(self):
        total = v4.total_cost_usd(9.00, 8.44, 3.50)
        self.assertEqual(total, 20.94)          # 9,00 + 8,44 + 3,50
        self.assertNotEqual(total, 23.88)       # 11,94 n'est PAS le prix du produit

    def test_variant_economics_uses_the_three_components(self):
        e = v4.variant_economics(9.00, 8.44, 3.50)
        self.assertEqual((e["total_usd"], e["total_eur"], e["price"]), (20.94, 18.85, 28.26))
        self.assertEqual(e["margin_eur"], round(e["revenue_ht"] - 18.85, 2))

    def test_usd_to_eur_conversion_with_explicit_config(self):
        self.assertEqual(v4.USD_TO_EUR, 0.90)
        self.assertEqual(v4.usd_to_eur(20.94), 18.85)
        with mock.patch.object(v4, "USD_TO_EUR", 0.5):
            self.assertEqual(v4.usd_to_eur(20.94), 10.47)

    def test_min_price(self):
        self.assertEqual(v4.sell_price(2.0), 24.99)


class ColorTest(unittest.TestCase):
    def test_known_colors(self):
        for src, fr in [("Dark Blue", "Bleu foncé"), ("Black", "Noir"), ("White", "Blanc"), ("Red", "Rouge"),
                        ("Green", "Vert"), ("Pink", "Rose"), ("Beige", "Beige"), ("Brown", "Marron"),
                        ("Grey", "Gris"), ("Gray", "Gris")]:
            self.assertEqual(v4.color_fr(src), fr, src)

    def test_unknown_color_is_kept_exactly(self):
        self.assertEqual(v4.color_fr("Midnight Teal"), "Midnight Teal")
        self.assertEqual(v4.split_color_size("Midnight-Teal M"), ("Midnight-Teal", "M"))

    def test_split_color_size(self):
        self.assertEqual(v4.split_color_size("Dark Blue S"), ("Dark Blue", "S"))
        self.assertEqual(v4.split_color_size("Dark Blue-2XL"), ("Dark Blue", "2XL"))
        self.assertIsNone(v4.split_color_size("One piece"))


class OptionsTest(unittest.TestCase):
    def test_two_colors(self):
        vs = [{"name": n} for n in ["Black S", "Black M", "Dark Blue S", "Dark Blue M"]]
        mode, options, combos = v4.build_options(vs)
        self.assertEqual(mode, "color_size")
        self.assertEqual(options, [("Couleur", ["Noir", "Bleu foncé"]), ("Taille", ["S", "M"])])
        self.assertEqual(combos[0], ("Noir", "S"))

    def test_one_color_uses_size_only(self):
        mode, options, combos = v4.build_options([{"name": f"Dark Blue {s}"} for s in ["S", "M", "L"]])
        self.assertEqual((mode, options), ("size", [("Taille", ["S", "M", "L"])]))

    def test_fallback_and_single(self):
        self.assertEqual(v4.build_options([{"name": "Style A"}, {"name": "Style B"}])[0], "single")
        self.assertEqual(v4.build_options([{"name": "X"}])[0], "none")


class FitFilterTest(unittest.TestCase):
    def test_accepts_womens_clothing(self):
        for t in ["Women's Cowboy Long-Sleeve Fashion Dress", "Women Blouse", "V neck sweater women",
                  "Women Trench Coat", "Women Denim Jacket", "Women Jeans", "Women Pleated Skirt", "Women Casual Top"]:
            self.assertEqual(v4.fit_check(t), [], t)

    def test_shoes_are_rejected(self):
        for t in ["Women's Casual Shoes", "Women Sandals", "Women Ankle Boots", "Women Sneakers",
                  "Women High Heels", "Women's Dress Shoes"]:
            self.assertTrue(v4.fit_check(t), t)

    def test_other_excluded_families(self):
        for t in ["Women Leather Wallet", "Commuter Bag For Women", "Women Gold Necklace", "Men's Hoodie",
                  "Kids Dress", "Dog Sweater", "Phone Case", "Home Decor Curtain", "Fun Gadget"]:
            self.assertTrue(v4.fit_check(t), t)

    def test_women_is_not_matched_as_men(self):
        self.assertEqual(v4.fit_check("Women dress"), [])


class TranslationUnitTest(unittest.TestCase):
    def test_incomplete_translation(self):
        r = v4.translate_title("Women's Cowboy Long-Sleeve Fashion Dress")
        self.assertFalse(r["complete"])
        self.assertEqual(r["english_left"], ["Cowboy", "Fashion"])
        self.assertEqual(r["title_fr"], "Robe Femme à manches longues Cowboy Fashion")

    def test_complete_translation(self):
        r = v4.translate_title("Women Long Sleeve Dress")
        self.assertTrue(r["complete"])
        self.assertEqual((r["title_fr"], r["english_left"]), ("Robe Femme à manches longues", []))
        self.assertEqual(v4.translate_title("Women V-Neck Knitted Sweater")["title_fr"], "Pull Femme col en V en maille")

    def test_no_known_garment_type_is_incomplete_not_an_error(self):
        r = v4.translate_title("Mystery product")
        self.assertFalse(r["complete"])
        self.assertEqual(r["title_fr"], "Mystery product")

    def test_second_garment_word_is_flagged(self):
        self.assertFalse(v4.translate_title("Sweater Dress")["complete"])


class LimitsUnitTest(unittest.TestCase):
    def test_defaults_are_explicit(self):
        self.assertEqual(v4.MAX_VARIANTS_PER_CANDIDATE, 12)
        self.assertEqual(v4.MAX_CANDIDATES, 3)
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("MAX_VARIANTS_PER_CANDIDATE", None)
            os.environ.pop("MAX_CANDIDATES", None)
            self.assertEqual((v4.max_candidates(), v4.max_variants_per_candidate()), (3, 12))

    def test_env_override_and_validation(self):
        with mock.patch.dict(os.environ, {"MAX_VARIANTS_PER_CANDIDATE": "5"}):
            self.assertEqual(v4.max_variants_per_candidate(), 5)
        with mock.patch.dict(os.environ, {"MAX_VARIANTS_PER_CANDIDATE": "0"}):
            with self.assertRaises(RuntimeError):
                v4.max_variants_per_candidate()
        with mock.patch.dict(os.environ, {"MAX_CANDIDATES": "abc"}):
            with self.assertRaises(RuntimeError):
                v4.max_candidates()


# ----------------------------------------------------------------------
# Preflight (lecture seule) : tests unitaires
# ----------------------------------------------------------------------
def fake_graphql(scopes, locations=None, record=None):
    locations = [LOCATION] if locations is None else locations

    def graphql(shop, token, query, variables=None):
        if record is not None:
            record.append(query)
        if "currentAppInstallation" in query:
            return {"currentAppInstallation": {"accessScopes": [{"handle": s} for s in sorted(scopes)]}}
        if "locations(first" in query:
            return {"locations": {"nodes": locations}}
        raise AssertionError("requête inattendue pendant le preflight : " + query)
    return graphql


class PreflightUnitTest(unittest.TestCase):
    def run_pf(self, scopes, locations=None):
        calls = []
        with mock.patch.object(v4, "graphql", side_effect=fake_graphql(scopes, locations, calls)):
            try:
                return v4.preflight("shop", "tok"), calls, None
            except v4.PreflightError as e:
                return None, calls, str(e)

    def test_read_locations_alone_is_enough(self):
        pf, _, err = self.run_pf({"read_locations", "write_inventory"})
        self.assertIsNone(err)
        self.assertEqual(pf["location_id"], LOCATION["id"])
        self.assertEqual(pf["read_scopes_present"], ["read_locations"])

    def test_read_inventory_alone_is_enough(self):
        pf, _, err = self.run_pf({"read_inventory", "write_inventory"})
        self.assertIsNone(err)
        self.assertEqual(pf["read_scopes_present"], ["read_inventory"])

    def test_both_read_scopes_ok(self):
        pf, _, err = self.run_pf({"read_locations", "read_inventory", "write_inventory"})
        self.assertIsNone(err)
        self.assertEqual(pf["read_scopes_present"], ["read_locations", "read_inventory"])

    def test_neither_read_scope_fails_and_names_both(self):
        pf, calls, err = self.run_pf({"write_inventory", "read_products"})
        self.assertIsNone(pf)
        self.assertIn("read_locations", err)
        self.assertIn("read_inventory", err)
        self.assertIn("aucun scope Shopify requis pour lire les locations", err)
        self.assertIn("Aucune mutation effectuée", err)
        self.assertEqual(len(calls), 1)                   # seule la requête des scopes a été faite

    def test_missing_write_inventory_names_exact_scope(self):
        pf, calls, err = self.run_pf({"read_locations"})
        self.assertIsNone(pf)
        self.assertIn("scope Shopify manquant : write_inventory", err)
        self.assertNotIn("permission Shopify manquante", err)   # jamais de message générique
        self.assertEqual(len(calls), 1)                   # arrêt avant de chercher les locations

    def test_read_scope_check_comes_before_write_inventory_check(self):
        _, _, err = self.run_pf(set())
        self.assertIn("read_locations", err)
        self.assertNotIn("scope Shopify manquant : write_inventory", err)

    def test_no_active_location_stops(self):
        pf, _, err = self.run_pf(FULL_SCOPES, [{"id": "gid://shopify/Location/9", "name": "Old", "isActive": False}])
        self.assertIsNone(pf)
        self.assertIn("locationId", err)
        self.assertIn("Aucune mutation effectuée", err)

    def test_query_order_and_zero_mutation(self):
        pf, calls, err = self.run_pf(FULL_SCOPES)
        self.assertIsNone(err)
        self.assertIn("currentAppInstallation", calls[0])
        self.assertIn("locations(first", calls[1])
        self.assertTrue(all("mutation" not in q for q in calls))


# ----------------------------------------------------------------------
# Flux complet avec CJ et Shopify simulés
# ----------------------------------------------------------------------
class FlowTest(unittest.TestCase):
    def setUp(self):
        self.env = mock.patch.dict(os.environ, {"SHOPIFY_STORE": "x.myshopify.com", "SHOPIFY_CLIENT_ID": "i",
                                                "SHOPIFY_CLIENT_SECRET": "s", "CJ_API_KEY": "k"})
        self.env.start()
        for name in ("MAX_CANDIDATES", "MAX_VARIANTS_PER_CANDIDATE"):
            os.environ.pop(name, None)
        self.lines, self.calls, self.cj_calls = [], [], []
        self.stock_calls = 0

    def tearDown(self):
        self.env.stop()

    def run_main(self, apply, existing_tag=False, existing_sku=False, cj_variants=None, ship_usd=None,
                 stock=None, candidates=None, variants_by_pid=None, scopes=None, locations=None, expect_exit=False):
        """ship_usd : {vid: livraison USD} (défaut 7,63) ; stock : {vid: stock} (défaut 500)."""
        candidates = candidates or [CANDIDATE]
        titles = {c["pid"]: c["productNameEn"] for c in candidates}
        names = ["Dark Blue S", "Dark Blue M", "Dark Blue L"]
        cj_variants = cj_variants or [variant(i + 1, n, weight=380 + 20 * i) for i, n in enumerate(names)]
        variants_by_pid = variants_by_pid or {}
        ship_usd, stock = ship_usd or {}, stock or {}
        scopes = FULL_SCOPES if scopes is None else scopes
        locations = [LOCATION] if locations is None else locations

        def graphql(shop, token, query, variables=None):
            self.calls.append((query, variables))
            if "currentAppInstallation" in query:
                return {"currentAppInstallation": {"accessScopes": [{"handle": s} for s in sorted(scopes)]}}
            if "locations(first" in query:
                return {"locations": {"nodes": locations}}
            if "products(first: 1" in query:
                q = variables["q"]
                hit = (q.startswith("tag:") and existing_tag) or (q.startswith("sku:") and existing_sku)
                return {"products": {"nodes": [{"id": "gid://x"}] if hit else []}}
            if "productCreate(" in query:
                opts = variables["product"].get("productOptions") or []
                sel = [{"name": o["name"], "value": o["values"][0]["name"]} for o in opts]
                return {"productCreate": {"product": {"id": "gid://shopify/Product/1", "title": variables["product"]["title"],
                        "status": variables["product"]["status"],
                        "variants": {"nodes": [{"id": "gid://v/1", "selectedOptions": sel, "inventoryItem": {"id": "gid://ii/1"}}]}},
                        "userErrors": []}}
            if "productVariantsBulkUpdate(" in query:
                sku = variables["variants"][0]["inventoryItem"]["sku"]
                return {"productVariantsBulkUpdate": {"productVariants": [
                    {"id": "gid://v/1", "price": "1", "inventoryItem": {"id": "gid://ii/1", "sku": sku}}], "userErrors": []}}
            if "productVariantsBulkCreate(" in query:
                pvs = [{"id": f"gid://v/{k}", "inventoryItem": {"id": f"gid://ii/{k}", "sku": x["inventoryItem"]["sku"]}}
                       for k, x in enumerate(variables["variants"], start=2)]
                return {"productVariantsBulkCreate": {"productVariants": pvs, "userErrors": []}}
            if "inventoryActivate(" in query:
                return {"inventoryActivate": {"inventoryLevel": {"id": "gid://il/1"}, "userErrors": []}}
            if "inventorySetQuantities(" in query:
                return {"inventorySetQuantities": {"inventoryAdjustmentGroup": {"id": "gid://iag/1"}, "userErrors": []}}
            raise AssertionError(query)

        def cj_call(method, path, token=None, **kw):
            self.cj_calls.append(path)
            if path == "product/query":
                pid = kw["params"]["pid"]
                return {"productNameEn": titles[pid], "categoryName": "Lady Dresses",
                        "productImageSet": ["https://img/1.jpg", "https://img/2.jpg"],
                        "productSku": f"CJ-{pid}", "materialNameEn": ["Cloth"], "productWeight": "375.00"}
            raise AssertionError(path)

        def get_stock(cj_token, vid):
            self.stock_calls += 1
            return stock.get(vid, 500)

        def get_shipping(cj_token, vid):
            return {"price_usd": ship_usd.get(vid, 7.63), "aging": "8-15", "name": "YunExpress",
                    "raw": {"logisticName": "YunExpress"}}

        patches = [
            mock.patch.object(v4, "get_shopify_token", return_value="tok"),
            mock.patch.object(v4, "get_cj_token", return_value="cj"),
            mock.patch.object(v4, "load_candidates", return_value=candidates),
            mock.patch.object(v4, "graphql", side_effect=graphql),
            mock.patch.object(v4, "cj_call", side_effect=cj_call),
            mock.patch.object(v4, "get_variants", side_effect=lambda t, pid: variants_by_pid.get(pid, cj_variants)),
            mock.patch.object(v4, "get_stock", side_effect=get_stock),
            mock.patch.object(v4, "get_shipping", side_effect=get_shipping),
            mock.patch.object(v4, "write_summary", side_effect=lambda lines: self.lines.extend(lines)),
        ]
        for p in patches:
            p.start()
        try:
            with mock.patch.dict(os.environ, {"APPLY": "1" if apply else "0"}), contextlib.redirect_stdout(io.StringIO()):
                if expect_exit:
                    with self.assertRaises(SystemExit) as cm:
                        v4.main()
                    self.assertEqual(cm.exception.code, 1)
                else:
                    v4.main()
        finally:
            for p in patches:
                p.stop()
        return "\n".join(self.lines)

    def mutation_names(self):
        names = []
        for q, _ in self.calls:
            if "mutation" in q:
                for n in ("productCreate", "productVariantsBulkUpdate", "productVariantsBulkCreate",
                          "inventoryActivate", "inventorySetQuantities"):
                    if f"{n}(" in q:
                        names.append(n)
        return names

    def vars_of(self, name):
        return [v for q, v in self.calls if f"{name}(" in q][0]

    # --- Preflight : bloquant, y compris en simulation --------------------------------
    def assert_blocked(self, out):
        self.assertEqual(self.mutation_names(), [])
        self.assertEqual(self.cj_calls, [])                        # aucun appel CJ
        self.assertEqual(len(self.calls), 1)                       # seule la lecture des scopes
        for forbidden in ("inventoryActivate", "inventorySetQuantities", "productCreate"):
            self.assertTrue(all(forbidden + "(" not in q for q, _ in self.calls))

    def test_no_read_scope_stops_in_apply_mode(self):
        out = self.run_main(apply=True, scopes={"write_inventory", "write_products"}, expect_exit=True)
        self.assert_blocked(out)
        self.assertIn("read_locations", out)
        self.assertIn("read_inventory", out)
        self.assertIn("Aucune mutation effectuée", out)

    def test_preflight_also_runs_in_dry_run(self):
        out = self.run_main(apply=False, scopes={"write_inventory"}, expect_exit=True)
        self.assert_blocked(out)
        self.assertIn("aucun scope Shopify requis pour lire les locations", out)

    def test_missing_write_inventory_stops_with_exact_scope(self):
        out = self.run_main(apply=True, scopes={"read_locations", "read_inventory"}, expect_exit=True)
        self.assertEqual(self.mutation_names(), [])
        self.assertIn("scope Shopify manquant : write_inventory", out)
        self.assertEqual(self.cj_calls, [])

    def test_no_usable_location_stops(self):
        out = self.run_main(apply=True, locations=[{"id": "gid://shopify/Location/9", "name": "x", "isActive": False}],
                            expect_exit=True)
        self.assertEqual(self.mutation_names(), [])
        self.assertIn("locationId", out)

    def test_either_read_scope_lets_the_workflow_continue(self):
        for scopes in ({"read_locations", "write_inventory"}, {"read_inventory", "write_inventory"}):
            self.lines, self.calls, self.cj_calls = [], [], []
            out = self.run_main(apply=False, scopes=scopes)
            self.assertIn("Preflight Shopify (lecture seule) : réussi", out)
            self.assertIn("SIMULATION", out)

    def test_preflight_is_first_and_read_only(self):
        self.run_main(apply=True)
        self.assertIn("currentAppInstallation", self.calls[0][0])
        self.assertIn("locations(first", self.calls[1][0])
        self.assertEqual(self.cj_calls[:1], ["product/query"])
        first_mutation = next(i for i, (q, _) in enumerate(self.calls) if "mutation" in q)
        self.assertGreater(first_mutation, 1)

    # --- Dry run / création -------------------------------------------------------------
    def test_dry_run_never_writes_and_shows_limits_rate_and_draft(self):
        out = self.run_main(apply=False)
        self.assertEqual(self.mutation_names(), [])
        self.assertIn("SIMULATION", out)
        self.assertIn("MAX_CANDIDATES = 3", out)
        self.assertIn("MAX_VARIANTS_PER_CANDIDATE = 12", out)
        self.assertIn("Taux USD→EUR utilisé : 0.90", out)
        self.assertIn("pas un taux de marché en temps réel", out)
        self.assertIn("DRAFT", out)
        self.assertIn("Livraison USD", out)

    def test_apply_sequence_inventory_and_payloads(self):
        out = self.run_main(apply=True)
        self.assertEqual(self.mutation_names(), ["productCreate", "productVariantsBulkUpdate", "productVariantsBulkCreate",
                                                 "inventoryActivate", "inventoryActivate", "inventoryActivate",
                                                 "inventorySetQuantities"])
        product = self.vars_of("productCreate")["product"]
        self.assertEqual(product["status"], "DRAFT")
        inv = self.vars_of("productVariantsBulkUpdate")["variants"][0]["inventoryItem"]
        self.assertEqual(inv["measurement"], {"weight": {"value": 380.0, "unit": "GRAMS"}})
        self.assertTrue(inv["tracked"])
        self.assertEqual(inv["cost"], "9.85")
        bulk = self.vars_of("productVariantsBulkCreate")["variants"]
        self.assertEqual(bulk[0]["optionValues"], [{"optionName": "Taille", "name": "M"}])
        qty = self.vars_of("inventorySetQuantities")["input"]
        self.assertEqual((qty["name"], qty["reason"]), ("available", "correction"))
        self.assertEqual([(q["inventoryItemId"], q["locationId"], q["quantity"], q["changeFromQuantity"])
                          for q in qty["quantities"]],
                         [(f"gid://ii/{k}", LOCATION["id"], 500, None) for k in (1, 2, 3)])
        self.assertIn("Brouillon créé", out)

    def test_existing_tag_skips_everything(self):
        out = self.run_main(apply=True, existing_tag=True)
        self.assertEqual(self.mutation_names(), [])
        self.assertIn("Aucun produit retenu", out)

    def test_existing_sku_blocks_duplicate(self):
        out = self.run_main(apply=True, existing_sku=True)
        self.assertEqual(self.mutation_names(), [])
        self.assertIn("Doublon", out)

    # --- Traduction : jamais bloquante, produit toujours DRAFT --------------------------
    def test_incomplete_translation_creates_draft_with_a_corriger(self):
        out = self.run_main(apply=True, candidates=[CANDIDATE])             # « Cowboy » et « Fashion » restent
        self.assertIn("productCreate", self.mutation_names())
        product = self.vars_of("productCreate")["product"]
        self.assertIn("a-corriger", product["tags"])
        self.assertEqual(product["status"], "DRAFT")
        self.assertEqual(product["title"], "Robe Femme à manches longues Cowboy Fashion")
        self.assertIn("Traduction du titre : incomplète", out)
        self.assertIn("le produit est créé quand même", out)
        self.assertNotIn("translation failure", out.lower())
        self.assertNotIn("Erreur", out)

    def test_complete_translation_has_no_a_corriger_and_is_still_draft(self):
        out = self.run_main(apply=True, candidates=[COMPLETE])
        product = self.vars_of("productCreate")["product"]
        self.assertNotIn("a-corriger", product["tags"])
        self.assertEqual(product["status"], "DRAFT")
        self.assertEqual(product["title"], "Robe Femme à manches longues")
        self.assertIn("Traduction du titre : complète", out)

    def test_status_is_always_draft_in_all_cases(self):
        for cand in (CANDIDATE, COMPLETE):
            self.calls = []
            self.run_main(apply=True, candidates=[cand])
            self.assertEqual(self.vars_of("productCreate")["product"]["status"], "DRAFT")

    def test_create_product_refuses_any_status_other_than_draft(self):
        plan = {"product_input": {"status": "ACTIVE"}, "media": [], "variants": [], "options": [], "combos": []}
        with mock.patch.object(v4, "graphql") as g:
            with self.assertRaises(RuntimeError):
                v4.create_product("shop", "tok", plan, "gid://shopify/Location/1", "P1")
            g.assert_not_called()

    def test_unknown_color_creates_product_and_flags_it(self):
        names = ["Black S", "Black M", "Midnight Teal S", "Midnight Teal M"]
        cjv = [variant(i + 1, n, weight=400) for i, n in enumerate(names)]
        self.run_main(apply=True, candidates=[COMPLETE], cj_variants=cjv)     # titre complet : seule la couleur reste anglaise
        product = self.vars_of("productCreate")["product"]
        self.assertEqual(product["productOptions"][0],
                         {"name": "Couleur", "values": [{"name": "Noir"}, {"name": "Midnight Teal"}]})
        self.assertIn("a-corriger", product["tags"])
        self.assertEqual(product["status"], "DRAFT")

    # --- Limites ------------------------------------------------------------------------
    def test_never_audits_more_than_12_variants_per_candidate(self):
        names = [f"Color{i} S" for i in range(20)]
        cjv = [variant(i + 1, n, weight=300) for i, n in enumerate(names)]
        out = self.run_main(apply=True, candidates=[COMPLETE], cj_variants=cjv)
        self.assertEqual(v4.MAX_VARIANTS_PER_CANDIDATE, 12)
        self.assertEqual(self.stock_calls, 12)                                # jamais plus de 12 audits
        self.assertIn("variantes auditées : 12 (max 12 par candidat)", out)
        self.assertIn("Variantes CJ : 20 ; auditées : 12", out)
        created = 1 + len(self.vars_of("productVariantsBulkCreate")["variants"])
        self.assertLessEqual(created, 12)

    def test_variant_limit_is_configurable(self):
        names = [f"Color{i} S" for i in range(20)]
        cjv = [variant(i + 1, n) for i, n in enumerate(names)]
        with mock.patch.dict(os.environ, {"MAX_VARIANTS_PER_CANDIDATE": "5"}):
            out = self.run_main(apply=False, candidates=[COMPLETE], cj_variants=cjv)
        self.assertEqual(self.stock_calls, 5)
        self.assertIn("MAX_VARIANTS_PER_CANDIDATE = 5", out)

    def test_max_candidates_limits_audits_to_three(self):
        cands = [{**COMPLETE, "pid": f"Q{i}", "productNameEn": f"Women Long Sleeve Dress {i}"} for i in range(5)]
        out = self.run_main(apply=False, candidates=cands)
        self.assertEqual(self.cj_calls.count("product/query"), 3)
        self.assertIn("audités : 3/3", out)

    def test_ranking_selects_the_best_margin_and_only_one_product(self):
        a = {**COMPLETE, "pid": "A", "productNameEn": "Women Long Sleeve Dress"}
        b = {**COMPLETE, "pid": "B", "productNameEn": "Women Pleated Skirt"}
        by_pid = {"A": [variant(1, "Dark Blue S", cost=10.94), variant(2, "Dark Blue M", cost=10.94)],
                  "B": [variant(3, "Dark Blue S", cost=14.0), variant(4, "Dark Blue M", cost=14.0)]}
        out = self.run_main(apply=True, candidates=[a, b], variants_by_pid=by_pid)
        self.assertEqual(self.mutation_names().count("productCreate"), 1)
        self.assertIn("cj-pid-B", self.vars_of("productCreate")["product"]["tags"])
        self.assertIn("Classement 1 : Women Pleated Skirt", out)

    # --- Rentabilité par variante -------------------------------------------------------
    def sizes_with_one_deficit(self):
        names = ["Dark Blue S", "Dark Blue M", "Dark Blue L", "Dark Blue 2XL"]
        cjv = [variant(i + 1, n, weight=400) for i, n in enumerate(names)]
        return cjv, {"vid1": 7.63, "vid2": 7.90, "vid3": 8.10, "vid4": 12.00}

    def test_one_deficit_size_is_excluded_dry_run(self):
        cjv, ship = self.sizes_with_one_deficit()
        out = self.run_main(apply=False, cj_variants=cjv, ship_usd=ship)
        self.assertEqual(self.mutation_names(), [])
        self.assertIn("Taille: S, M, L", out)
        self.assertNotIn("Taille: S, M, L, 2XL", out)
        self.assertIn("Dark Blue 2XL — EXCLUE", out)
        self.assertIn("Marge insuffisante", out)
        self.assertIn("Coût total : 26.44 USD", out)
        self.assertNotIn("Produit rejeté", out)

    def test_deficit_size_is_never_created_nor_stocked(self):
        cjv, ship = self.sizes_with_one_deficit()
        self.run_main(apply=True, cj_variants=cjv, ship_usd=ship)
        created = self.vars_of("productVariantsBulkCreate")["variants"]
        self.assertEqual([c["optionValues"][0]["name"] for c in created], ["M", "L"])
        self.assertEqual(len(self.vars_of("inventorySetQuantities")["input"]["quantities"]), 3)
        self.assertNotIn("2XL", json.dumps(created))

    def test_all_sizes_deficit_rejects_whole_product(self):
        cjv = [variant(i + 1, n) for i, n in enumerate(["Dark Blue S", "Dark Blue M", "Dark Blue L"])]
        out = self.run_main(apply=True, cj_variants=cjv, ship_usd={"vid1": 15, "vid2": 15, "vid3": 15})
        self.assertEqual(self.mutation_names(), [])
        self.assertIn("Produit rejeté", out)
        self.assertIn("aucune variante rentable", out)
        self.assertEqual(out.count("— EXCLUE"), 3)

    def test_low_stock_variant_is_excluded_with_reason(self):
        out = self.run_main(apply=False, stock={"vid2": 5})
        self.assertIn("Stock insuffisant (5 < 100)", out)
        self.assertIn("Taille: S, L", out)

    def test_shoes_rejected_before_any_cj_call(self):
        shoes = {**CANDIDATE, "pid": "P2", "productNameEn": "Women's Casual Shoes", "categoryName": "Women's Shoes"}
        out = self.run_main(apply=True, candidates=[shoes])
        self.assertEqual(self.mutation_names(), [])
        self.assertEqual(self.cj_calls, [])
        self.assertIn("hors périmètre v4", out)


class StandaloneTest(unittest.TestCase):
    def test_v4_neither_imports_nor_touches_v3(self):
        src = Path(v4.__file__).read_text(encoding="utf-8")
        for forbidden in ("shopify_add_product_v3", "shopify_add_product-2", "cj-to-shopify-v3", "import shopify_add_product."):
            self.assertNotIn(forbidden, src)

    def test_no_code_path_can_publish(self):
        src = Path(v4.__file__).read_text(encoding="utf-8")
        self.assertEqual(v4.PRODUCT_STATUS, "DRAFT")
        self.assertNotIn('"ACTIVE"', src)
        self.assertNotIn("publishablePublish", src)


if __name__ == "__main__":
    unittest.main()
