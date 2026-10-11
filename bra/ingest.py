"""Ingestion d'un dossier de XML BRA dans la base SQLite.

Usage : python -m bra.ingest BRA_DB --db data/bra.sqlite
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

from .database import charger_bulletin, connecter, initialiser
from .parser import BulletinInvalide, parser_bulletin


def ingerer_dossier(dossier: Path, chemin_db: Path) -> tuple[int, list[str]]:
    conn = connecter(chemin_db)
    initialiser(conn)
    charges, erreurs = 0, []
    for fichier in sorted(dossier.glob("*.xml")):
        try:
            charger_bulletin(conn, parser_bulletin(fichier))
            charges += 1
        except (BulletinInvalide, ValueError, sqlite3.Error) as e:
            erreurs.append(f"{fichier.name} : {e}")
    conn.close()
    return charges, erreurs


def main() -> int:
    args = argparse.ArgumentParser(description=__doc__)
    args.add_argument("dossier", type=Path)
    args.add_argument("--db", type=Path, default=Path("data/bra.sqlite"))
    ns = args.parse_args()

    charges, erreurs = ingerer_dossier(ns.dossier, ns.db)
    print(f"{charges} bulletin(s) chargé(s) dans {ns.db}")
    for erreur in erreurs:
        print(f"  REJETÉ {erreur}", file=sys.stderr)
    return 1 if erreurs else 0


if __name__ == "__main__":
    sys.exit(main())
