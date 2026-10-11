"""Requêtes SQL déterministes pour les questions factuelles (niveau d'un jour, évolution).

Toutes les fonctions prennent une date explicite : l'utilisateur peut interroger
n'importe quel jour archivé, pas seulement le dernier bulletin.

Convention : « le risque du jour J » = le bulletin dont la date de validité tombe
le jour J (il est émis la veille vers 16h).
"""
from __future__ import annotations

import sqlite3
from datetime import date, timedelta


def bulletin_du_jour(conn: sqlite3.Connection, id_massif: str, jour: date) -> dict | None:
    """Renvoie le bulletin valable le jour donné, ou None s'il n'y en a pas (jamais d'approximation)."""
    ligne = conn.execute(
        """SELECT b.*, m.nom_affiche, s1.nom AS situation_typique_1_nom, s2.nom AS situation_typique_2_nom
           FROM bulletins b JOIN massifs m USING (id_massif)
           LEFT JOIN situations_typiques s1 ON s1.code = b.situation_typique_1
           LEFT JOIN situations_typiques s2 ON s2.code = b.situation_typique_2
           WHERE b.id_massif = ? AND date(b.date_validite) = ?
           ORDER BY b.date_bulletin DESC LIMIT 1""",
        (id_massif, jour.isoformat()),
    ).fetchone()
    if ligne is None:
        return None
    bulletin = dict(ligne)
    bulletin["risques_par_altitude"] = [
        dict(r)
        for r in conn.execute(
            """SELECT niveau_risque, altitude_seuil, zone, evolution
               FROM risques_par_altitude WHERE id_bulletin = ? ORDER BY niveau_risque""",
            (bulletin["id_bulletin"],),
        )
    ]
    pentes = conn.execute(
        "SELECT orientation, exposee FROM pentes_exposees WHERE id_bulletin = ?", (bulletin["id_bulletin"],)
    ).fetchall()
    bulletin["pentes_exposees"] = [r["orientation"] for r in pentes if r["exposee"]]
    # Distingue « aucune pente signalée » (balise présente, tout à false) de « balise absente ».
    bulletin["pentes_renseignees"] = bool(pentes)
    return bulletin


def evolution_risque(
    conn: sqlite3.Connection, id_massif: str, fin: date, nb_jours: int = 7
) -> list[tuple[date, int]]:
    """Série du risque maxi sur les nb_jours précédant `fin` (inclus). Les jours sans donnée sont absents."""
    debut = fin - timedelta(days=nb_jours - 1)
    return [
        (date.fromisoformat(r["date"]), r["risque_maxi"])
        for r in conn.execute(
            """SELECT date, risque_maxi FROM historique_risque_7j
               WHERE id_massif = ? AND date BETWEEN ? AND ? ORDER BY date""",
            (id_massif, debut.isoformat(), fin.isoformat()),
        )
    ]


def dernier_jour_disponible(conn: sqlite3.Connection, id_massif: str, jusqu_au: date | None = None) -> date | None:
    """Jour du bulletin le plus récent en base : date par défaut quand la question n'en précise pas.

    `jusqu_au` ignore les bulletins valables après ce jour : indispensable pour simuler une date
    passée (évaluation), sans effet en usage réel où la base ne contient rien de plus récent.
    """
    ligne = conn.execute(
        "SELECT MAX(date(date_validite)) FROM bulletins WHERE id_massif = ? AND (? IS NULL OR date(date_validite) <= ?)",
        (id_massif, jusqu_au and jusqu_au.isoformat(), jusqu_au and jusqu_au.isoformat()),
    ).fetchone()
    return date.fromisoformat(ligne[0]) if ligne[0] else None


def jours_disponibles(conn: sqlite3.Connection, id_massif: str) -> list[date]:
    """Jours pour lesquels un bulletin est disponible, pour proposer un choix de date à l'utilisateur."""
    return [
        date.fromisoformat(r[0])
        for r in conn.execute(
            "SELECT DISTINCT date(date_validite) FROM bulletins WHERE id_massif = ? ORDER BY 1",
            (id_massif,),
        )
    ]
