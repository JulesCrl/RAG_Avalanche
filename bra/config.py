"""Configuration lue dans le fichier .env (clés d'API, modèles Claude).

Les modèles se changent dans .env sans toucher au code, par exemple pour passer à
Sonnet pour la démo finale. Le nom daté fige la version, ce qui garde l'évaluation
reproductible.
"""
from __future__ import annotations

import os
from pathlib import Path

FICHIER_ENV = Path(__file__).resolve().parent.parent / ".env"
MODELE_PAR_DEFAUT = "claude-haiku-4-5-20251001"


def charger_env(fichier: Path = FICHIER_ENV) -> None:
    """Ajoute les variables du .env à l'environnement, sans écraser celles déjà définies."""
    if not fichier.exists():
        return
    for ligne in fichier.read_text().splitlines():
        if "=" in ligne and not ligne.lstrip().startswith("#"):
            cle, valeur = ligne.split("=", 1)
            os.environ.setdefault(cle.strip(), valeur.strip())


charger_env()

MODELE_CLASSIFICATION = os.environ.get("CLAUDE_MODEL_CLASSIFICATION") or os.environ.get("CLAUDE_MODEL") or MODELE_PAR_DEFAUT
MODELE_REPONSE = os.environ.get("CLAUDE_MODEL_REPONSE") or os.environ.get("CLAUDE_MODEL") or MODELE_PAR_DEFAUT
