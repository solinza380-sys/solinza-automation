"""Tests hors ligne de shopify_scope_check.py (aucun appel réseau)."""
import json
import os
import unittest
from unittest import mock

import shopify_scope_check as sc

SECRET_TOKEN = "shpat_TOP_SECRET_TOKEN_123"
CLIENT_ID = "CLIENT_ID_ABC"
CLIENT_SECRET = "CLIENT_SECRET_XYZ"


class FakeResponse:
    def __init__(self, status, body):
        self.status_code, self._body, self.text = status, body, json.dumps(body)

    def json(self):
        return self._body


def run_with(scopes, token_scope="", locations_error=None, token_ok=True, app_title="Solinza-Automation"):
    posts = []

    def post(url, **kw):
        posts.append((url, kw))
        if "oauth/access_token" in url:
            return FakeResponse(200 if token_ok else 400,
                                {"access_token": SECRET_TOKEN, "scope": token_scope, "expires_in": 86399}
                                if token_ok else {"error": "invalid_client"})
        query = kw["json"]["query"]
        if "currentAppInstallation" in query:
            return FakeResponse(200, {"data": {"currentAppInstallation": {
                "app": {"id": "gid://shopify/App/1", "title": app_title, "handle": "solinza-automation"},
                "accessScopes": [{"handle": s} for s in scopes]}}})
        if locations_error:
            return FakeResponse(200, {"errors": [{"message": locations_error}]})
        return FakeResponse(200, {"data": {"locations": {"nodes": [{"id": "gid://shopify/Location/1"}]}}})

    env = {"SHOPIFY_STORE": "x.myshopify.com", "SHOPIFY_CLIENT_ID": CLIENT_ID, "SHOPIFY_CLIENT_SECRET": CLIENT_SECRET}
    with mock.patch.dict(os.environ, env), mock.patch.object(sc.requests, "post", side_effect=post):
        lines, code = sc.diagnose()
    return "\n".join(lines), code, posts


class ScopeCheckTest(unittest.TestCase):
    def test_missing_read_scopes_reported_as_fail(self):
        out, code, _ = run_with({"read_products", "write_products"}, "read_products,write_products",
                                locations_error="Access denied for locations field. Required access: `read_locations`")
        self.assertEqual(code, 0)
        self.assertIn("| read_locations | FAIL | FAIL |", out)
        self.assertIn("| read_inventory | FAIL | FAIL |", out)
        self.assertIn("| write_inventory | FAIL | FAIL |", out)
        self.assertIn("« read_locations OU read_inventory » : **FAIL**", out)
        self.assertIn("REFUSÉ", out)
        self.assertIn("Required access: `read_locations`", out)

    def test_read_locations_present_passes(self):
        out, _, _ = run_with({"read_locations", "write_inventory"}, "read_locations,write_inventory")
        self.assertIn("| read_locations | PASS | PASS |", out)
        self.assertIn("« read_locations OU read_inventory » : **PASS**", out)
        self.assertIn("« write_inventory » : **PASS**", out)
        self.assertIn("Test réel de lecture des locations : OK", out)

    def test_read_inventory_present_passes(self):
        out, _, _ = run_with({"read_inventory", "write_inventory"})
        self.assertIn("« read_locations OU read_inventory » : **PASS**", out)
        self.assertIn("| read_locations | FAIL |", out)

    def test_app_identity_is_shown(self):
        out, _, _ = run_with(set(), app_title="Une autre application")
        self.assertIn("« Une autre application »", out)

    def test_token_failure_stops_cleanly(self):
        out, code, posts = run_with(set(), token_ok=False)
        self.assertEqual(code, 1)
        self.assertIn("Aucun token reçu", out)
        self.assertEqual(len(posts), 1)                       # aucune requête GraphQL sans token

    def test_never_prints_token_or_secrets(self):
        out, _, _ = run_with({"read_locations", "write_inventory"}, "read_locations,write_inventory")
        for secret in (SECRET_TOKEN, CLIENT_ID, CLIENT_SECRET):
            self.assertNotIn(secret, out)

    def test_no_mutation_is_ever_sent(self):
        _, _, posts = run_with({"read_locations", "write_inventory"})
        for _, kw in posts:
            if "json" in kw:
                self.assertNotIn("mutation", kw["json"]["query"])

    def test_guard_refuses_mutations(self):
        with self.assertRaises(RuntimeError):
            sc.gql("shop", "tok", "mutation { x }")


if __name__ == "__main__":
    unittest.main()
