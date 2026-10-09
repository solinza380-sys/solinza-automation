"""
shopify_scope_check.py  —  DIAGNOSTIC EN LECTURE SEULE

But : savoir exactement quels scopes Shopify donne au token utilisé par GitHub
(SHOPIFY_CLIENT_ID / SHOPIFY_CLIENT_SECRET), et à quelle application il appartient.

Ce script :
  - demande un token (client_credentials), comme shopify_add_product_v4.py ;
  - interroge UNIQUEMENT en lecture : currentAppInstallation (app + accessScopes) et locations ;
  - n'envoie AUCUNE mutation (garde-fou dans gql()) ;
  - n'affiche JAMAIS le token, le client id ni le client secret.

Variables d'environnement : SHOPIFY_STORE, SHOPIFY_CLIENT_ID, SHOPIFY_CLIENT_SECRET
"""
import os
import sys

import requests

API_VERSION = "2026-07"
READ_ANY = ("read_locations", "read_inventory")      # l'un OU l'autre suffit (règle v4)
WRITE_REQUIRED = "write_inventory"

APP_QUERY = "query { currentAppInstallation { app { id title handle } accessScopes { handle } } }"
LOCATIONS_QUERY = "query { locations(first: 5) { nodes { id name isActive } } }"


def env(name):
    return os.environ[name].strip()


def shop_subdomain():
    value = env("SHOPIFY_STORE").lower().replace("https://", "").replace("http://", "").split("/")[0]
    return value.replace(".myshopify.com", "")


def request_token(shop):
    """Retourne (token ou None, infos non sensibles sur la réponse)."""
    response = requests.post(
        f"https://{shop}.myshopify.com/admin/oauth/access_token",
        data={"grant_type": "client_credentials", "client_id": env("SHOPIFY_CLIENT_ID"),
              "client_secret": env("SHOPIFY_CLIENT_SECRET")},
        timeout=30,
    )
    try:
        body = response.json()
    except ValueError:
        body = {}
    info = {
        "http_status": response.status_code,
        "has_token": bool(body.get("access_token")),
        "expires_in": body.get("expires_in"),
        "scope": sorted(s.strip() for s in str(body.get("scope", "")).split(",") if s.strip()),
    }
    return body.get("access_token"), info


def gql(shop, token, query):
    """Requête GraphQL LECTURE SEULE. Refuse tout ce qui n'est pas une query."""
    if not query.lstrip().startswith("query"):
        raise RuntimeError("Garde-fou : ce script n'envoie jamais de mutation.")
    response = requests.post(
        f"https://{shop}.myshopify.com/admin/api/{API_VERSION}/graphql.json",
        headers={"Content-Type": "application/json", "X-Shopify-Access-Token": token},
        json={"query": query}, timeout=30,
    )
    try:
        return response.status_code, response.json()
    except ValueError:
        return response.status_code, {}


def mark(ok):
    return "PASS" if ok else "FAIL"


def diagnose():
    lines = ["## Diagnostic Shopify — scopes du token GitHub (LECTURE SEULE)", ""]
    shop = shop_subdomain()
    token, info = request_token(shop)
    lines.append(f"- Token demandé (client_credentials) : HTTP {info['http_status']} ; token reçu : "
                 f"{'oui' if info['has_token'] else 'non'} ; expires_in : {info['expires_in']}")
    lines.append("- Scopes renvoyés avec le token (champ `scope`) : "
                 + (", ".join(info["scope"]) if info["scope"] else "(champ absent ou vide)"))
    if not token:
        lines += ["", "### ⛔ Aucun token reçu : identifiants invalides, ou application non installée sur la boutique."]
        return lines, 1

    status, body = gql(shop, token, APP_QUERY)
    install = ((body.get("data") or {}).get("currentAppInstallation")) or {}
    app = install.get("app") or {}
    handles = sorted(s["handle"] for s in (install.get("accessScopes") or []))
    lines.append(f"- Application du token : « {app.get('title')} » (handle : {app.get('handle')}, id : {app.get('id')})")
    lines.append("- accessScopes (currentAppInstallation) : " + (", ".join(handles) if handles else "(vide ou illisible)"))
    if body.get("errors"):
        lines.append(f"- Erreur sur currentAppInstallation : {str(body['errors'])[:300]}")

    token_scopes = set(info["scope"])
    lines += ["", "| Scope | accessScopes | champ `scope` du token |", "|---|---|---|"]
    for scope in ("read_locations", "read_inventory", WRITE_REQUIRED):
        lines.append(f"| {scope} | {mark(scope in handles)} | {mark(scope in token_scopes) if token_scopes else 'INCONNU'} |")

    read_ok = any(s in handles for s in READ_ANY)
    write_ok = WRITE_REQUIRED in handles
    lines += ["", f"- Règle v4 « read_locations OU read_inventory » : **{mark(read_ok)}**",
              f"- Règle v4 « write_inventory » : **{mark(write_ok)}**"]

    status, body = gql(shop, token, LOCATIONS_QUERY)
    if body.get("errors"):
        msgs = " | ".join(str(e.get("message", e))[:240] for e in body["errors"])
        lines.append(f"- Test réel de lecture des locations : REFUSÉ — {msgs}")
    else:
        nodes = ((body.get("data") or {}).get("locations") or {}).get("nodes") or []
        lines.append(f"- Test réel de lecture des locations : OK ({len(nodes)} location(s) renvoyée(s))")

    lines += ["", "**Lecture du résultat**",
              "- Application différente de « Solinza-Automation » → les identifiants GitHub ne sont pas ceux de la bonne application (cas C).",
              "- Scopes absents de `scope` ET d'accessScopes → ils ne sont pas dans la version INSTALLÉE : version non publiée ou installation non mise à jour (A ou B ; à départager dans le Dev Dashboard).",
              "- `write_inventory` présent sans scope de lecture → Shopify ne liste peut-être pas la lecture implicite (cas D).",
              "- Aucune mutation n'a été envoyée par ce diagnostic."]
    return lines, 0


def main():
    lines, code = diagnose()
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if path:
        with open(path, "a", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
    print("\n".join(lines))
    sys.exit(code)


if __name__ == "__main__":
    main()
