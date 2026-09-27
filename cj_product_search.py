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
