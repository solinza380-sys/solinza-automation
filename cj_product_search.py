"""
cj_product_search.py

Ce script fait deux choses :
1. Se connecte à l'API CJ Dropshipping (avec la clé API stockée dans
   les GitHub Secrets) pour récupérer un Access Token.
2. Utilise ce token pour chercher des produits correspondant aux
   mots-clés tendance trouvés via Google Trends (robe fluide femme,
   trench coat femme, pull femme col v, etc.).

Variable d'environnement nécessaire (déjà ajoutée dans GitHub Secrets) :
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
    """Récupère un Access Token CJ Dropshipping (nouvelle API : apiKey seul)."""
    url = f"{CJ_BASE_URL}/authentication/getAccessToken"
    payload = {
        "apiKey": os.environ["CJ_API_KEY"].strip(),
    }
    headers = {
        "Content-Type": "application/json",
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
        "CJ-Access-Token": access_token.strip(),
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


def write_summary(lines):
    """Écrit un résumé lisible directement dans l'onglet Summary de
    GitHub Actions (plus simple à consulter que les logs bruts)."""
    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    if not summary_path:
        return
    with open(summary_path, "a", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")


def main():
    summary = ["## Résultat de la recherche CJ Dropshipping", ""]
    all_results = {}

    try:
        access_token = get_access_token()
        summary.append("- Access Token : récupéré avec succès ✅")

        for keyword in KEYWORDS:
            print(f"\nRecherche des produits pour : {keyword}")
            products = search_products(access_token, keyword)
            all_results[keyword] = products
            print(f"  -> {len(products)} produit(s) trouvé(s)")
            summary.append(f"- **{keyword}** : {len(products)} produit(s) trouvé(s)")
            time.sleep(1)  # éviter de dépasser les limites de l'API

    except Exception as e:
        summary.append("")
        summary.append(f"### ❌ Erreur : {e}")
        write_summary(summary)
        raise

    # Sauvegarde des résultats dans un fichier JSON (utile pour
    # inspection manuelle ou pour une étape suivante d'automatisation)
    output_path = "cj_search_results.json"
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(all_results, f, ensure_ascii=False, indent=2)

    summary.append("")
    summary.append(f"Résultats sauvegardés dans `{output_path}`.")
    write_summary(summary)
    print(f"\nRésultats sauvegardés dans {output_path}")


if __name__ == "__main__":
    main()
