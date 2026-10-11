"""Graphique d'évolution du taux de réussite, version par version, pour le README.

Lit evals/assistant/historique.json et écrit evals/assistant/evolution.svg : un point par version, avec son
intervalle de confiance à 95 %. Le SVG suit le thème clair ou sombre du lecteur (GitHub compris).

Usage : uv run python evals/assistant/graphique.py
"""
from __future__ import annotations

import json
from pathlib import Path

DOSSIER = Path(__file__).resolve().parent
LARGEUR, HAUTEUR = 720, 360
MARGE_G, MARGE_D, MARGE_H, MARGE_B = 56, 24, 64, 56
Y_MIN, Y_MAX = 0.4, 1.0


def _y(valeur: float) -> float:
    return MARGE_H + (Y_MAX - valeur) / (Y_MAX - Y_MIN) * (HAUTEUR - MARGE_H - MARGE_B)


def generer(historique: list[dict]) -> str:
    pas = (LARGEUR - MARGE_G - MARGE_D) / len(historique)
    xs = [MARGE_G + pas * (i + 0.5) for i in range(len(historique))]
    p = [
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {LARGEUR} {HAUTEUR}" width="{LARGEUR}" '
        f'role="img" aria-labelledby="titre desc" font-family="-apple-system, Segoe UI, Helvetica, Arial, sans-serif">',
        '<title id="titre">Taux de réussite de l\'assistant par version</title>',
        '<desc id="desc">' + ", ".join(f"{h['version']} : {h['reussite']:.1%}" for h in historique) + "</desc>",
        "<style>",
        "  svg { --surface: #fcfcfb; --texte: #0b0b0b; --texte-2: #52514e; --grille: #e4e3df; --serie: #2a78d6; }",
        "  @media (prefers-color-scheme: dark) {",
        "    svg { --surface: #1a1a19; --texte: #f0efec; --texte-2: #c3c2b7; --grille: #3a3936; --serie: #3987e5; }",
        "  }",
        "  .fond { fill: var(--surface); } .titre { fill: var(--texte); font-size: 16px; font-weight: 600; }",
        "  .sous-titre, .axe { fill: var(--texte-2); font-size: 12px; } .valeur { fill: var(--texte); font-size: 13px; font-weight: 600; paint-order: stroke; stroke: var(--surface); stroke-width: 4px; stroke-linejoin: round; }",
        "  .grille { stroke: var(--grille); stroke-width: 1; } .ligne { stroke: var(--serie); stroke-width: 2; fill: none; }",
        "  .ic { stroke: var(--serie); stroke-width: 2; stroke-linecap: round; opacity: .45; }",
        "  .point { fill: var(--serie); stroke: var(--surface); stroke-width: 2; }",
        "</style>",
        f'<rect class="fond" width="{LARGEUR}" height="{HAUTEUR}" rx="8"/>',
        f'<text class="titre" x="{MARGE_G}" y="28">Taux de réussite de l\'assistant par version</text>',
        f'<text class="sous-titre" x="{MARGE_G}" y="47">Jeu de 48 questions, noté par un juge LLM · '
        f"barres : intervalle de confiance à 95 %</text>",
    ]
    for v in (0.4, 0.6, 0.8, 1.0):
        y = _y(v)
        p.append(f'<line class="grille" x1="{MARGE_G}" x2="{LARGEUR - MARGE_D}" y1="{y:.1f}" y2="{y:.1f}"/>')
        p.append(f'<text class="axe" x="{MARGE_G - 8}" y="{y + 4:.1f}" text-anchor="end">{v:.0%}</text>')
    points = " ".join(f"{x:.1f},{_y(h['reussite']):.1f}" for x, h in zip(xs, historique))
    p.append(f'<polyline class="ligne" points="{points}"/>')
    for x, h in zip(xs, historique):
        bas, haut = h["ic95"]
        y = _y(h["reussite"])
        p.append(f'<line class="ic" x1="{x:.1f}" x2="{x:.1f}" y1="{_y(haut):.1f}" y2="{_y(bas):.1f}"/>')
        p.append(f'<circle class="point" cx="{x:.1f}" cy="{y:.1f}" r="5"/>')
        libelle = f"{h['reussite'] * 100:.1f} %".replace(".", ",")
        p.append(f'<text class="valeur" x="{x - 9:.1f}" y="{y - 10:.1f}" text-anchor="end">{libelle}</text>')
        passages = f"{h['passages']} passage{'s' if h['passages'] > 1 else ''}"
        p.append(f'<text class="axe" x="{x:.1f}" y="{HAUTEUR - MARGE_B + 22}" text-anchor="middle">{h["version"]}</text>')
        p.append(f'<text class="axe" x="{x:.1f}" y="{HAUTEUR - MARGE_B + 38}" text-anchor="middle">{passages}</text>')
    p.append("</svg>")
    return "\n".join(p) + "\n"


def main() -> None:
    historique = json.loads((DOSSIER / "historique.json").read_text())
    (DOSSIER / "evolution.svg").write_text(generer(historique))
    print(f"écrit : {DOSSIER / 'evolution.svg'}")


if __name__ == "__main__":
    main()
