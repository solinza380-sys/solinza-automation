"""
shopify_add_product.py

1. Lit cj_search_results.json (produit par cj_product_search.py).
2. Choisit le premier produit valide qui n'est pas déjà dans la boutique.
3. Se connecte à Shopify (client credentials grant) et crée le produit
   en BROUILLON (DRAFT), avec photo, prix de vente et tags.

Variables d'environnement (GitHub Secrets) :
  - SHOPIFY_STORE
  - SHOPIFY_CLIENT_ID
  - SHOPIFY_CLIENT_SECRET
"""

import json
import os
import re
import sys

import requests

API_VERSION = "2026-07"
MARKUP = 2.5  # prix de vente = prix CJ x 2.5
VENDOR = "Solinza"
RESULTS_FILE = "cj_search_results.json"


def env(name):
    return os.environ[name].strip()


def shop_subdomain():
    value = env("SHOPIFY_STORE").lower()
    value = value.replace("https://", "").replace("http://", "")
    value = value.split("/")[0]
    return value.replace(".myshopify.com", "")


def write_summary(lines):
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if not path:
        return
    with open(path, "a", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")


def get_token(shop):
    url = f"https://{shop}.myshopify.com/admin/oauth/access_token"
    data = {
        "grant_type": "client_credentials",
        "client_id": env("SHOPIFY_CLIENT_ID"),
        "client_secret": env("SHOPIFY_CLIENT_SECRET"),
    }
    response = requests.post(url, data=data, timeout=30)
    if not response.ok:
        raise RuntimeError(
            f"Token Shopify refusé ({response.status_code}) : {response.text[:300]}"
        )
    return response.json()["access_token"]


def graphql(shop, token, query, variables=None):
    url = f"https://{shop}.myshopify.com/admin/api/{API_VERSION}/graphql.json"
    headers = {
        "Content-Type": "application/json",
        "X-Shopify-Access-Token": token,
    }
    payload = {"query": query, "variables": variables or {}}
    response = requests.post(url, headers=headers, json=payload, timeout=60)
    if not response.ok:
        raise RuntimeError(
            f"Requête GraphQL refusée ({response.status_code}) : {response.text[:300]}"
        )
    body = response.json()
    if body.get("errors"):
        raise RuntimeError(f"Erreurs GraphQL : {json.dumps(body['errors'])[:500]}")
    return body["data"]


def parse_price(value):
    """Extrait le premier nombre d'un prix CJ (ex: 11.85 ou '10.5 -- 20.6')."""
    match = re.search(r"\d+(?:\.\d+)?", str(value))
    return float(match.group()) if match else 0.0


def load_candidates():
    with open(RESULTS_FILE, encoding="utf-8") as f:
        results = json.load(f)
    candidates = []
    for keyword, products in results.items():
        for p in products:
            title = (p.get("productNameEn") or "").strip()
            price = parse_price(p.get("sellPrice"))
            if p.get("pid") and title and p.get("productImage") and price > 0:
                candidates.append({**p, "keyword": keyword, "cj_price": price})
    return candidates


def already_in_store(shop, token, pid):
    query = """
    query($q: String!) {
      products(first: 1, query: $q) { nodes { id } }
    }
    """
    data = graphql(shop, token, query, {"q": f'tag:"cj-pid-{pid}"'})
    return len(data["products"]["nodes"]) > 0


def create_product(shop, token, p):
    title = p["productNameEn"].strip()
    mutation = """
    mutation($product: ProductCreateInput!, $media: [CreateMediaInput!]) {
      productCreate(product: $product, media: $media) {
        product { id title variants(first: 1) { nodes { id } } }
        userErrors { field message }
      }
    }
    """
    variables = {
        "product": {
            "title": title,
            "descriptionHtml": f"<p>{title}</p>",
            "vendor": VENDOR,
            "productType": p.get("categoryName") or "",
            "tags": ["cj-auto", f"cj-pid-{p['pid']}", f"cj-sku-{p.get('productSku', '')}"],
            "status": "DRAFT",
        },
        "media": [
            {
                "originalSource": p["productImage"],
                "mediaContentType": "IMAGE",
                "alt": title,
            }
        ],
    }
    data = graphql(shop, token, mutation, variables)["productCreate"]
    if data["userErrors"]:
        raise RuntimeError(f"Création refusée : {data['userErrors']}")
    product = data["product"]

    sell_price = round(p["cj_price"] * MARKUP, 2)
    variant_id = product["variants"]["nodes"][0]["id"]
    price_mutation = """
    mutation($productId: ID!, $variants: [ProductVariantsBulkInput!]!) {
      productVariantsBulkUpdate(productId: $productId, variants: $variants) {
        productVariants { id price }
        userErrors { field message }
      }
    }
    """
    price_data = graphql(
        shop,
        token,
        price_mutation,
        {
            "productId": product["id"],
            "variants": [{"id": variant_id, "price": f"{sell_price:.2f}"}],
        },
    )["productVariantsBulkUpdate"]
    if price_data["userErrors"]:
        raise RuntimeError(f"Prix refusé : {price_data['userErrors']}")
    return product, sell_price


def main():
    summary = ["## Ajout d'un produit CJ dans Shopify", ""]
    try:
        shop = shop_subdomain()
        token = get_token(shop)
        summary.append("- Connexion Shopify : réussie ✅")

        candidates = load_candidates()
        summary.append(f"- Produits candidats : {len(candidates)}")

        chosen = None
        for p in candidates:
            if not already_in_store(shop, token, p["pid"]):
                chosen = p
                break

        if not chosen:
            summary.append("- Aucun nouveau produit à ajouter (tous déjà dans la boutique).")
        else:
            product, sell_price = create_product(shop, token, chosen)
            summary.append(f"- Produit créé en brouillon : **{product['title']}** ✅")
            summary.append(f"- Prix CJ : {chosen['cj_price']} → prix de vente : {sell_price}")
            summary.append(f"- Mot-clé d'origine : {chosen['keyword']}")
    except Exception as e:
        summary.append("")
        summary.append(f"### ❌ Erreur : {e}")
        write_summary(summary)
        print(f"Erreur : {e}")
        sys.exit(1)

    write_summary(summary)
    print("\n".join(summary))


if __name__ == "__main__":
    main()
