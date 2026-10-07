"""
shopify_add_product_v4.py  (version 4)

La V3 n'est PAS modifiée : ce fichier est autonome.

Chaîne de décision (dans cet ordre)
------------------------------------
  Preflight (LECTURE SEULE, aussi en simulation)  ->  Audit CJ  ->  Classement
  ->  1 candidat maximum  ->  Traduction  ->  Création en DRAFT (si APPLY=1)

Règles principales
------------------
1. PREFLIGHT lecture seule, avant tout le reste, y compris avec APPLY=0 :
     a) scopes disponibles (currentAppInstallation.accessScopes) ;
     b) lecture des locations : read_locations OU read_inventory (un seul suffit) ;
     c) write_inventory ;
     d) récupération du locationId ; e) existence d'un locationId actif utilisable.
   Échec => STOP immédiat, aucune mutation, message qui nomme le(s) scope(s) exact(s).
2. SIMULATION PAR DÉFAUT : sans APPLY=1, rien n'est écrit.
3. TOUJOURS DRAFT : tout produit créé est en DRAFT, traduction complète ou non, avec ou sans
   tag a-corriger. Aucune publication / activation automatique.
4. TRADUCTION non bloquante : s'il reste des mots anglais (titre ou couleurs inconnues),
   le produit est quand même créé (DRAFT) avec le tag a-corriger. Traduction complète : pas de tag.
5. LIMITES configurables (variables d'environnement) : MAX_CANDIDATES (défaut 3) et
   MAX_VARIANTS_PER_CANDIDATE (défaut 12). Elles sont affichées dans le résumé.
6. Rentabilité PAR VARIANTE : coût total = prix produit CJ + livraison + douane ; une variante
   non rentable est exclue (jamais créée) ; aucune variante rentable => produit rejeté.
7. Filtre d'identité : mode féminine (robes, hauts, pulls, chemisiers, manteaux, vestes,
   jeans/pantalons, jupes). Chaussures, sacs, bijoux, accessoires, homme, enfant... exclus.
8. Stock : après création, inventoryActivate + inventorySetQuantities au locationId validé
   (quantité = stock CJ relevé pendant l'audit, NON synchronisé ensuite).

Variables d'environnement (GitHub Secrets) :
  SHOPIFY_STORE, SHOPIFY_CLIENT_ID, SHOPIFY_CLIENT_SECRET, CJ_API_KEY
  APPLY=1                         crée réellement le brouillon (sinon : simulation)
  MAX_CANDIDATES                  optionnel (défaut 3)
  MAX_VARIANTS_PER_CANDIDATE      optionnel (défaut 12)
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
# RÉGLAGES (toutes les hypothèses chiffrées sont centralisées ici)
# ----------------------------------------------------------------------
SCRIPT_VERSION = "v4"
API_VERSION = "2026-07"
CJ_BASE_URL = "https://developers.cjdropshipping.com/api2.0/v1"

MARKUP = 3.14           # prix de vente TTC = prix produit CJ (USD) x 3.14
MIN_PRICE = 24.99       # jamais en dessous de ce prix (EUR)

# Taux de CONFIGURATION utilisé par le calcul v4 pour convertir USD -> EUR.
# Ce n'est PAS un taux de marché en temps réel : à ajuster manuellement.
USD_TO_EUR = 0.90

# Douane par article (USD) : provision de CONFIGURATION fondée sur l'exemple relevé sur la page
# CJ (2XL : livraison 8,44 USD + douane 3,50 USD). Non garantie par l'API : À VÉRIFIER.
CUSTOMS_USD = 3.50

VAT_RATE = 0.20         # TVA France supposée (prix de vente TTC) : à vérifier
MIN_STOCK = 100         # stock minimum par variante
MIN_MARGIN_EUR = 8.0    # marge minimale par variante (EUR, hors TVA) ; en dessous = exclue
MAX_DELIVERY_DAYS = 20  # délai maximum accepté (borne haute CJ)

# Limites par défaut (surchargeables par variables d'environnement du même nom).
MAX_CANDIDATES = 3                  # candidats AUDITÉS par exécution (chaque audit coûte des points CJ)
MAX_VARIANTS_PER_CANDIDATE = 12     # variantes AUDITÉES par candidat
MAX_SCAN = 30                       # candidats examinés (filtre d'identité inclus)
MAX_IMAGES = 9
SHIP_FROM = "CN"
SHIP_TO = "FR"

VENDOR = "Solinza"
RESULTS_FILE = "cj_search_results.json"
OPTION_NAME = "Variante"
OPT_COLOR = "Couleur"
OPT_SIZE = "Taille"
METAFIELD_NAMESPACE = "solinza_cj"

# Scopes Shopify (preflight)
LOCATION_READ_SCOPES = ("read_locations", "read_inventory")   # l'un OU l'autre suffit
WRITE_INVENTORY_SCOPE = "write_inventory"
INVENTORY_REASON = "correction"

# Statut imposé à tout produit créé. Ne jamais changer.
PRODUCT_STATUS = "DRAFT"

# Filtre d'identité v4 : mode féminine à variantes couleur/taille standard. Pas de chaussures.
ALLOW = [
    r"\bdress(es)?\b",                       # robes
    r"\btops?\b", r"\btees?\b", r"\bt-shirts?\b", r"\btanks?\b",   # hauts
    r"\bsweaters?\b", r"\bpullovers?\b", r"\bcardigans?\b", r"\bhoodies?\b", r"\bsweatshirts?\b",  # pulls
    r"\bblouses?\b", r"\bshirts?\b",         # chemisiers
    r"\bcoats?\b", r"\btrench\b",            # manteaux
    r"\bjackets?\b", r"\bblazers?\b",        # vestes
    r"\bjeans?\b", r"\bpants\b", r"\btrousers\b", r"\bleggings\b",  # jeans / pantalons
    r"\bskirts?\b",                          # jupes
]
DENY = [
    # chaussures
    r"\bshoes?\b", r"\bsandals?\b", r"\bboots?\b", r"\bsneakers?\b", r"\bheels?\b",
    r"\bslippers?\b", r"\bfootwear\b", r"\bloafers?\b",
    # sacs, portefeuilles, bijoux, accessoires
    r"\bbags?\b", r"\bhandbags?\b", r"\bwallets?\b", r"\bbackpacks?\b", r"\bpurses?\b",
    r"\bjewel(?:ry|lery)\b", r"\bnecklaces?\b", r"\brings?\b", r"\bearrings?\b", r"\bbracelets?\b",
    r"\bwatch(?:es)?\b", r"\baccessor(?:y|ies)\b", r"\bscarf\b", r"\bbelts?\b", r"\bhats?\b",
    # homme / enfant / bébé / animaux
    r"\bmen('s)?\b", r"\bmale\b", r"\bboys?\b", r"\bgirls?\b", r"\bkids?\b", r"\bchildren\b",
    r"\bbaby\b", r"\bpets?\b", r"\bdogs?\b", r"\bcats?\b",
    # électronique / maison / gadgets / autres
    r"\bphones?\b", r"\busb\b", r"\bcharger\b", r"\bled\b", r"\bspeaker\b", r"\bheadphones?\b",
    r"\bhome\b", r"\bkitchen\b", r"\bdecor\b", r"\bbedding\b", r"\bcurtains?\b",
    r"\bgadgets?\b", r"\btoys?\b",
    r"\blingerie\b", r"\bunderwear\b", r"\bbikini\b", r"\bswimsuits?\b", r"\bcostumes?\b",
    r"\bhalloween\b", r"\bcosplay\b",
]

# Dictionnaire centralisé de traduction des couleurs (clé = minuscules, espaces simples).
# Couleur absente : valeur d'origine conservée (aucune traduction inventée), produit NON rejeté.
COLOR_FR = {
    "dark blue": "Bleu foncé", "light blue": "Bleu clair", "navy blue": "Bleu marine",
    "navy": "Bleu marine", "blue": "Bleu", "black": "Noir", "white": "Blanc", "red": "Rouge",
    "wine red": "Rouge bordeaux", "burgundy": "Bordeaux", "green": "Vert", "yellow": "Jaune",
    "pink": "Rose", "purple": "Violet", "gray": "Gris", "grey": "Gris", "brown": "Marron",
    "beige": "Beige", "apricot": "Abricot", "khaki": "Kaki", "orange": "Orange",
    "camel": "Camel", "cream": "Crème",
}

# Glossaire de traduction du TITRE (déterministe, volontairement limité). Ce n'est pas une
# rédaction : tout mot non reconnu reste tel quel et déclenche le tag a-corriger.
GENDER_EN = {"women", "woman", "womens", "ladies", "lady", "female"}
TYPE_FR = {
    "dress": "Robe", "dresses": "Robe", "coat": "Manteau", "trench": "Trench", "jacket": "Veste",
    "blazer": "Blazer", "sweater": "Pull", "pullover": "Pull", "cardigan": "Cardigan",
    "hoodie": "Sweat à capuche", "sweatshirt": "Sweat", "jeans": "Jean", "jean": "Jean",
    "pants": "Pantalon", "trousers": "Pantalon", "leggings": "Legging", "shirt": "Chemise",
    "blouse": "Chemisier", "top": "Haut", "tops": "Haut", "tee": "T-shirt", "tshirt": "T-shirt",
    "tank": "Débardeur", "skirt": "Jupe",
}
PHRASES_FR = {
    "long sleeve": "à manches longues", "long sleeves": "à manches longues",
    "short sleeve": "à manches courtes", "short sleeves": "à manches courtes",
    "v neck": "col en V", "round neck": "col rond", "o neck": "col rond",
    "high waist": "taille haute", "wide leg": "jambe large", "plus size": "grande taille",
}
WORDS_FR = {
    "denim": "en denim", "knitted": "en maille", "knit": "en maille", "velvet": "en velours",
    "linen": "en lin", "floral": "à fleurs",
}
FRENCH_INVARIANT = {"à", "de", "du", "en", "et", "la", "le", "les", "un", "une", "avec", "pour", "sans", "femme"}

SIZE_RE = re.compile(
    r"^(?P<color>.*?)[\s\-_]+(?P<size>XXS|XS|S|M|L|XL|XXL|XXXL|\d+XL|ONE SIZE|FREE SIZE)$",
    re.IGNORECASE,
)


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


def to_grams(value):
    n = parse_price(value)
    return round(n, 1) if n > 0 else None


def get_limit(name, default):
    """Limite configurable par variable d'environnement (entier >= 1), sinon valeur par défaut."""
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        value = int(raw.strip())
    except ValueError:
        raise RuntimeError(f"{name} doit être un entier >= 1 (reçu : {raw!r})")
    if value < 1:
        raise RuntimeError(f"{name} doit être un entier >= 1 (reçu : {raw!r})")
    return value


def max_candidates():
    return get_limit("MAX_CANDIDATES", MAX_CANDIDATES)


def max_variants_per_candidate():
    return get_limit("MAX_VARIANTS_PER_CANDIDATE", MAX_VARIANTS_PER_CANDIDATE)


# ----------------------------------------------------------------------
# RÈGLES MÉTIER (fonctions pures, testées hors ligne)
# ----------------------------------------------------------------------
def fit_check(title, category=""):
    """Filtre d'identité Solinza. Retourne une liste de raisons de refus (vide = OK)."""
    text = f"{title} {category}".lower()
    for pattern in DENY:
        m = re.search(pattern, text)
        if m:
            return [f"hors périmètre v4 (mot « {m.group().strip()} »)"]
    if not any(re.search(pattern, text) for pattern in ALLOW):
        return ["catégorie non reconnue (aucun mot de la liste ALLOW)"]
    return []


def color_fr(color):
    """Traduit une couleur connue ; sinon conserve exactement la valeur d'origine."""
    key = re.sub(r"\s+", " ", color.strip().lower())
    return COLOR_FR.get(key, color.strip())


def split_color_size(label):
    """'Dark Blue S' / 'Dark Blue-2XL' -> ('Dark Blue', '2XL'). None si non reconnu.
    La partie couleur est renvoyée telle qu'écrite (sans transformation)."""
    m = SIZE_RE.match(str(label).strip())
    if not m or not m.group("color").strip():
        return None
    return m.group("color").strip(), m.group("size").upper()


def build_options(variants):
    """Retourne (mode, options, combos).
    options : [(nom_option, [valeurs...])] ; combos : valeurs d'options de chaque variante.
    Les valeurs sont listées dans l'ordre d'apparition : la 1re combinaison est donc celle
    de la 1re variante (celle que Shopify crée automatiquement)."""
    if len(variants) > 1:
        parsed = [split_color_size(v["name"]) for v in variants]
        if all(parsed):
            combos = [(color_fr(c), s) for c, s in parsed]
            if len(set(combos)) == len(combos):
                colors = list(dict.fromkeys(c for c, _ in combos))
                sizes = list(dict.fromkeys(s for _, s in combos))
                if len(colors) >= 2:
                    return "color_size", [(OPT_COLOR, colors), (OPT_SIZE, sizes)], combos
                return "size", [(OPT_SIZE, sizes)], [(s,) for _, s in combos]
        return "single", [(OPTION_NAME, [v["name"] for v in variants])], [(v["name"],) for v in variants]
    return "none", [], [()]


def translate_title(title):
    """Traduction FR du titre par glossaire. Retourne title_fr, english_left, complete.
    Un mot non reconnu reste tel quel (aucune traduction inventée) et est listé dans english_left."""
    cleaned = re.sub(r"(?i)t-shirt", "tshirt", title.replace("’", "'"))
    cleaned = re.sub(r"'s\b", "", cleaned)
    raw = re.findall(r"[A-Za-zÀ-ÿ0-9]+", cleaned)
    low = [t.lower() for t in raw]
    ptype, gender, mods, left = None, False, [], []
    i = 0
    while i < len(low):
        if i + 1 < len(low) and f"{low[i]} {low[i + 1]}" in PHRASES_FR:
            mods.append(PHRASES_FR[f"{low[i]} {low[i + 1]}"])
            i += 2
            continue
        tok = low[i]
        if tok in GENDER_EN:
            gender = True
        elif tok in TYPE_FR and ptype is None:
            ptype = TYPE_FR[tok]
        elif tok in WORDS_FR:
            mods.append(WORDS_FR[tok])
        elif tok in FRENCH_INVARIANT or tok.isdigit():
            pass
        else:
            left.append(raw[i])
        i += 1
    if ptype is None:  # aucun type de vêtement reconnu : impossible de composer un titre français
        every = [t for t in raw if t.lower() not in FRENCH_INVARIANT and not t.isdigit()]
        return {"title_fr": title, "english_left": every, "complete": False}
    title_fr = " ".join([ptype] + (["Femme"] if gender else []) + mods + left)
    return {"title_fr": title_fr, "english_left": left, "complete": not left}


def colors_in_variants(variants):
    colors = []
    for v in variants:
        parsed = split_color_size(v["name"])
        if parsed and parsed[0] not in colors:
            colors.append(parsed[0])
    return colors


def unknown_colors(colors):
    """Couleurs absentes du dictionnaire (conservées telles quelles) : mots anglais potentiels."""
    return [c for c in colors if re.sub(r"\s+", " ", c.strip().lower()) not in COLOR_FR]


def sell_price(product_usd):
    return max(round(product_usd * MARKUP, 2), MIN_PRICE)


def usd_to_eur(amount_usd):
    """Conversion avec le taux de CONFIGURATION USD_TO_EUR (pas un taux de marché)."""
    return round(amount_usd * USD_TO_EUR, 2)


def total_cost_usd(product_usd, shipping_usd, customs_usd):
    """Coût total d'une variante = prix produit CJ + livraison + douane (USD)."""
    return round(product_usd + shipping_usd + customs_usd, 2)


def variant_economics(product_usd, shipping_usd, customs_usd=None):
    """Chiffres de rentabilité d'UNE variante."""
    customs = CUSTOMS_USD if customs_usd is None else customs_usd
    total_usd = total_cost_usd(product_usd, shipping_usd, customs)
    total_eur = usd_to_eur(total_usd)
    price = sell_price(product_usd)
    revenue_ht = round(price / (1 + VAT_RATE), 2)
    return {
        "price": price,
        "cost_eur": usd_to_eur(product_usd),     # coût produit seul (champ « coût » Shopify)
        "shipping_usd": shipping_usd,
        "customs_usd": customs,
        "total_usd": total_usd,
        "total_eur": total_eur,
        "revenue_ht": revenue_ht,
        "margin_eur": round(revenue_ht - total_eur, 2),
    }


def max_days(aging):
    numbers = [int(n) for n in re.findall(r"\d+", aging or "")]
    return max(numbers) if numbers else None


def rank_score(a):
    """Classement : marge moyenne des variantes conservées, puis nombre de variantes."""
    margins = [v["margin_eur"] for v in a["variants"]]
    return (sum(margins) / len(margins), len(margins))


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
        raise RuntimeError(f"Token Shopify refusé ({response.status_code}) : {response.text[:300]}")
    return response.json()["access_token"]


def graphql(shop, token, query, variables=None):
    url = f"https://{shop}.myshopify.com/admin/api/{API_VERSION}/graphql.json"
    headers = {"Content-Type": "application/json", "X-Shopify-Access-Token": token}
    response = requests.post(url, headers=headers, json={"query": query, "variables": variables or {}}, timeout=60)
    if not response.ok:
        raise RuntimeError(f"Requête GraphQL refusée ({response.status_code}) : {response.text[:300]}")
    body = response.json()
    if body.get("errors"):
        raise RuntimeError(f"Erreurs GraphQL : {json.dumps(body['errors'])[:500]}")
    return body["data"]


SEARCH_QUERY = """
query($q: String!) {
  products(first: 1, query: $q) { nodes { id } }
}
"""
SCOPES_QUERY = "query { currentAppInstallation { accessScopes { handle } } }"
LOCATIONS_QUERY = "query { locations(first: 20) { nodes { id name isActive } } }"


def already_in_store(shop, token, pid):
    data = graphql(shop, token, SEARCH_QUERY, {"q": f'tag:"cj-pid-{pid}"'})
    return len(data["products"]["nodes"]) > 0


def sku_in_store(shop, token, sku):
    """Détecte un doublon créé sans tags (copie manuelle, par exemple)."""
    if not sku:
        return False
    data = graphql(shop, token, SEARCH_QUERY, {"q": f'sku:"{sku}"'})
    return len(data["products"]["nodes"]) > 0


class PreflightError(RuntimeError):
    """Preflight en échec : arrêt immédiat, aucune mutation."""


def preflight(shop, token):
    """Preflight LECTURE SEULE (aucune mutation), dans l'ordre imposé :
       1) scopes disponibles ; 2) lecture des locations (read_locations OU read_inventory) ;
       3) write_inventory ; 4) récupération du locationId ; 5) locationId actif utilisable."""
    data = graphql(shop, token, SCOPES_QUERY)
    scopes = {s["handle"] for s in data["currentAppInstallation"]["accessScopes"]}

    if not any(s in scopes for s in LOCATION_READ_SCOPES):
        raise PreflightError(
            "STOP: aucun scope Shopify requis pour lire les locations n'est disponible.\n"
            "Scopes requis : read_locations ou read_inventory.\n"
            "Aucun des deux scopes n'est présent. Aucune mutation effectuée."
        )
    if WRITE_INVENTORY_SCOPE not in scopes:
        raise PreflightError(
            f"STOP: scope Shopify manquant : {WRITE_INVENTORY_SCOPE}.\n"
            "Impossible de modifier l'inventaire. Aucune mutation effectuée."
        )

    nodes = graphql(shop, token, LOCATIONS_QUERY)["locations"]["nodes"]
    usable = [n for n in nodes if n.get("id") and n.get("isActive")]
    if not usable:
        raise PreflightError(
            "STOP: aucun locationId Shopify actif et utilisable n'a été trouvé. "
            "Aucune mutation effectuée."
        )
    read_used = [s for s in LOCATION_READ_SCOPES if s in scopes]
    return {"location_id": usable[0]["id"], "location_name": usable[0].get("name") or "",
            "read_scopes_present": read_used}


# ----------------------------------------------------------------------
# CJ DROPSHIPPING
# ----------------------------------------------------------------------
def cj_call(method, path, cj_token=None, **kwargs):
    headers = {"Content-Type": "application/json"}
    if cj_token:
        headers["CJ-Access-Token"] = cj_token
    time.sleep(1.1)  # éviter de dépasser les limites de l'API
    response = requests.request(method, f"{CJ_BASE_URL}/{path}", headers=headers, timeout=30, **kwargs)
    if response.status_code == 429:
        raise RuntimeError("CJ : points API insuffisants (429)")
    response.raise_for_status()
    body = response.json()
    if not body.get("result"):
        raise RuntimeError(f"CJ {path} : {body.get('message')}")
    return body.get("data")


def get_cj_token():
    data = cj_call("POST", "authentication/getAccessToken", json={"apiKey": env("CJ_API_KEY")})
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
    """Option de livraison la moins chère vers la France (+ option brute pour l'audit)."""
    try:
        data = cj_call(
            "POST", "logistic/freightCalculate", cj_token,
            json={"startCountryCode": SHIP_FROM, "endCountryCode": SHIP_TO,
                  "products": [{"quantity": 1, "vid": vid}]},
        )
    except Exception:
        return None
    options = [o for o in (data or []) if isinstance(o, dict) and o.get("logisticPrice") is not None]
    if not options:
        return None
    best = min(options, key=lambda o: float(o["logisticPrice"]))
    raw = {k: v for k, v in best.items() if v is None or isinstance(v, (str, int, float, bool))}
    return {
        "price_usd": float(best["logisticPrice"]),
        "aging": str(best.get("logisticAging") or ""),
        "name": best.get("logisticName") or "",
        "raw": raw,
    }


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


def evaluate_variants(cj_token, cj_variants, title, product_weight, limit):
    """Évalue CHAQUE variante séparément, sans jamais en auditer plus de `limit`.
    Retourne (conservées, exclues, nombre_de_variantes_auditées)."""
    kept, excluded, used = [], [], set()
    window = cj_variants[:limit]
    for v in window:
        vid = v.get("vid")
        product_usd = parse_price(v.get("variantSellPrice"))
        if not vid or product_usd <= 0:
            continue
        item = {
            "vid": vid, "name": variant_label(v, title, used), "sku": v.get("variantSku") or "",
            "product_usd": product_usd,
            "weight_g": to_grams(v.get("variantWeight")) or to_grams(product_weight),
        }
        stock = get_stock(cj_token, vid)
        item["stock"] = stock
        if stock < MIN_STOCK:
            excluded.append({**item, "reason": f"Stock insuffisant ({stock} < {MIN_STOCK})"})
            continue
        ship = get_shipping(cj_token, vid)
        if not ship:
            excluded.append({**item, "reason": "Livraison vers la France indisponible"})
            continue
        days = max_days(ship["aging"])
        if days is None or days > MAX_DELIVERY_DAYS:
            excluded.append({**item, "reason": f"Délai de livraison trop long ou inconnu ({ship['aging']})"})
            continue
        item.update(variant_economics(product_usd, ship["price_usd"]))
        item["ship"] = ship
        if item["margin_eur"] < MIN_MARGIN_EUR:
            excluded.append({**item, "reason": "Marge insuffisante"})
            continue
        kept.append(item)
    return kept, excluded, len(window)


def audit_candidate(cj_token, p, limit):
    """Interroge CJ et décide si le produit est exploitable (au moins une variante rentable)."""
    pid = p["pid"]
    reasons = []

    detail = cj_call("GET", "product/query", cj_token, params={"pid": pid}) or {}
    title = (detail.get("productNameEn") or p["productNameEn"]).strip()
    category = detail.get("categoryName") or p.get("categoryName") or ""
    reasons += fit_check(title, category)

    images = [x for x in (detail.get("productImageSet") or []) if isinstance(x, str) and x.startswith("http")][:MAX_IMAGES]
    if not images and str(p.get("productImage", "")).startswith("http"):
        images = [p["productImage"]]
    if not images:
        reasons.append("aucune image")

    cj_variants = get_variants(cj_token, pid)
    kept, excluded, audited = evaluate_variants(cj_token, cj_variants, title, detail.get("productWeight"), limit)
    if not kept:
        reasons.append("aucune variante rentable (toutes exclues)" if excluded else "aucune variante exploitable")

    return {
        "ok": not reasons, "reasons": reasons, "title": title, "images": images,
        "variants": kept, "excluded": excluded, "category": category,
        "variants_cj_total": len(cj_variants), "variants_audited": audited,
        "cj_sku": detail.get("productSku") or p.get("productSku") or "",
        "material": detail.get("materialNameEn"), "weight_g": detail.get("productWeight"),
        "candidate": p,
    }


# ----------------------------------------------------------------------
# PLAN DE CRÉATION (aucune écriture) puis CRÉATION (brouillon)
# ----------------------------------------------------------------------
def inventory_item(v):
    item = {"sku": v["sku"], "cost": f"{v['cost_eur']:.2f}", "tracked": True, "requiresShipping": True}
    if v.get("weight_g"):
        item["measurement"] = {"weight": {"value": float(v["weight_g"]), "unit": "GRAMS"}}
    return item


def audit_variant_fields(v):
    keys = ("sku", "stock", "weight_g", "product_usd", "shipping_usd", "customs_usd",
            "total_usd", "total_eur", "price", "margin_eur", "reason")
    return {k: v.get(k) for k in keys if k in v}


def plan_product(p, a):
    variants = a["variants"]  # uniquement les variantes RENTABLES
    mode, options, combos = build_options(variants)

    # Traduction : jamais bloquante. Mots anglais restants => tag a-corriger, création quand même.
    tr = translate_title(a["title"])
    color_left = unknown_colors(colors_in_variants(variants))
    english_left = tr["english_left"] + color_left
    needs_fix = bool(english_left)
    title_fr = tr["title_fr"]

    first_ship = variants[0]["ship"]
    audit = {
        "script_version": SCRIPT_VERSION, "source": "CJ", "cj_pid": p["pid"], "cj_sku": a["cj_sku"],
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "config": {"usd_to_eur": USD_TO_EUR, "usd_to_eur_note": "taux de configuration, pas un taux de marché",
                   "customs_usd": CUSTOMS_USD, "vat_rate_assumed": VAT_RATE, "min_margin_eur": MIN_MARGIN_EUR,
                   "max_candidates": max_candidates(), "max_variants_per_candidate": max_variants_per_candidate()},
        "translation": {"title_en": a["title"], "title_fr": title_fr, "english_left": english_left,
                        "complete": not needs_fix},
        "shipping_to": SHIP_TO, "shipping_method_first_variant": first_ship["name"],
        "shipping_days_first_variant": first_ship["aging"], "shipping_raw_first_variant": first_ship.get("raw"),
        "material": a["material"], "weight_g": a["weight_g"], "options_mode": mode,
        "variants_cj_total": a["variants_cj_total"], "variants_audited": a["variants_audited"],
        "variants_kept": [audit_variant_fields(v) for v in variants],
        "variants_excluded": [audit_variant_fields(v) for v in a["excluded"]],
        "note": "Marge brute avant publicité, frais de paiement et retours. A verifier.",
    }
    tags = ["cj-auto", f"cj-pid-{p['pid']}", f"cj-sku-{a['cj_sku']}", f"cj-{SCRIPT_VERSION}"]
    if needs_fix:
        tags.append("a-corriger")
    product_input = {
        "title": title_fr,
        "descriptionHtml": f"<p>{html.escape(title_fr)}</p>",
        "vendor": VENDOR,
        "productType": a["category"],
        "tags": tags,
        "status": PRODUCT_STATUS,
        "metafields": [{"namespace": METAFIELD_NAMESPACE, "key": "audit", "type": "json",
                        "value": json.dumps(audit, ensure_ascii=False)}],
    }
    if options:
        product_input["productOptions"] = [{"name": n, "values": [{"name": x} for x in vals]} for n, vals in options]
    media = [{"originalSource": url, "mediaContentType": "IMAGE", "alt": title_fr} for url in a["images"]]
    return {"product_input": product_input, "media": media, "variants": variants,
            "mode": mode, "options": options, "combos": combos,
            "translation": {"title_fr": title_fr, "english_left": english_left, "complete": not needs_fix}}


def check_errors(errors, label):
    if errors:
        raise RuntimeError(f"{label} : {errors}")


CREATE_MUTATION = """
mutation($product: ProductCreateInput!, $media: [CreateMediaInput!]) {
  productCreate(product: $product, media: $media) {
    product {
      id
      title
      status
      variants(first: 50) { nodes { id selectedOptions { name value } inventoryItem { id } } }
    }
    userErrors { field message }
  }
}
"""
UPDATE_MUTATION = """
mutation($productId: ID!, $variants: [ProductVariantsBulkInput!]!) {
  productVariantsBulkUpdate(productId: $productId, variants: $variants) {
    productVariants { id price inventoryItem { id sku } }
    userErrors { field message }
  }
}
"""
BULK_CREATE_MUTATION = """
mutation($productId: ID!, $variants: [ProductVariantsBulkInput!]!) {
  productVariantsBulkCreate(productId: $productId, variants: $variants) {
    productVariants { id inventoryItem { id sku } }
    userErrors { field message }
  }
}
"""
ACTIVATE_MUTATION = """
mutation($inventoryItemId: ID!, $locationId: ID!) {
  inventoryActivate(inventoryItemId: $inventoryItemId, locationId: $locationId) {
    inventoryLevel { id }
    userErrors { field message }
  }
}
"""
SET_QUANTITIES_MUTATION = """
mutation($input: InventorySetQuantitiesInput!) {
  inventorySetQuantities(input: $input) {
    inventoryAdjustmentGroup { id }
    userErrors { field message code }
  }
}
"""


def set_inventory(shop, token, location_id, items, pid, label):
    """items : [(inventoryItemId, quantité)]. Appelé UNIQUEMENT après un preflight réussi."""
    for item_id, _ in items:
        res = graphql(shop, token, ACTIVATE_MUTATION, {"inventoryItemId": item_id, "locationId": location_id})["inventoryActivate"]
        check_errors(res["userErrors"], f"Activation du stock refusée ({label})")
    quantities = [{"inventoryItemId": i, "locationId": location_id, "quantity": int(q), "changeFromQuantity": None}
                  for i, q in items]
    res = graphql(shop, token, SET_QUANTITIES_MUTATION, {"input": {
        "name": "available", "reason": INVENTORY_REASON,
        "referenceDocumentUri": f"gid://solinza-automation/CJImport/{pid}", "quantities": quantities,
    }})["inventorySetQuantities"]
    check_errors(res["userErrors"], f"Quantités refusées ({label})")


def create_product(shop, token, plan, location_id, pid):
    # Règle absolue : tout produit créé est en DRAFT. Vérifié avant la moindre mutation.
    if plan["product_input"].get("status") != PRODUCT_STATUS:
        raise RuntimeError(f"Refus : le statut du produit doit être {PRODUCT_STATUS}.")
    variants, options, combos = plan["variants"], plan["options"], plan["combos"]
    data = graphql(shop, token, CREATE_MUTATION, {"product": plan["product_input"], "media": plan["media"]})["productCreate"]
    check_errors(data["userErrors"], "Création refusée")
    product = data["product"]
    label = f"produit {product['id']} déjà créé en brouillon"
    first_node = product["variants"]["nodes"][0]

    # La variante créée automatiquement porte la 1re valeur de chaque option = la 1re variante conservée.
    first_combo = tuple(o["value"] for o in (first_node.get("selectedOptions") or []))
    if options and first_combo != combos[0]:
        raise RuntimeError(f"Incohérence d'options ({label}) : {first_combo} != {combos[0]}")

    first = variants[0]
    upd = graphql(shop, token, UPDATE_MUTATION, {
        "productId": product["id"],
        "variants": [{"id": first_node["id"], "price": f"{first['price']:.2f}", "inventoryItem": inventory_item(first)}],
    })["productVariantsBulkUpdate"]
    check_errors(upd["userErrors"], f"Prix refusé ({label})")
    inventory_ids = [upd["productVariants"][0]["inventoryItem"]["id"]]

    if len(variants) > 1:
        rest = []
        for i, v in enumerate(variants[1:], start=1):
            rest.append({
                "price": f"{v['price']:.2f}",
                "optionValues": [{"optionName": options[j][0], "name": combos[i][j]} for j in range(len(options))],
                "inventoryItem": inventory_item(v),
            })
        res = graphql(shop, token, BULK_CREATE_MUTATION, {"productId": product["id"], "variants": rest})["productVariantsBulkCreate"]
        check_errors(res["userErrors"], f"Variantes refusées ({label})")
        inventory_ids += [pv["inventoryItem"]["id"] for pv in res["productVariants"]]

    set_inventory(shop, token, location_id, list(zip(inventory_ids, [v["stock"] for v in variants])), pid, label)
    return product


# ----------------------------------------------------------------------
# AFFICHAGE (résumé / simulation)
# ----------------------------------------------------------------------
def config_lines(n_candidates, n_variants):
    return [
        f"- Limites utilisées : MAX_CANDIDATES = {n_candidates} ; MAX_VARIANTS_PER_CANDIDATE = {n_variants} ; MAX_SCAN = {MAX_SCAN}",
        f"- Taux USD→EUR utilisé : {USD_TO_EUR:.2f} (taux de configuration du calcul v4, pas un taux de marché en temps réel)",
        f"- Douane supposée : {CUSTOMS_USD:.2f} USD par article (configuration, à vérifier) ; TVA supposée : {VAT_RATE * 100:.0f} %",
        f"- Coût total d'une variante = prix produit CJ + livraison + douane ; marge minimale : {MIN_MARGIN_EUR} EUR par variante",
        f"- Statut de tout produit créé : {PRODUCT_STATUS} (jamais de publication automatique)",
    ]


def preflight_lines(pf):
    return [
        "- Preflight Shopify (lecture seule) : réussi ✅",
        f"  - Lecture des locations autorisée via : {' + '.join(pf['read_scopes_present'])} (l'un des deux suffit)",
        f"  - {WRITE_INVENTORY_SCOPE} : présent ; locationId retenu : {pf['location_id']} ({pf['location_name']})",
    ]


def excluded_lines(excluded):
    lines = []
    for v in excluded:
        lines.append(f"- **{v['name']} — EXCLUE**")
        lines.append(f"  - {v['reason']}")
        if "total_usd" in v:
            lines.append(
                f"  - Coût total : {v['total_usd']:.2f} USD / {v['total_eur']:.2f} EUR "
                f"(produit {v['product_usd']:.2f} + livraison {v['shipping_usd']:.2f} + douane {v['customs_usd']:.2f} USD)"
            )
            lines.append(f"  - Prix de vente : {v['price']:.2f} EUR TTC ({v['revenue_ht']:.2f} EUR HT) ; marge {v['margin_eur']:.2f} EUR < seuil {MIN_MARGIN_EUR}")
    return lines


def plan_lines(plan, a, p):
    tr = plan["translation"]
    if tr["complete"]:
        tr_line = f"- Traduction du titre : complète ✅ → pas de tag a-corriger. Titre : **{tr['title_fr']}**"
    else:
        tr_line = (f"- Traduction du titre : incomplète ⚠️ (mots anglais restants : {', '.join(tr['english_left'])}) "
                   f"→ tag a-corriger ajouté ; le produit est créé quand même. Titre : **{tr['title_fr']}**")
    lines = [
        f"- Titre CJ : {a['title']}",
        tr_line,
        f"- Statut : **{PRODUCT_STATUS}** (toujours)",
        f"- Catégorie CJ : {a['category'] or 'inconnue'}",
        f"- Options : {plan['mode']} -> " + (", ".join(f"{n}: {', '.join(vals)}" for n, vals in plan['options']) or "aucune"),
        f"- Variantes CJ : {a['variants_cj_total']} ; auditées : {a['variants_audited']} (limite {max_variants_per_candidate()}) ; "
        f"conservées : {len(plan['variants'])} ; exclues : {len(a['excluded'])}",
        f"- Photos : {len(a['images'])} ; mot-clé d'origine : {p.get('keyword')}",
        "", "**Variantes conservées**", "",
        "| Variante | SKU | Produit USD | Livraison USD | Douane USD | Total USD | Total EUR | Prix TTC | Marge EUR | Stock CJ | Poids g |",
        "|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for v in plan["variants"]:
        lines.append(
            f"| {v['name']} | {v['sku']} | {v['product_usd']:.2f} | {v['shipping_usd']:.2f} | {v['customs_usd']:.2f} | "
            f"{v['total_usd']:.2f} | {v['total_eur']:.2f} | {v['price']:.2f} | {v['margin_eur']:.2f} | {v['stock']} | {v.get('weight_g') or 'n/d'} |"
        )
    if a["excluded"]:
        lines += ["", "**Variantes exclues (jamais créées)**", ""] + excluded_lines(a["excluded"])
    return lines


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
    apply = os.environ.get("APPLY") == "1"
    summary = [f"## CJ vers Shopify {SCRIPT_VERSION} — " + ("CRÉATION DU BROUILLON" if apply else "SIMULATION (aucune écriture)"), ""]
    try:
        n_candidates, n_variants = max_candidates(), max_variants_per_candidate()
        summary += config_lines(n_candidates, n_variants)

        shop = shop_subdomain()
        token = get_shopify_token(shop)
        summary.append("- Connexion Shopify : réussie ✅")

        # PREFLIGHT (lecture seule) : toujours exécuté, avant CJ et avant toute mutation.
        pf = preflight(shop, token)
        summary += preflight_lines(pf)

        cj_token = get_cj_token()
        summary.append("- Connexion CJ : réussie ✅")
        candidates = load_candidates()
        summary.append(f"- Produits candidats : {len(candidates)}")

        scanned = audited = ignored_lines = audited_variants_total = 0
        accepted = []
        for p in candidates:
            if scanned >= MAX_SCAN or audited >= n_candidates:
                break
            scanned += 1
            if already_in_store(shop, token, p["pid"]):
                continue
            fit = fit_check(p.get("productNameEn", ""), p.get("categoryName", ""))
            if fit:
                if ignored_lines < 15:
                    summary.append(f"- ⏭️ Ignoré : {p.get('productNameEn')} → {fit[0]}")
                    ignored_lines += 1
                continue
            audited += 1
            a = audit_candidate(cj_token, p, n_variants)
            audited_variants_total += a["variants_audited"]
            if not a["ok"]:
                summary.append(f"- ❌ Produit rejeté : **{a['title']}** → " + " ; ".join(a["reasons"]))
                summary += excluded_lines(a["excluded"])
                continue
            if sku_in_store(shop, token, a["variants"][0]["sku"]):
                summary.append(f"- ⏭️ Doublon (SKU déjà en boutique) : **{a['title']}**")
                continue
            accepted.append(a)

        summary.append(f"- Candidats examinés : {scanned} ; audités : {audited}/{n_candidates} ; "
                       f"variantes auditées : {audited_variants_total} (max {n_variants} par candidat)")

        if not accepted:
            summary.append("- Aucun produit retenu. Voir les raisons ci-dessus.")
        else:
            accepted.sort(key=rank_score, reverse=True)       # classement ; 1 seul candidat retenu
            for rank, a in enumerate(accepted, start=1):
                avg, n = rank_score(a)
                summary.append(f"- Classement {rank} : {a['title']} (marge moyenne {avg:.2f} EUR, {n} variante(s))")
            best = accepted[0]
            p = best["candidate"]
            plan = plan_product(p, best)
            if not apply:
                summary.append("- 🧪 SIMULATION : voici ce qui serait créé en brouillon (rien n'a été écrit)")
            else:
                product = create_product(shop, token, plan, pf["location_id"], p["pid"])
                summary.append(f"- Brouillon créé : **{product['title']}** ✅ ({product['id']})")
            summary += plan_lines(plan, best, p)
    except PreflightError as e:
        summary += ["", f"### ⛔ {e}"]
        write_summary(summary)
        print("\n".join(summary))
        sys.exit(1)
    except Exception as e:
        summary += ["", f"### ❌ Erreur : {e}"]
        write_summary(summary)
        print(f"Erreur : {e}")
        sys.exit(1)

    write_summary(summary)
    print("\n".join(summary))


if __name__ == "__main__":
    main()
