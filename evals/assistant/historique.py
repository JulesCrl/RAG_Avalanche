"""Historique des scores de l'évaluation, une entrée par version de l'assistant.

Les valeurs sont recalculées depuis results.jsonl (jamais saisies à la main) et ajoutées à
evals/assistant/historique.json, qui servira à tracer l'évolution des bonnes réponses.

Usage :
  uv run python evals/assistant/historique.py ajouter baseline "Mesure de départ"
  uv run python evals/assistant/historique.py ajouter v1 "Surnoms, classification, dates relatives"
  uv run python evals/assistant/historique.py afficher
  uv run python evals/assistant/historique.py recalculer   # après avoir renoté des versions avec un nouveau juge
"""
from __future__ import annotations

import json
import sys
from datetime import date
from pathlib import Path

DOSSIER = Path(__file__).resolve().parent
sys.path.insert(0, str(DOSSIER))

from run_eval import DOSSIER_FLOW, METRIQUES, wilson  # noqa: E402

FICHIER = DOSSIER / "historique.json"


def calculer(variante: str) -> dict:
    lignes = [json.loads(l) for l in (DOSSIER_FLOW / variante / "results.jsonl").read_text().splitlines()]
    ok = [l for l in lignes if l["status"] == "ok"]
    if not ok:
        raise SystemExit(f"aucun résultat noté pour {variante}")
    reussites = sum(l["grade"]["reussite"] for l in ok)
    bas, haut = wilson(reussites, len(ok))
    par_metrique = {}
    for m in METRIQUES:
        valeurs = [l["grade"][m["id"]] for l in ok if l["grade"][m["id"]] is not None]
        par_metrique[m["id"]] = round(sum(valeurs) / len(valeurs), 4) if valeurs else None
    erreurs = DOSSIER_FLOW / variante / "errors.jsonl"
    return {
        "reussite": round(reussites / len(ok), 4),
        "ic95": [round(bas, 4), round(haut, 4)],
        "bonnes_reponses": int(reussites),
        "reponses_notees": len(ok),
        "cas": len({l["prompt_id"] for l in ok}),
        "passages": max(l["rep"] for l in ok) + 1,
        "metriques": par_metrique,
        "modele_teste": sorted({l["model"] for l in ok}),
        "juge": sorted({l["judge_model"] for l in ok}),
        "version_juge": sorted({l.get("judge_version", 1) for l in ok}),
        "cout_usd": round(sum(l["cost_usd"] + l["judge_cost_usd"] for l in lignes), 3),
        "erreurs_techniques": len(erreurs.read_text().splitlines()) if erreurs.exists() else 0,
    }


def charger() -> list[dict]:
    return json.loads(FICHIER.read_text(encoding="utf-8")) if FICHIER.exists() else []


def main() -> int:
    if len(sys.argv) >= 2 and sys.argv[1] == "afficher":
        for e in charger():
            print(f"{e['version']:<9} {e['date']}  juge v{','.join(map(str, e.get('version_juge', [1])))}  {e['reussite']:.1%} "
                  f"({e['bonnes_reponses']}/{e['reponses_notees']}, IC 95 % {e['ic95'][0]:.0%}-{e['ic95'][1]:.0%})  {e['description']}")
        return 0
    if len(sys.argv) == 2 and sys.argv[1] == "recalculer":
        historique = [{**e, **calculer(e["version"])} for e in charger()]
        FICHIER.write_text(json.dumps(historique, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        return main_afficher()
    if len(sys.argv) != 4 or sys.argv[1] != "ajouter":
        print(__doc__)
        return 1
    _, _, variante, description = sys.argv
    historique = charger()
    if any(e["version"] == variante for e in historique):
        raise SystemExit(f"{variante} est déjà dans l'historique")
    historique.append({"version": variante, "date": date.today().isoformat(), "description": description, **calculer(variante)})
    FICHIER.write_text(json.dumps(historique, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return main_afficher()


def main_afficher() -> int:
    sys.argv = [sys.argv[0], "afficher"]
    return main()


if __name__ == "__main__":
    sys.exit(main())
