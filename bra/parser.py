"""Parsing d'un bulletin BRA (XML Météo-France) en structures Python.

Règle du projet : aucune valeur n'est devinée ni générée. Tout vient des
attributs et balises du XML ; si une donnée critique manque, on lève une
erreur plutôt que d'inventer une valeur par défaut.
"""
from __future__ import annotations

import re
import unicodedata
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path

# Sections textuelles transformées en chunks (un chunk par section thématique).
SECTIONS_TEXTE = {
    "stabilite": "STABILITE/TEXTE",
    "qualite_neige": "QUALITE/TEXTE",
    "resume": "CARTOUCHERISQUE/RESUME",
    "tendance": "CARTOUCHERISQUE/CommentaireRisqueJ2",
}

# Codes d'orientation tels qu'écrits dans la balise PENTE (notation anglaise : W = ouest).
ORIENTATIONS = ("N", "NE", "E", "SE", "S", "SW", "W", "NW")

# Variantes de noms rencontrées dans les données réelles. Le chargement vérifie en plus
# l'ID Météo-France, donc un alias ne peut pas rattacher un bulletin au mauvais massif.
ALIAS_MASSIFS = {
    "EMBRUNNAIS_PARPAILLON": "EMBRUNAIS_PARPAILLON",  # faute de frappe Météo-France jusqu'au 07/01/2026
}


class BulletinInvalide(ValueError):
    """Le XML ne contient pas une donnée indispensable ou contient une valeur incohérente."""


@dataclass
class RisqueAltitude:
    niveau_risque: int
    altitude_seuil: int | None
    zone: str | None
    evolution: int | None


@dataclass
class Chunk:
    type_contenu: str
    texte: str


@dataclass
class Bulletin:
    id_massif: str
    nom_massif: str
    code_meteofrance: int
    date_bulletin: datetime
    date_validite: datetime
    risque_maxi: int
    risque_maxi_j2: int | None
    commentaire_risque: str | None
    situation_typique_1: str | None
    situation_typique_2: str | None
    commentaire_pentes: str | None = None
    pentes_exposees: dict[str, bool] = field(default_factory=dict)
    risques_par_altitude: list[RisqueAltitude] = field(default_factory=list)
    historique_7j: list[tuple[date, int]] = field(default_factory=list)
    chunks: list[Chunk] = field(default_factory=list)

    def id_chunk(self, chunk: Chunk) -> str:
        # L'heure est nécessaire : lors d'un épisode exceptionnel, une mise à jour peut être émise le matin.
        return f"{self.id_massif}_{self.date_bulletin:%Y%m%d%H%M}_{chunk.type_contenu}"


def normaliser_id_massif(nom: str) -> str:
    """'Embrunais Parpaillon' -> 'EMBRUNAIS_PARPAILLON'."""
    sans_accents = unicodedata.normalize("NFKD", nom).encode("ascii", "ignore").decode()
    id_massif = re.sub(r"[^A-Z0-9]+", "_", sans_accents.upper()).strip("_")
    return ALIAS_MASSIFS.get(id_massif, id_massif)


def _entier(valeur: str | None) -> int | None:
    if valeur is None or valeur.strip() == "":
        return None
    return int(valeur)


def _niveau(valeur: str | None, champ: str) -> int | None:
    """Niveau de risque 1-5. Météo-France écrit -1 pour « non renseigné »."""
    niveau = _entier(valeur)
    if niveau == -1:
        return None
    if niveau is not None and not 1 <= niveau <= 5:
        raise BulletinInvalide(f"{champ}={niveau} hors de l'échelle 1-5")
    return niveau


def _texte(racine: ET.Element, chemin: str) -> str | None:
    element = racine.find(chemin)
    if element is None or element.text is None:
        return None
    return element.text.strip() or None


def _parser_risques_altitude(risque: ET.Element) -> list[RisqueAltitude]:
    """Le cartouche est polymorphe : niveau unique, ou split par altitude (RISQUE1/RISQUE2)."""
    altitude = _entier(risque.get("ALTITUDE"))
    zones = []
    for i in (1, 2):
        niveau = _niveau(risque.get(f"RISQUE{i}"), f"RISQUE{i}")
        if niveau is None:
            continue
        zone = (risque.get(f"LOC{i}") or "").strip() or None
        zones.append(
            RisqueAltitude(
                niveau_risque=niveau,
                altitude_seuil=altitude if zone else None,
                zone=zone,
                evolution=_niveau(risque.get(f"EVOLURISQUE{i}"), f"EVOLURISQUE{i}"),
            )
        )
    return zones


def _parser_pentes(pente: ET.Element | None, chemin) -> dict[str, bool]:
    """Orientations les plus exposées. Un attribut absent n'est pas interprété comme 'non exposé'."""
    if pente is None:
        return {}
    pentes = {}
    for orientation in ORIENTATIONS:
        valeur = pente.get(orientation)
        if valeur is None or valeur == "":
            continue
        if valeur not in ("true", "false"):
            raise BulletinInvalide(f"{chemin} : PENTE {orientation}={valeur!r} inattendu")
        pentes[orientation] = valeur == "true"
    return pentes


def parser_bulletin(chemin: str | Path) -> Bulletin:
    racine = ET.parse(chemin).getroot()
    if racine.tag != "BULLETINS_NEIGE_AVALANCHE":
        raise BulletinInvalide(f"{chemin} : racine inattendue <{racine.tag}>")

    nom_massif = racine.get("MASSIF")
    if not nom_massif:
        raise BulletinInvalide(f"{chemin} : attribut MASSIF absent")

    risque = racine.find("CARTOUCHERISQUE/RISQUE")
    if risque is None:
        raise BulletinInvalide(f"{chemin} : cartouche de risque absent")

    risque_maxi = _niveau(risque.get("RISQUEMAXI"), "RISQUEMAXI")
    if risque_maxi is None:
        raise BulletinInvalide(f"{chemin} : RISQUEMAXI absent")

    risques_altitude = _parser_risques_altitude(risque)
    for zone in risques_altitude:
        if max(zone.niveau_risque, zone.evolution or 0) > risque_maxi:
            raise BulletinInvalide(
                f"{chemin} : niveau {zone.niveau_risque} ({zone.zone}) supérieur à RISQUEMAXI={risque_maxi}"
            )

    pente = racine.find("CARTOUCHERISQUE/PENTE")
    sat = racine.find("STABILITE/SitAvalTyp")
    historique = [
        (datetime.fromisoformat(r.get("DATE")).date(), _niveau(r.get("RISQUEMAXI"), "BSH/RISQUEMAXI"))
        for r in racine.findall("BSH/RISQUES/RISQUE")
        if _entier(r.get("RISQUEMAXI")) is not None
    ]

    chunks = [
        Chunk(type_contenu, texte)
        for type_contenu, chemin_xml in SECTIONS_TEXTE.items()
        if (texte := _texte(racine, chemin_xml))
    ]

    return Bulletin(
        id_massif=normaliser_id_massif(nom_massif),
        nom_massif=nom_massif,
        code_meteofrance=int(racine.get("ID")),
        date_bulletin=datetime.fromisoformat(racine.get("DATEBULLETIN")),
        date_validite=datetime.fromisoformat(racine.get("DATEVALIDITE")),
        risque_maxi=risque_maxi,
        risque_maxi_j2=_niveau(risque.get("RISQUEMAXIJ2"), "RISQUEMAXIJ2"),
        commentaire_risque=(risque.get("COMMENTAIRE") or "").strip() or None,
        situation_typique_1=(sat.get("SAT1") or None) if sat is not None else None,
        situation_typique_2=(sat.get("SAT2") or None) if sat is not None else None,
        commentaire_pentes=((pente.get("COMMENTAIRE") or "").strip() or None) if pente is not None else None,
        pentes_exposees=_parser_pentes(pente, chemin),
        risques_par_altitude=risques_altitude,
        historique_7j=historique,
        chunks=chunks,
    )
