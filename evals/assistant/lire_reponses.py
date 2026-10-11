"""Page HTML de lecture des réponses d'une version : question, réponse affichée, verdicts du juge, documents.

Usage : uv run python evals/assistant/lire_reponses.py v2        -> evals/assistant/reponses_v2.html
"""
from __future__ import annotations

import html
import json
import sys
from collections import defaultdict
from pathlib import Path

DOSSIER = Path(__file__).resolve().parent
sys.path.insert(0, str(DOSSIER))

from run_eval import DOSSIER_FLOW  # noqa: E402

LIBELLES = {"type_ok": "Bon type", "faits_ok": "Faits SQL", "fidele": "Fidélité",
            "pas_feu_vert": "Pas feu vert", "criteres": "Critères"}


def pastille(valeur) -> str:
    if valeur is None:
        return '<span class="na">—</span>'
    classe = "ok" if valeur == 1 else "ko"
    texte = "OK" if valeur == 1 else ("KO" if valeur == 0 else f"{valeur:.0%}")
    return f'<span class="{classe}">{texte}</span>'


def main() -> int:
    variante = sys.argv[1] if len(sys.argv) > 1 else "v2"
    dossier = DOSSIER_FLOW / variante
    cas = {c["id"]: c for c in json.loads((DOSSIER / "cas.json").read_text(encoding="utf-8"))}
    lignes = [json.loads(l) for l in (dossier / "results.jsonl").read_text().splitlines()]
    par_cas = defaultdict(list)
    for l in sorted(lignes, key=lambda l: l["rep"]):
        par_cas[l["prompt_id"]].append(l)

    sections = []
    for cid in [c for c in cas if c in par_cas]:
        c, reps = cas[cid], par_cas[cid]
        reussites = sum(r["grade"]["reussite"] for r in reps)
        etat = "ok" if reussites == len(reps) else ("ko" if reussites == 0 else "mixte")
        blocs = []
        for r in reps:
            trace = json.loads((dossier / "traces" / f"{cid}_rep{r['rep']}.json").read_text())
            sortie = next(t["content"] for t in reversed(trace) if t["role"] == "assistant")
            docs = next((t["content"] for t in trace if t["role"] == "tool_result"), "")
            classif = next((t["content"] for t in trace if t["role"] == "tool_call"), "")
            notes = " ".join(f"{LIBELLES[m]} {pastille(r['grade'][m])}" for m in LIBELLES)
            verdicts = "".join(
                f"<li><strong>{LIBELLES[m]}</strong> : {html.escape(r['explanation'].get(m, ''))}</li>"
                for m in LIBELLES if r["grade"][m] is not None and r["grade"][m] < 1)
            blocs.append(f"""
  <div class="rep">
    <p class="notes">Passage {r['rep'] + 1} · réussite {pastille(r['grade']['reussite'])} · {notes} · {r['latency_s']} s</p>
    <pre class="sortie">{html.escape(sortie)}</pre>
    {f'<ul class="verdicts">{verdicts}</ul>' if verdicts else ''}
    <details><summary>Classification</summary><pre>{html.escape(classif)}</pre></details>
    {f'<details><summary>Documents fournis à Claude</summary><pre>{html.escape(docs)}</pre></details>' if docs else ''}
  </div>""")
        attendu = " ou ".join(c["attendu"]["types"])
        if c["attendu"].get("massifs"):
            attendu += f" — {', '.join(c['attendu']['massifs'])} — {c['attendu']['date_validite']}"
        criteres = "".join(f"<li>{html.escape(x)}</li>" for x in c["criteres"])
        sections.append(f"""
<section class="{etat}" id="{cid}">
  <h2><span class="id">{cid}</span> {html.escape(c['question'])} <span class="score {etat}">{reussites:.0f}/{len(reps)}</span></h2>
  <p class="meta">{' · '.join(map(html.escape, c['tags']))} · date simulée {c['aujourd_hui']} · attendu : {html.escape(attendu)}</p>
  {f'<ul class="criteres">{criteres}</ul>' if criteres else ''}
  {''.join(blocs)}
</section>""")

    total = sum(l["grade"]["reussite"] for l in lignes)
    sommaire = " ".join(
        f'<a class="{"ok" if sum(r["grade"]["reussite"] for r in par_cas[k]) == len(par_cas[k]) else "ko" if sum(r["grade"]["reussite"] for r in par_cas[k]) == 0 else "mixte"}" href="#{k}">{k}</a>'
        for k in cas if k in par_cas)
    page = f"""<!doctype html><html lang="fr"><head><meta charset="utf-8"><title>Réponses {variante}</title>
<style>
:root {{ --fond:#fff; --texte:#1a1a1a; --doux:#666; --bord:#ddd; --code:#f6f6f6; --ok:#1a7f37; --ko:#c62828; --mixte:#b26a00; }}
@media (prefers-color-scheme: dark) {{ :root {{ --fond:#161616; --texte:#eee; --doux:#aaa; --bord:#333; --code:#222; --ok:#5fd07a; --ko:#ff6b6b; --mixte:#f0b44c; }} }}
body {{ background:var(--fond); color:var(--texte); font:15px/1.5 system-ui, sans-serif; max-width:900px; margin:0 auto; padding:16px; }}
section {{ border-top:1px solid var(--bord); padding:14px 0; }}
h2 {{ font-size:17px; margin:0 0 4px; }} .id {{ color:var(--doux); font-family:monospace; }}
.meta, .notes {{ color:var(--doux); font-size:13px; margin:2px 0 8px; }}
pre {{ background:var(--code); padding:10px; white-space:pre-wrap; font-size:13px; border-radius:4px; }}
.sortie {{ font-size:14px; }} .rep {{ margin:10px 0 18px; padding-left:10px; border-left:3px solid var(--bord); }}
.ok {{ color:var(--ok); font-weight:600; }} .ko {{ color:var(--ko); font-weight:600; }} .mixte {{ color:var(--mixte); font-weight:600; }}
.na {{ color:var(--doux); }} .score {{ float:right; }} .verdicts li {{ margin-bottom:6px; }} .criteres {{ color:var(--doux); font-size:13px; }}
.sommaire a {{ margin-right:6px; text-decoration:none; font-family:monospace; }}
</style></head><body>
<h1>Réponses de l'assistant — {variante}</h1>
<p>{len(par_cas)} questions × {max(l['rep'] for l in lignes) + 1} passage(s) · réussite {total / len(lignes):.1%}
 · <span class="ok">vert</span> = réussie à chaque passage, <span class="mixte">orange</span> = instable, <span class="ko">rouge</span> = toujours en échec.
 Les verdicts du juge ne s'affichent que pour les critères non respectés.</p>
<p class="sommaire">{sommaire}</p>
{''.join(sections)}
</body></html>"""
    sortie = DOSSIER / f"reponses_{variante}.html"
    sortie.write_text(page, encoding="utf-8")
    print(f"page écrite : {sortie}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
