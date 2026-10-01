"""
shopify_add_product.py (version 3)

1. Lit cj_search_results.json (produit par cj_product_search.py).
2. Pour chaque candidat (maximum MAX_CHECKS par exécution) :
   - interroge CJ : détails, variantes, stock, livraison vers la France ;
   - calcule la marge brute (avant publicité, paiement, retours) ;
   - refuse le produit si stock, marge ou délai ne respectent pas les seuils.
3. Crée le premier produit valide en BROUILLON (DRAFT) dans Shopify :
   photos, variantes en stock, prix, coût, SKU, et une fiche d'audit CJ
   (champ méta solinza_cj.audit). AUCUNE publication.

Variables d'environnement (GitHub Secrets) :
  - SHOPIFY_STORE
  - SHOPIFY_CLIENT_ID
  - SHOPIFY_CLIENT_SECRET
  - CJ_API_KEY
"""

import html
import json
import os
import re
import sys
import time
from datetime import datetime, timezone

import requests

# ----------------------------------------------------------------------
# RÉGLAGES (à ajuster si besoin)
# ----------------------------------------------------------------------
API_VERSION = "2026-07"
CJ_BASE_URL = "https://developers.cjdropshipping.com/api2.0/v1"

MARKUP = 3.14          # prix de vente = prix CJ x 3.14
MIN_PRICE = 24.99      # jamais en dessous de ce prix
USD_TO_EUR = 0.90      # taux APPROXIMATIF : à vérifier
VAT_RATE = 0.20        # TVA France supposée : à vérifier

MIN_STOCK = 100        # stock minimum par variante
MIN_MARGIN_EUR = 8.0   # marge brute minimum par article (en EUR, hors TVA)
MAX_DELIVERY_DAYS = 20 # délai maximum accepté (borne haute CJ)

MAX_CHECKS = 3         # candidats vérifiés par exécution (chaque appel CJ coûte des points)
MAX_VARIANTS = 8
MAX_IMAGES = 9
SHIP_FROM = "CN"
SHIP_TO = "FR"

VENDOR = "Solinza"
RESULTS_FILE = "cj_search_results.json"
OPTION_NAME = "Variante"
METAFIELD_NAMESPACE = "solinza_cj"  # minimum 3 caracteres


# ----------------------------------------------------------------------
# OUTILS GÉNÉRAUX
# ----------------------------------------------------------------------
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


def parse_price(value):
    """Extrait le premier nombre d'un prix CJ (ex: 11.85 ou '10.5 -- 20.6')."""
    match = re.search(r"\d+(?:\.\d+)?", str(value))
    return float(match.group()) if match else 0.0


# ----------------------------------------------------------------------
# SHOPIFY
# ----------------------------------------------------------------------
def get_shopify_token(shop):
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


def already_in_store(shop, token, pid):
    query = """
    query($q: String!) {
      products(first: 1, query: $q) { nodes { id } }
    }
    """
    data = graphql(shop, token, query, {"q": f'tag:"cj-pid-{pid}"'})
    return len(data["products"]["nodes"]) > 0


# ----------------------------------------------------------------------
# CJ DROPSHIPPING
# ----------------------------------------------------------------------
def cj_call(method, path, cj_token=None, **kwargs):
    headers = {"Content-Type": "application/json"}
    if cj_token:
        headers["CJ-Access-Token"] = cj_token
    time.sleep(1.1)  # éviter de dépasser les limites de l'API
    response = requests.request(
        method, f"{CJ_BASE_URL}/{path}", headers=headers, timeout=30, **kwargs
    )
    if response.status_code == 429:
        raise RuntimeError("CJ : points API insuffisants (429)")
    response.raise_for_status()
    body = response.json()
    if not body.get("result"):
        raise RuntimeError(f"CJ {path} : {body.get('message')}")
    return body.get("data")


def get_cj_token():
    data = cj_call(
        "POST", "authentication/getAccessToken", json={"apiKey": env("CJ_API_KEY")}
    )
    return data["accessToken"]


def get_variants(cj_token, pid):
    for path in ("product/variant/query", "product/variant/queryByPid"):
        try:
            data = cj_call("GET", path, cj_token, params={"pid": pid})
        except Exception:
            data = None
        if isinstance(data, list) and data:
            return data
    return []


def get_stock(cj_token, vid):
    """Stock total d'une variante (entrepôt de départ si disponible)."""
    try:
        data = cj_call("GET", "product/stock/queryByVid", cj_token, params={"vid": vid})
    except Exception:
        return 0
    entries = data if isinstance(data, list) else []
    local = [e for e in entries if e.get("countryCode") == SHIP_FROM]
    total = 0
    for e in local or entries:
        total += int(e.get("totalInventoryNum") or e.get("storageNum") or 0)
    return total


def get_shipping(cj_token, vid):
    """Option de livraison la moins chère vers la France."""
    try:
        data = cj_call(
            "POST",
            "logistic/freightCalculate",
            cj_token,
            json={
                "startCountryCode": SHIP_FROM,
                "endCountryCode": SHIP_TO,
                "products": [{"quantity": 1, "vid": vid}],
            },
        )
    except Exception:
        return None
    options = [
        o for o in (data or []) if isinstance(o, dict) and o.get("logisticPrice") is not None
    ]
    if not options:
        return None
    best = min(options, key=lambda o: float(o["logisticPrice"]))
    return {
        "price_usd": float(best["logisticPrice"]),
        "aging": str(best.get("logisticAging") or ""),
        "name": best.get("logisticName") or "",
    }


def max_days(aging):
    numbers = [int(n) for n in re.findall(r"\d+", aging or "")]
    return max(numbers) if numbers else None


def variant_label(v, title, used):
    name = (v.get("variantNameEn") or "").strip()
    if name.lower().startswith(title.lower()):
        name = name[len(title):].strip()
    name = str(name or v.get("variantKey") or v.get("variantSku") or "Standard")[:200]
    base, i = name, 2
    while name in used:
        name = f"{base} ({i})"
        i += 1
    used.add(name)
    return name


def sell_price(cost_usd):
    return max(round(cost_usd * MARKUP, 2), MIN_PRICE)


def audit_candidate(cj_token, p):
    """Interroge CJ et décide si le produit est exploitable."""
    pid = p["pid"]
    reasons = []

    detail = cj_call("GET", "product/query", cj_token, params={"pid": pid}) or {}
    title = (detail.get("productNameEn") or p["productNameEn"]).strip()

    images = [
        x for x in (detail.get("productImageSet") or [])
        if isinstance(x, str) and x.startswith("http")
    ][:MAX_IMAGES]
    if not images and str(p.get("productImage", "")).startswith("http"):
        images = [p["productImage"]]
    if not images:
        reasons.append("aucune image")

    used = set()
    variants = []
    for v in get_variants(cj_token, pid)[:MAX_VARIANTS]:
        vid = v.get("vid")
        cost = parse_price(v.get("variantSellPrice"))
        if not vid or cost <= 0:
            continue
        stock = get_stock(cj_token, vid)
        if stock < MIN_STOCK:
            continue
        variants.append({
            "vid": vid,
            "name": variant_label(v, title, used),
            "sku": v.get("variantSku") or "",
            "cost_usd": cost,
            "stock": stock,
        })
    if not variants:
        reasons.append(f"aucune variante avec stock >= {MIN_STOCK}")

    ship = None
    if variants:
        ship = get_shipping(cj_token, variants[0]["vid"])
        if not ship:
            reasons.append("livraison vers la France indisponible")
        else:
            days = max_days(ship["aging"])
            if days is None or days > MAX_DELIVERY_DAYS:
                reasons.append(f"délai de livraison trop long ou inconnu ({ship['aging']})")

    min_margin = None
    if variants and ship:
        for v in variants:
            v["price"] = sell_price(v["cost_usd"])
            v["cost_eur"] = round(v["cost_usd"] * USD_TO_EUR, 2)
            total_cost = (v["cost_usd"] + ship["price_usd"]) * USD_TO_EUR
            v["margin_eur"] = round(v["price"] / (1 + VAT_RATE) - total_cost, 2)
        min_margin = min(v["margin_eur"] for v in variants)
        if min_margin < MIN_MARGIN_EUR:
            reasons.append(f"marge brute trop faible ({min_margin} EUR < {MIN_MARGIN_EUR})")

    return {
        "ok": not reasons,
        "reasons": reasons,
        "title": title,
        "images": images,
        "variants": variants,
        "ship": ship,
        "min_margin": min_margin,
        "category": detail.get("categoryName") or p.get("categoryName") or "",
        "cj_sku": detail.get("productSku") or p.get("productSku") or "",
        "material": detail.get("materialNameEn"),
        "weight_g": detail.get("productWeight"),
    }


# ----------------------------------------------------------------------
# CRÉATION DU PRODUIT (BROUILLON)
# ----------------------------------------------------------------------
def check_errors(errors, label):
    if errors:
        raise RuntimeError(f"{label} : {errors}")


def create_product(shop, token, p, a):
    title = a["title"]
    variants = a["variants"]

    audit = {
        "source": "CJ",
        "cj_pid": p["pid"],
        "cj_sku": a["cj_sku"],
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "usd_to_eur_approx": USD_TO_EUR,
        "vat_rate_assumed": VAT_RATE,
        "shipping_to": SHIP_TO,
        "shipping_usd": a["ship"]["price_usd"],
        "shipping_days": a["ship"]["aging"],
        "shipping_method": a["ship"]["name"],
        "material": a["material"],
        "weight_g": a["weight_g"],
        "variants": [
            {k: v[k] for k in ("sku", "cost_usd", "stock", "price", "margin_eur")}
            for v in variants
        ],
        "note": "Marge brute avant publicité, frais de paiement et retours. A verifier.",
    }

    product_input = {
        "title": title,
        "descriptionHtml": f"<p>{html.escape(title)}</p>",
        "vendor": VENDOR,
        "productType": a["category"],
        "tags": ["cj-auto", f"cj-pid-{p['pid']}", f"cj-sku-{a['cj_sku']}", "a-corriger"],
        "status": "DRAFT",
        "metafields": [
            {
                "namespace": METAFIELD_NAMESPACE,
                "key": "audit",
                "type": "json",
                "value": json.dumps(audit, ensure_ascii=False),
            }
        ],
    }
    if len(variants) > 1:
        product_input["productOptions"] = [
            {"name": OPTION_NAME, "values": [{"name": v["name"]} for v in variants]}
        ]

    media = [
        {"originalSource": url, "mediaContentType": "IMAGE", "alt": title}
        for url in a["images"]
    ]

    create_mutation = """
    mutation($product: ProductCreateInput!, $media: [CreateMediaInput!]) {
      productCreate(product: $product, media: $media) {
        product {
          id
          title
          variants(first: 50) { nodes { id selectedOptions { name value } } }
        }
        userErrors { field message }
      }
    }
    """
    data = graphql(shop, token, create_mutation, {"product": product_input, "media": media})
    data = data["productCreate"]
    check_errors(data["userErrors"], "Création refusée")
    product = data["product"]
    nodes = product["variants"]["nodes"]

    # Associer chaque variante CJ à une variante Shopify déjà créée (si elle existe)
    by_name = {}
    for n in nodes:
        opts = n.get("selectedOptions") or []
        if opts:
            by_name[opts[0]["value"]] = n["id"]

    to_update = []
    to_create = []
    if len(variants) == 1:
        to_update = [(nodes[0]["id"], variants[0])]
    else:
        for v in variants:
            shopify_id = by_name.get(v["name"])
            if shopify_id:
                to_update.append((shopify_id, v))
            else:
                to_create.append(v)
        if not to_update:
            to_update = [(nodes[0]["id"], variants[0])]
            to_create = variants[1:]

    update_mutation = """
    mutation($productId: ID!, $variants: [ProductVariantsBulkInput!]!) {
      productVariantsBulkUpdate(productId: $productId, variants: $variants) {
        productVariants { id price }
        userErrors { field message }
      }
    }
    """
    upd = graphql(
        shop,
        token,
        update_mutation,
        {
            "productId": product["id"],
            "variants": [
                {
                    "id": shopify_id,
                    "price": f"{v['price']:.2f}",
                    "inventoryItem": {
                        "sku": v["sku"],
                        "cost": f"{v['cost_eur']:.2f}",
                        "tracked": False,
                    },
                }
                for shopify_id, v in to_update
            ],
        },
    )["productVariantsBulkUpdate"]
    check_errors(upd["userErrors"], f"Prix refusé (produit {product['id']} déjà créé en brouillon)")

    if to_create:
        create_variants_mutation = """
        mutation($productId: ID!, $variants: [ProductVariantsBulkInput!]!) {
          productVariantsBulkCreate(productId: $productId, variants: $variants) {
            productVariants { id }
            userErrors { field message }
          }
        }
        """
        rest = [
            {
                "price": f"{v['price']:.2f}",
                "optionValues": [{"optionName": OPTION_NAME, "name": v["name"]}],
                "inventoryItem": {
                    "sku": v["sku"],
                    "cost": f"{v['cost_eur']:.2f}",
                    "tracked": False,
                },
            }
            for v in to_create
        ]
        res = graphql(
            shop,
            token,
            create_variants_mutation,
            {"productId": product["id"], "variants": rest},
        )["productVariantsBulkCreate"]
        check_errors(
            res["userErrors"],
            f"Variantes refusées (produit {product['id']} déjà créé en brouillon)",
        )

    return product


# ----------------------------------------------------------------------
# PROGRAMME PRINCIPAL
# ----------------------------------------------------------------------
def load_candidates():
    with open(RESULTS_FILE, encoding="utf-8") as f:
        results = json.load(f)
    candidates = []
    for keyword, products in results.items():
        for p in products:
            title = (p.get("productNameEn") or "").strip()
            price = parse_price(p.get("sellPrice"))
            if p.get("pid") and title and p.get("productImage") and price > 0:
                candidates.append({**p, "keyword": keyword})
    return candidates


def main():
    summary = ["## Ajout d'un produit CJ dans Shopify (brouillon)", ""]
    try:
        shop = shop_subdomain()
        token = get_shopify_token(shop)
        summary.append("- Connexion Shopify : réussie ✅")
        cj_token = get_cj_token()
        summary.append("- Connexion CJ : réussie ✅")

        candidates = load_candidates()
        summary.append(f"- Produits candidats : {len(candidates)}")

        checked = 0
        created = False
        for p in candidates:
            if already_in_store(shop, token, p["pid"]):
                continue
            if checked >= MAX_CHECKS:
                break
            checked += 1

            a = audit_candidate(cj_token, p)
            name = a["title"]
            if not a["ok"]:
                summary.append(f"- ❌ Refusé : **{name}** → " + " ; ".join(a["reasons"]))
                continue

            product = create_product(shop, token, p, a)
            created = True
            summary.append(f"- Produit créé en brouillon : **{product['title']}** ✅")
            summary.append(f"  - Variantes en stock : {len(a['variants'])}")
            summary.append(f"  - Photos : {len(a['images'])}")
            summary.append(
                f"  - Livraison France : {a['ship']['price_usd']} USD, "
                f"{a['ship']['aging']} jours ({a['ship']['name']})"
            )
            summary.append(f"  - Marge brute minimale : {a['min_margin']} EUR (avant pub, paiement, retours)")
            summary.append(f"  - Mot-clé d'origine : {p['keyword']}")
            break

        if not created:
            summary.append(
                f"- Aucun produit créé (candidats vérifiés : {checked}). "
                "Voir les raisons ci-dessus."
            )
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
