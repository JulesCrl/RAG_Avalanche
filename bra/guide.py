"""Découpage du guide avalanche Météo-France (transcription Markdown) en sections.

Chaque titre « ## Titre | page N » ouvre une section, qui devient un chunk de la
collection Chroma du guide. Le numéro de page sert à citer la source.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path

CHEMIN_GUIDE = Path("sources/guide-avalanche-2025.md")
SOURCE = "Guide avalanche Météo-France 2025"
TITRE = re.compile(r"^## (?P<titre>.+?) \| page (?P<page>\d+)\s*$")


@dataclass
class Section:
    titre: str
    page: int
    texte: str

    @property
    def id_section(self) -> str:
        ascii_ = unicodedata.normalize("NFKD", self.titre).encode("ascii", "ignore").decode().lower()
        return "guide_" + re.sub(r"[^a-z0-9]+", "_", ascii_).strip("_")


def lire_sections(chemin: str | Path = CHEMIN_GUIDE) -> list[Section]:
    """Sections du guide. Le guide n'est pas versionné (© Météo-France) : absent, la liste est vide et
    l'assistant répond sans ses définitions."""
    if not Path(chemin).exists():
        return []
    contenu = re.sub(r"<!--.*?-->", "", Path(chemin).read_text(encoding="utf-8"), flags=re.S)
    sections, courante, lignes = [], None, []
    for ligne in contenu.splitlines():
        if m := TITRE.match(ligne):
            if courante:
                sections.append(Section(*courante, "\n".join(lignes).strip()))
            courante, lignes = (m["titre"], int(m["page"])), []
        elif ligne.startswith("## "):
            raise ValueError(f"titre sans numéro de page : {ligne!r}")
        elif courante:
            lignes.append(ligne)
    if courante:
        sections.append(Section(*courante, "\n".join(lignes).strip()))
    identifiants = [s.id_section for s in sections]
    if len(set(identifiants)) != len(identifiants):
        raise ValueError("deux sections du guide ont le même titre")
    return sections
