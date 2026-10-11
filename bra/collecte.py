"""Collecte des XML BRA pour les massifs du référentiel.

Deux sources :
- archive : l'archive data.gouv (un dossier par jour, depuis 2016), pour l'historique ;
- api     : l'API DonneesPubliquesBRA, pour le bulletin du jour (saison novembre-mai).

Les fichiers sont enregistrés dans le dossier de sortie sous la forme
<Massif>_<AAAAMMJJhhmmss>.xml, puis chargés en base par bra.ingest.

Usage :
    uv run python -m bra.collecte archive --debut 2025-11-01 --fin 2026-06-30
    uv run python -m bra.collecte api
"""
from __future__ import annotations

import argparse
import os
import re
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import date, datetime, timedelta
from pathlib import Path

import certifi

from .database import MASSIFS
from .parser import normaliser_id_massif

URL_ARCHIVE = "https://files.data.gouv.fr/meteofrance/data/BULLETIN/BRA"
URL_API = "https://public-api.meteofrance.fr/public/DPBRA/v1"
DOSSIER_DEFAUT = Path("BRA_DB")
PAUSE_SECONDES = 0.3  # politesse envers les serveurs ; l'API est limitée à 50 appels/minute

# Certificats de certifi : le Python de python.org sur macOS n'a pas accès aux certificats du système.
CONTEXTE_SSL = ssl.create_default_context(cafile=certifi.where())

IDS_MASSIFS = {id_massif for id_massif, _, _, _ in MASSIFS}


def _telecharger(url: str, entetes: dict | None = None) -> bytes | None:
    """Renvoie le contenu, ou None si la ressource n'existe pas (404)."""
    requete = urllib.request.Request(url, headers={"User-Agent": "rag-ski-bra", **(entetes or {})})
    try:
        with urllib.request.urlopen(requete, timeout=30, context=CONTEXTE_SSL) as reponse:
            return reponse.read()
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None
        raise


def _enregistrer(contenu: bytes, chemin: Path) -> bool:
    """Écrit le fichier s'il n'existe pas déjà. Renvoie True si un nouveau fichier a été écrit."""
    if chemin.exists():
        return False
    chemin.parent.mkdir(parents=True, exist_ok=True)
    chemin.write_bytes(contenu)
    return True


# --- Archive data.gouv -----------------------------------------------------

def fichiers_du_jour(jour: date) -> list[str]:
    """Noms des XML publiés ce jour-là dans l'archive (tous massifs confondus)."""
    page = _telecharger(f"{URL_ARCHIVE}/{jour:%Y/%m/%d}/xml/")
    if page is None:
        return []
    liens = re.findall(r'href="([^"]+\.xml)"', page.decode("utf-8", "replace"))
    return [urllib.parse.unquote(lien.rsplit("/", 1)[-1]) for lien in liens]


def massif_du_fichier(nom_fichier: str) -> str:
    """'Embrunais Parpaillon_20260207161000.xml' ou 'EMBRUNAIS-PARPAILLON_2016...' -> 'EMBRUNAIS_PARPAILLON'."""
    return normaliser_id_massif(nom_fichier.rsplit("_", 1)[0])


def collecter_archive(debut: date, fin: date, dossier: Path) -> int:
    nouveaux = 0
    jour = debut
    while jour <= fin:
        a_telecharger = [f for f in fichiers_du_jour(jour) if massif_du_fichier(f) in IDS_MASSIFS]
        for nom in a_telecharger:
            chemin = dossier / nom
            if chemin.exists():
                continue
            contenu = _telecharger(f"{URL_ARCHIVE}/{jour:%Y/%m/%d}/xml/{urllib.parse.quote(nom)}")
            if contenu is not None and _enregistrer(contenu, chemin):
                nouveaux += 1
            time.sleep(PAUSE_SECONDES)
        print(f"{jour} : {len(a_telecharger)} bulletin(s) dans l'archive", file=sys.stderr)
        jour += timedelta(days=1)
    return nouveaux


# --- API Météo-France ------------------------------------------------------

def lire_cle_api(fichier_env: Path = Path(".env")) -> str:
    cle = os.environ.get("METEOFRANCE_BRA_KEY")
    if not cle and fichier_env.exists():
        for ligne in fichier_env.read_text().splitlines():
            if ligne.startswith("METEOFRANCE_BRA_KEY="):
                cle = ligne.split("=", 1)[1].strip()
    if not cle:
        raise SystemExit("Clé absente : définir METEOFRANCE_BRA_KEY dans .env")
    return cle


def collecter_api(dossier: Path) -> int:
    cle = lire_cle_api()
    nouveaux = 0
    for _, nom_affiche, code, _ in MASSIFS:
        contenu = _telecharger(
            f"{URL_API}/massif/BRA?id-massif={code}&format=xml", {"apikey": cle}
        )
        time.sleep(PAUSE_SECONDES)
        if contenu is None:
            print(f"{nom_affiche} : aucun bulletin", file=sys.stderr)
            continue
        racine = ET.fromstring(contenu)
        if racine.tag != "BULLETINS_NEIGE_AVALANCHE":
            # Hors saison, l'API renvoie <message>...</message> au lieu d'un bulletin.
            print(f"{nom_affiche} : {(racine.text or '').strip().splitlines()[0]}", file=sys.stderr)
            continue
        horodatage = datetime.fromisoformat(racine.get("DATEDIFFUSION")).strftime("%Y%m%d%H%M%S")
        if _enregistrer(contenu, dossier / f"{nom_affiche}_{horodatage}.xml"):
            nouveaux += 1
    return nouveaux


def main() -> int:
    parseur = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sous = parseur.add_subparsers(dest="source", required=True)
    archive = sous.add_parser("archive", help="historique depuis data.gouv")
    archive.add_argument("--debut", type=date.fromisoformat, required=True)
    archive.add_argument("--fin", type=date.fromisoformat, default=date.today())
    sous.add_parser("api", help="bulletin du jour via l'API Météo-France")
    parseur.add_argument("--dossier", type=Path, default=DOSSIER_DEFAUT)
    ns = parseur.parse_args()

    if ns.source == "archive":
        nouveaux = collecter_archive(ns.debut, ns.fin, ns.dossier)
    else:
        nouveaux = collecter_api(ns.dossier)
    print(f"{nouveaux} nouveau(x) fichier(s) dans {ns.dossier}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
