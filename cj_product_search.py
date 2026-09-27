"""
cj_product_search.py

Ce script fait deux choses :
1. Se connecte à l'API CJ Dropshipping (avec email + password + API key
   stockés dans les GitHub Secrets) pour récupérer un Access Token.
2. Utilise ce token pour chercher des produits correspondant aux
   mots-clés tendance trouvés via Google Trends (robe fluide femme,
   trench coat femme, pull femme col v, etc.).

Variables d'environnement nécessaires (déjà ajoutées dans GitHub Secrets) :
  - CJ_EMAIL
  - CJ_PASSWORD
  - CJ_API_KEY
"""

import os
import json
import time
import requests

CJ_BASE_URL = "https://developers.cjdropshipping.com/api2.0/v1"

# Mots-clés tendance identifiés (à ajuster/étendre selon les résultats
# du script trending_products_solinza.py)
KEYWORDS = [
    "robe fluide femme",
    "trench coat femme",
    "pull femme col v",
]


def get_access_token():
    """Récupère un Access Token CJ Dropshipping."""
    url = f"{CJ_BASE_URL}/authentication/getAccessToken"
    payload = {
        "email": os.environ["CJ_EMAIL"],
        "password": os.environ["CJ_PASSWORD"],
    }
    headers = {
        "Content-Type": "application/json",
        # Certaines versions de l'API CJ demandent aussi la clé API
        # dans les headers ; on l'ajoute par sécurité.
        "CJ-Access-Token": os.environ.get("CJ_API_KEY", ""),
    }

    response = requests.post(url, json=payload, headers=headers, timeout=30)
    response.raise_for_status()
    data = response.json()

    if not data.get("result"):
        raise RuntimeError(f"Échec de l'authentification CJ : {data}")

    access_token = data["data"]["accessToken"]
    print("Access Token récupéré avec succès.")
    return access_token


def search_products(access_token, keyword, page_size=10):
    """Cherche des produits CJ Dropshipping correspondant à un mot-clé."""
    url = f"{CJ_BASE_URL}/product/list"
    headers = {
        "CJ-Access-Token": access_token,
        "Content-Type": "application/json",
    }
    params = {
        "productNameEn": keyword,
        "pageSize": page_size,
        "pageNum": 1,
    }

    response = requests.get(url, headers=headers, params=params, timeout=30)
    response.raise_for_status()
    data = response.json()

    if not data.get("result"):
        print(f"Aucun résultat ou erreur pour '{keyword}': {data.get('message')}")
        return []

    return data.get("data", {}).get("list", [])


def main():
    access_token = get_access_token()

    all_results = {}
    for keyword in KEYWORDS:
        print(f"\nRecherche des produits pour : {keyword}")
        products = search_products(access_token, keyword)
        all_results[keyword] = products
        print(f"  -> {len(products)} produit(s) trouvé(s)")
        time.sleep(1)  # éviter de dépasser les limites de l'API

    # Sauvegarde des résultats dans un fichier JSON (utile pour
    # inspection manuelle ou pour une étape suivante d'automatisation)
    output_path = "cj_search_results.json"
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(all_results, f, ensure_ascii=False, indent=2)

    print(f"\nRésultats sauvegardés dans {output_path}")


if __name__ == "__main__":
    main()
