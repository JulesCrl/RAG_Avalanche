"""Base SQLite : schéma, référentiel des massifs et chargement idempotent des bulletins.

Relancer l'ingestion sur les mêmes fichiers ne crée aucun doublon : un bulletin
est identifié par (id_massif, date_bulletin), un point d'historique par
(id_massif, date), un chunk par id_chunk.
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from .parser import Bulletin

# Référentiel : périmètre du cadrage (stations fréquentées par le club).
# code_meteofrance = attribut ID du XML, utile pour interroger l'API par massif.
MASSIFS = [
    ("HAUTE_TARENTAISE", "Haute-Tarentaise", 6, ["Les Arcs"]),
    ("VANOISE", "Vanoise", 10, ["Val Thorens"]),
    ("GRANDES_ROUSSES", "Grandes-Rousses", 12, ["Alpe d'Huez"]),
    ("QUEYRAS", "Queyras", 17, ["Vars", "Risoul"]),
    ("EMBRUNAIS_PARPAILLON", "Embrunais Parpaillon", 20, ["Vars", "Risoul"]),
]

# Codes SAT1/SAT2 du XML (la doc XML renvoie à un « fichier de correspondance » séparé).
# Correspondance déduite de l'ordre du guide avalanche (p. 13) et du bulletin exemple du guide
# (Capcir 10/12/2024 : SAT1=2 neige ventée, SAT2=5 avalanches de fond), puis confirmée
# sur la source Météo-France le 28/09/2026.
SITUATIONS_TYPIQUES = [
    ("1", "neige fraîche"),
    ("2", "neige ventée"),
    ("3", "couche fragile persistante"),
    ("4", "neige humide"),
    ("5", "avalanches de fond"),
    ("6", "aucune situation avalancheuse typique prédominante"),
]

SCHEMA = """
CREATE TABLE IF NOT EXISTS massifs (
    id_massif          TEXT PRIMARY KEY,
    nom_affiche        TEXT NOT NULL,
    code_meteofrance   INTEGER NOT NULL UNIQUE,
    stations_associees TEXT NOT NULL  -- liste JSON
);

CREATE TABLE IF NOT EXISTS situations_typiques (
    code TEXT PRIMARY KEY,
    nom  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS bulletins (
    id_bulletin         INTEGER PRIMARY KEY AUTOINCREMENT,
    id_massif           TEXT NOT NULL REFERENCES massifs(id_massif),
    date_bulletin       TEXT NOT NULL,  -- ISO 8601, émission
    date_validite       TEXT NOT NULL,  -- ISO 8601, jour couvert par le bulletin
    risque_maxi         INTEGER NOT NULL CHECK (risque_maxi BETWEEN 1 AND 5),
    risque_maxi_j2      INTEGER CHECK (risque_maxi_j2 BETWEEN 1 AND 5),
    commentaire_risque  TEXT,
    situation_typique_1 TEXT REFERENCES situations_typiques(code),
    situation_typique_2 TEXT REFERENCES situations_typiques(code),
    commentaire_pentes  TEXT,  -- ex. "Altitude + crêtes"
    UNIQUE (id_massif, date_bulletin)
);

CREATE TABLE IF NOT EXISTS risques_par_altitude (
    id_bulletin    INTEGER NOT NULL REFERENCES bulletins(id_bulletin) ON DELETE CASCADE,
    niveau_risque  INTEGER NOT NULL CHECK (niveau_risque BETWEEN 1 AND 5),
    altitude_seuil INTEGER,
    zone           TEXT,
    evolution      INTEGER CHECK (evolution BETWEEN 1 AND 5)
);

-- Une ligne par orientation renseignée (8 au plus). Aucune ligne = balise PENTE absente.
CREATE TABLE IF NOT EXISTS pentes_exposees (
    id_bulletin INTEGER NOT NULL REFERENCES bulletins(id_bulletin) ON DELETE CASCADE,
    orientation TEXT NOT NULL CHECK (orientation IN ('N', 'NE', 'E', 'SE', 'S', 'SW', 'W', 'NW')),
    exposee     INTEGER NOT NULL CHECK (exposee IN (0, 1)),
    PRIMARY KEY (id_bulletin, orientation)
);

CREATE TABLE IF NOT EXISTS historique_risque_7j (
    id_massif   TEXT NOT NULL REFERENCES massifs(id_massif),
    date        TEXT NOT NULL,  -- YYYY-MM-DD
    risque_maxi INTEGER NOT NULL CHECK (risque_maxi BETWEEN 1 AND 5),
    PRIMARY KEY (id_massif, date)
);

-- Zone de transit des chunks : l'étape d'indexation (Chroma) les lira d'ici.
CREATE TABLE IF NOT EXISTS chunks_texte (
    id_chunk     TEXT PRIMARY KEY,
    id_bulletin  INTEGER NOT NULL REFERENCES bulletins(id_bulletin) ON DELETE CASCADE,
    type_contenu TEXT NOT NULL,
    texte        TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_bulletins_validite ON bulletins (id_massif, date_validite);
"""


def connecter(chemin: str | Path) -> sqlite3.Connection:
    Path(chemin).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(chemin)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def initialiser(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    conn.executemany(
        """INSERT INTO massifs VALUES (?, ?, ?, ?)
           ON CONFLICT (id_massif) DO UPDATE SET
               nom_affiche = excluded.nom_affiche,
               code_meteofrance = excluded.code_meteofrance,
               stations_associees = excluded.stations_associees""",
        [(i, nom, code, json.dumps(stations, ensure_ascii=False)) for i, nom, code, stations in MASSIFS],
    )
    conn.executemany(
        "INSERT INTO situations_typiques VALUES (?, ?) ON CONFLICT (code) DO UPDATE SET nom = excluded.nom",
        SITUATIONS_TYPIQUES,
    )
    conn.commit()


def verifier_massif(conn: sqlite3.Connection, bulletin: Bulletin) -> None:
    """Refuse un bulletin hors périmètre ou dont l'ID Météo-France ne correspond pas au nom."""
    ligne = conn.execute(
        "SELECT code_meteofrance FROM massifs WHERE id_massif = ?", (bulletin.id_massif,)
    ).fetchone()
    if ligne is None:
        raise ValueError(f"massif {bulletin.id_massif} hors du référentiel")
    if ligne["code_meteofrance"] != bulletin.code_meteofrance:
        raise ValueError(
            f"{bulletin.id_massif} : ID Météo-France {bulletin.code_meteofrance}, "
            f"attendu {ligne['code_meteofrance']}"
        )


def charger_bulletin(conn: sqlite3.Connection, bulletin: Bulletin) -> int:
    """Insère ou met à jour un bulletin (cas d'un amendement) et renvoie son id_bulletin."""
    verifier_massif(conn, bulletin)
    with conn:
        id_bulletin = conn.execute(
            """INSERT INTO bulletins (id_massif, date_bulletin, date_validite, risque_maxi,
                   risque_maxi_j2, commentaire_risque, situation_typique_1, situation_typique_2,
                   commentaire_pentes)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT (id_massif, date_bulletin) DO UPDATE SET
                   date_validite = excluded.date_validite,
                   risque_maxi = excluded.risque_maxi,
                   risque_maxi_j2 = excluded.risque_maxi_j2,
                   commentaire_risque = excluded.commentaire_risque,
                   situation_typique_1 = excluded.situation_typique_1,
                   situation_typique_2 = excluded.situation_typique_2,
                   commentaire_pentes = excluded.commentaire_pentes
               RETURNING id_bulletin""",
            (
                bulletin.id_massif,
                bulletin.date_bulletin.isoformat(),
                bulletin.date_validite.isoformat(),
                bulletin.risque_maxi,
                bulletin.risque_maxi_j2,
                bulletin.commentaire_risque,
                bulletin.situation_typique_1,
                bulletin.situation_typique_2,
                bulletin.commentaire_pentes,
            ),
        ).fetchone()[0]

        conn.execute("DELETE FROM risques_par_altitude WHERE id_bulletin = ?", (id_bulletin,))
        conn.executemany(
            "INSERT INTO risques_par_altitude VALUES (?, ?, ?, ?, ?)",
            [
                (id_bulletin, r.niveau_risque, r.altitude_seuil, r.zone, r.evolution)
                for r in bulletin.risques_par_altitude
            ],
        )

        conn.execute("DELETE FROM pentes_exposees WHERE id_bulletin = ?", (id_bulletin,))
        conn.executemany(
            "INSERT INTO pentes_exposees VALUES (?, ?, ?)",
            [(id_bulletin, o, int(exposee)) for o, exposee in bulletin.pentes_exposees.items()],
        )

        # Chaque bulletin republie 7 jours : la première valeur reçue pour un jour est conservée.
        conn.executemany(
            """INSERT INTO historique_risque_7j VALUES (?, ?, ?)
               ON CONFLICT (id_massif, date) DO NOTHING""",
            [(bulletin.id_massif, jour.isoformat(), niveau) for jour, niveau in bulletin.historique_7j],
        )

        conn.execute("DELETE FROM chunks_texte WHERE id_bulletin = ?", (id_bulletin,))
        conn.executemany(
            "INSERT INTO chunks_texte VALUES (?, ?, ?, ?)",
            [(bulletin.id_chunk(c), id_bulletin, c.type_contenu, c.texte) for c in bulletin.chunks],
        )
    return id_bulletin
