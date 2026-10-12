"""Vérifie le jeu de questions et produit une page de relecture.

- chaque cas a les champs requis et des types attendus valides ;
- chaque (massif, date) attendu correspond à un bulletin en base : les faits de référence
  (niveau, détail, pentes) sont lus en SQL, jamais écrits à la main ;
- écrit <fichier>.html (cas.html par défaut) : une section par question, avec les faits de référence.

Usage : uv run python evals/assistant/verifier_cas.py [cas_caches.json]   (défaut : cas.json)
"""
from __future__ import annotations

import html
import json
import sys
from collections import Counter
from datetime import date
from pathlib import Path

RACINE = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(RACINE))

from bra.assistant import formater_faits  # noqa: E402
from bra.database import MASSIFS, connecter  # noqa: E402
from bra.requetes import bulletin_du_jour  # noqa: E402

DOSSIER = Path(__file__).resolve().parent
TYPES = {"bulletin", "comparaison", "generale", "precision", "absent", "hors_sujet"}
IDS_MASSIFS = {m[0] for m in MASSIFS}


def charger_cas(nom: str = "cas.json") -> list[dict]:
    return json.loads((DOSSIER / nom).read_text(encoding="utf-8"))


def verifier(cas: list[dict], conn) -> tuple[list[str], dict[str, list[dict]]]:
    erreurs, references = [], {}
    ids = Counter(c["id"] for c in cas)
    erreurs += [f"id en double : {i}" for i, n in ids.items() if n > 1]
    questions = Counter(c["question"].strip().lower() for c in cas)
    erreurs += [f"question en double : {q}" for q, n in questions.items() if n > 1]
    for c in cas:
        a = c["attendu"]
        if not set(a["types"]) <= TYPES:
            erreurs.append(f"{c['id']} : type inconnu {a['types']}")
        date.fromisoformat(c["aujourd_hui"])
        if a.get("massifs"):
            if not set(a["massifs"]) <= IDS_MASSIFS:
                erreurs.append(f"{c['id']} : massif inconnu {a['massifs']}")
                continue
            refs = []
            for m in a["massifs"]:
                b = bulletin_du_jour(conn, m, date.fromisoformat(a["date_validite"]))
                if b is None:
                    erreurs.append(f"{c['id']} : aucun bulletin {m} le {a['date_validite']}")
                    continue
                if a.get("date_bulletin") and b["date_bulletin"] != a["date_bulletin"]:
                    erreurs.append(f"{c['id']} : bulletin attendu émis le {a['date_bulletin']}, trouvé {b['date_bulletin']}")
                refs.append(b)
            references[c["id"]] = refs
        elif {"bulletin", "comparaison"} & set(a["types"]):
            if a["types"] != ["bulletin", "generale"]:  # s35 : l'un ou l'autre accepté, sans faits imposés
                erreurs.append(f"{c['id']} : type bulletin sans massif attendu")
    return erreurs, references


def page_html(cas: list[dict], references: dict[str, list[dict]]) -> str:
    par_categorie = Counter(c["tags"][0] for c in cas)
    sections = []
    for c in cas:
        a = c["attendu"]
        faits = "".join(
            f"<pre>{html.escape(formater_faits(b))}</pre>" for b in references.get(c["id"], [])
        )
        criteres = "".join(f"<li>{html.escape(x)}</li>" for x in c["criteres"]) or "<li><em>aucun critère spécifique</em></li>"
        attendu = " ou ".join(a["types"])
        if a.get("massifs"):
            attendu += f" — {', '.join(a['massifs'])} — bulletin valable le {a['date_validite']}"
        sections.append(f"""
<section>
  <h2><span class="id">{c['id']}</span> {html.escape(c['question'])}</h2>
  <p class="meta">{' · '.join(html.escape(t) for t in c['tags'])} · date simulée : {c['aujourd_hui']}</p>
  <p><strong>Attendu :</strong> {html.escape(attendu)}</p>
  <p><strong>Critères vérifiés par le juge :</strong></p><ul>{criteres}</ul>
  {f'<details><summary>Faits de référence (SQL)</summary>{faits}</details>' if faits else ''}
</section>""")
    resume = ", ".join(f"{k} : {n}" for k, n in sorted(par_categorie.items()))
    return f"""<!doctype html>
<html lang="fr"><head><meta charset="utf-8"><title>Jeu de questions — assistant BRA</title>
<style>
:root {{ --fond:#fff; --texte:#1a1a1a; --doux:#666; --bord:#ddd; --code:#f5f5f5; }}
@media (prefers-color-scheme: dark) {{ :root {{ --fond:#161616; --texte:#eee; --doux:#aaa; --bord:#333; --code:#222; }} }}
body {{ background:var(--fond); color:var(--texte); font:15px/1.5 system-ui, sans-serif; max-width:860px; margin:0 auto; padding:16px; }}
section {{ border-top:1px solid var(--bord); padding:12px 0; }}
h2 {{ font-size:17px; margin:0 0 4px; }} .id {{ color:var(--doux); font-family:monospace; margin-right:6px; }}
.meta {{ color:var(--doux); font-size:13px; margin:0 0 8px; }}
pre {{ background:var(--code); padding:8px; white-space:pre-wrap; font-size:13px; }}
</style></head><body>
<h1>Jeu de questions — assistant BRA</h1>
<p>{len(cas)} questions · {resume}</p>
<p>Pour chaque question : est-elle réaliste ? La réponse attendue est-elle la bonne ? Manque-t-il des cas ?</p>
{''.join(sections)}
</body></html>"""


def main() -> int:
    nom = sys.argv[1] if len(sys.argv) > 1 else "cas.json"
    cas = charger_cas(nom)
    conn = connecter(RACINE / "data" / "bra.sqlite")
    erreurs, references = verifier(cas, conn)
    page = DOSSIER / f"{Path(nom).stem}.html"
    page.write_text(page_html(cas, references), encoding="utf-8")
    print(f"{len(cas)} cas ; par catégorie : {dict(Counter(c['tags'][0] for c in cas))}")
    print(f"par source : {dict(Counter(c['tags'][1] for c in cas))}")
    print(f"types attendus : {dict(Counter(' | '.join(c['attendu']['types']) for c in cas))}")
    for e in erreurs:
        print(f"ERREUR {e}")
    print(f"page écrite : {page}")
    return 1 if erreurs else 0


if __name__ == "__main__":
    sys.exit(main())
