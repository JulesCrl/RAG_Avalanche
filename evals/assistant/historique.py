"""Historique des scores de l'évaluation, une entrée par version de l'assistant.

Les notes de chaque réponse sont exportées dans evals/assistant/resultats/<version>.jsonl (versionné :
notes, modèles et coûts, sans les textes du guide ni des réponses). Les scores de historique.json en sont
recalculés (jamais saisis à la main) : chacun peut les vérifier depuis le dépôt.

Usage :
  uv run python evals/assistant/historique.py ajouter baseline "Mesure de départ"
  uv run python evals/assistant/historique.py ajouter v1 "Surnoms, classification, dates relatives"
  uv run python evals/assistant/historique.py afficher
  uv run python evals/assistant/historique.py recalculer   # après un rejugement ou un changement de calcul
"""
from __future__ import annotations

import json
import sys
from datetime import date
from pathlib import Path

DOSSIER = Path(__file__).resolve().parent
sys.path.insert(0, str(DOSSIER))

from run_eval import DOSSIER_SORTIES, METRIQUES, ic_bootstrap  # noqa: E402

FICHIER = DOSSIER / "historique.json"
DOSSIER_RESULTATS = DOSSIER / "resultats"
CHAMPS_EXPORTES = ("prompt_id", "rep", "status", "grade", "model", "judge_model", "judge_version",
                   "in_tokens", "out_tokens", "latency_s", "cost_usd", "judge_cost_usd")


def exporter(variante: str) -> None:
    """Copie les notes des sorties brutes (non versionnées) vers resultats/, si elles sont disponibles."""
    brut = DOSSIER_SORTIES / variante / "results.jsonl"
    if not brut.exists():
        return
    lignes = [json.loads(l) for l in brut.read_text().splitlines()]
    lignes.sort(key=lambda l: (l["prompt_id"], l["rep"]))
    DOSSIER_RESULTATS.mkdir(exist_ok=True)
    (DOSSIER_RESULTATS / f"{variante}.jsonl").write_text(
        "".join(json.dumps({k: l.get(k) for k in CHAMPS_EXPORTES}, ensure_ascii=False) + "\n" for l in lignes))


def calculer(variante: str) -> dict:
    exporter(variante)
    lignes = [json.loads(l) for l in (DOSSIER_RESULTATS / f"{variante}.jsonl").read_text().splitlines()]
    ok = [l for l in lignes if l["status"] == "ok"]
    if not ok:
        raise SystemExit(f"aucun résultat noté pour {variante}")
    reussites = sum(l["grade"]["reussite"] for l in ok)
    bas, haut = ic_bootstrap(ok)
    par_metrique = {}
    for m in METRIQUES:
        valeurs = [l["grade"][m["id"]] for l in ok if l["grade"][m["id"]] is not None]
        par_metrique[m["id"]] = round(sum(valeurs) / len(valeurs), 4) if valeurs else None
    erreurs = DOSSIER_SORTIES / variante / "errors.jsonl"
    # Sans les sorties brutes, le nombre d'erreurs techniques déjà enregistré dans historique.json est conservé.
    bilan_erreurs = ({"erreurs_techniques": len(erreurs.read_text().splitlines()) if erreurs.exists() else 0}
                     if (DOSSIER_SORTIES / variante).exists() else {})
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
        **bilan_erreurs,
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
