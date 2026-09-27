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
