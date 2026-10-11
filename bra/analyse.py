"""Résolution déterministe du lieu et de la date extraits d'une question.

Claude propose un lieu (texte libre) et une expression de date ; ce module les convertit
en massifs du référentiel et en date exacte. Tout ce qui n'est pas reconnu renvoie None
ou lève une erreur : le système demande alors une précision à l'utilisateur au lieu de deviner.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from datetime import date, timedelta

from .database import MASSIFS


# Mots retirés en tête du lieu : Claude recopie parfois la préposition (« aux Arcs », « en Vanoise »).
MOTS_DE_TETE = {"a", "au", "aux", "en", "dans", "de", "du", "des", "d", "sur", "vers", "pour",
                "l", "le", "la", "les", "station", "massif"}


def _normaliser(texte: str) -> str:
    """'à l'Alpe-d'Huez' -> 'alpe d huez' ; 'aux Arcs' et 'Les Arcs' -> 'arcs'."""
    ascii_ = unicodedata.normalize("NFKD", texte).encode("ascii", "ignore").decode().lower()
    mots = re.sub(r"[^a-z0-9]+", " ", ascii_).split()
    while len(mots) > 1 and mots[0] in MOTS_DE_TETE:
        mots.pop(0)
    return " ".join(mots)


# Surnoms et villages des stations du référentiel (liste tenue à la main, correspondance exacte).
SURNOMS = {
    "Les Arcs": ["Arc 1600", "Arc 1800", "Arc 1950", "Arc 2000", "Arcs 1600", "Arcs 1800", "Arcs 1950", "Arcs 2000"],
    "Val Thorens": ["Val Tho", "Valtho"],
    "Alpe d'Huez": ["Alpe", "Alpe Huez", "Huez"],
    "Vars": ["Vars les Claux", "Vars Sainte-Marie"],
    "Risoul": ["Risoul 1850"],
}


def _index_lieux() -> dict[str, list[str]]:
    """Nom normalisé (massif, station ou surnom) -> massifs concernés, dans l'ordre du référentiel."""
    index: dict[str, list[str]] = {}
    for id_massif, nom, _, stations in MASSIFS:
        surnoms = [s for station in stations for s in SURNOMS.get(station, [])]
        for libelle in (nom, id_massif, *stations, *surnoms):
            massifs = index.setdefault(_normaliser(libelle), [])
            if id_massif not in massifs:
                massifs.append(id_massif)
    return index


INDEX_LIEUX = _index_lieux()


@dataclass
class Lieu:
    libelle: str
    massifs: list[str]

    @property
    def comparaison(self) -> bool:
        """Station à cheval sur deux massifs (Vars, Risoul) : la réponse compare les deux bulletins."""
        return len(self.massifs) > 1


def resoudre_lieu(texte: str | None) -> Lieu | None:
    """Correspondance exacte après normalisation, sans approximation : un lieu inconnu renvoie None."""
    if not texte:
        return None
    massifs = INDEX_LIEUX.get(_normaliser(texte))
    return Lieu(texte, list(massifs)) if massifs else None


def resoudre_lieux(textes: list[str] | None) -> tuple[Lieu | None, str | None]:
    """Plusieurs lieux cités (« Risoul ou Vars ») : union de leurs massifs.

    Renvoie (lieu, None), ou (None, texte non reconnu) pour demander une précision.
    """
    if not textes:
        return None, None
    massifs: list[str] = []
    for texte in textes:
        lieu = resoudre_lieu(texte)
        if lieu is None:
            return None, texte
        massifs += [m for m in lieu.massifs if m not in massifs]
    return Lieu(" / ".join(textes), massifs), None


@dataclass
class ExpressionDate:
    """Date telle que formulée dans la question, sans calcul.

    - decalage_jours : relatif à aujourd'hui (0 = aujourd'hui, 1 = demain, -1 = hier) ;
    - jour / mois / annee : date explicite, l'année pouvant manquer (« le 12 février »).
    """

    decalage_jours: int | None = None
    jour: int | None = None
    mois: int | None = None
    annee: int | None = None


class DateInvalide(ValueError):
    """Expression de date impossible à convertir (31 février, mois manquant…)."""


def resoudre_date(expression: ExpressionDate | None, aujourd_hui: date, dernier_disponible: date | None) -> date | None:
    """Convertit l'expression en date de validité de bulletin.

    Sans date dans la question : le dernier bulletin disponible (usage quotidien de l'appli).
    « Demain » a un sens réel : le bulletin émis aujourd'hui à 16h est valable demain.
    Sans année : l'occurrence la plus récente qui ne dépasse pas demain, car aucun bulletin
    n'existe au-delà.
    """
    if expression is None or expression == ExpressionDate():
        return dernier_disponible
    if expression.decalage_jours is not None:
        return aujourd_hui + timedelta(days=expression.decalage_jours)
    if expression.jour is None or expression.mois is None:
        raise DateInvalide("jour ou mois manquant")
    limite = aujourd_hui + timedelta(days=1)
    annee = expression.annee if expression.annee is not None else limite.year
    try:
        resultat = date(annee, expression.mois, expression.jour)
        if expression.annee is None and resultat > limite:
            resultat = date(annee - 1, expression.mois, expression.jour)
    except ValueError as e:
        raise DateInvalide(str(e)) from e
    return resultat
