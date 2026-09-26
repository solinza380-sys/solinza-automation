"""
Solinza - Recherche de produits tendance (vêtements femme)
=============================================================
"""

import time
import pandas as pd
from pytrends.request import TrendReq

KEYWORDS = [
    "manteau long femme",
    "robe longue fleurie",
    "pull femme col v",
    "jean stretch femme",
    "veste femme tendance",
    "robe d'hiver femme",
    "sac femme tendance",
    "cardigan femme oversize",
    "combinaison femme",
    "jupe longue femme",
 "veste jean femme",
    "robe fluide femme",
    "pantalon large femme",
    "pull oversize femme",
    "robe pull femme",
    "trench coat femme",
    "salopette femme",
    "kimono femme",
    "body femme",
    "maillot bain femme",]

GEO = "FR"

pytrends = TrendReq(hl="fr-FR", tz=60)

results = []

for kw in KEYWORDS:
    try:
        pytrends.build_payload([kw], cat=0, timeframe="today 3-m", geo=GEO)
        data = pytrends.interest_over_time()

        if data.empty:
            print(f"[!] Pas de données pour : {kw}")
            continue

        series = data[kw]
        recent = series.tail(2).mean()
        previous = series.tail(4).head(2).mean()
        growth = ((recent - previous) / previous * 100) if previous > 0 else 0

        results.append({
            "mot_cle": kw,
            "interet_moyen_3mois": round(series.mean(), 1),
            "interet_recent": round(recent, 1),
            "croissance_pct": round(growth, 1),
        })

        print(f"[OK] {kw} -> intérêt moyen: {series.mean():.1f}, croissance: {growth:.1f}%")

    except Exception as e:
        print(f"[ERREUR] {kw} : {e}")

    time.sleep(5)

df = pd.DataFrame(results)

if not df.empty:
    df = df.sort_values(by="croissance_pct", ascending=False)
    df.to_csv("trending_products.csv", index=False, encoding="utf-8-sig")
    print("\n=== Classement des produits ===")
    print(df.to_string(index=False))
    print("\nFichier exporté : trending_products.csv")
else:
    print("Aucun résultat récupéré. Vérifie ta connexion ou réessaie plus tard.")
