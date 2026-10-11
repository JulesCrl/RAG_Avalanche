import shutil
from datetime import date, datetime
from pathlib import Path

import pytest

from bra.database import charger_bulletin, connecter, initialiser
from bra.ingest import ingerer_dossier
from bra.parser import BulletinInvalide, normaliser_id_massif, parser_bulletin
from bra.requetes import bulletin_du_jour, dernier_jour_disponible, evolution_risque, jours_disponibles

# Jeu de référence : les 5 bulletins émis le 07/02/2026 (BRA_DB peut en contenir d'autres).
DOSSIER_XML = Path(__file__).parent.parent / "BRA_DB"
FICHIERS = sorted(DOSSIER_XML.glob("*_20260207*.xml"))
QUEYRAS = DOSSIER_XML / "Queyras_20260207161000.xml"

pytestmark = pytest.mark.skipif(not FICHIERS, reason="aucun XML dans BRA_DB/")


def xml_synthetique(tmp_path, attributs_risque: str) -> Path:
    """Copie du bulletin Queyras avec un cartouche de risque remplacé."""
    contenu = QUEYRAS.read_text(encoding="utf-8")
    debut = contenu.index("<RISQUE RISQUE1")
    fin = contenu.index("/>", debut) + 2
    fichier = tmp_path / "synthetique.xml"
    fichier.write_text(contenu[:debut] + f"<RISQUE {attributs_risque}/>" + contenu[fin:], encoding="utf-8")
    return fichier


# --- Parsing ---------------------------------------------------------------

@pytest.mark.parametrize("fichier", FICHIERS, ids=lambda f: f.stem)
def test_tous_les_bulletins_se_parsent(fichier):
    b = parser_bulletin(fichier)
    assert 1 <= b.risque_maxi <= 5
    assert b.date_validite > b.date_bulletin
    assert len(b.risques_par_altitude) in (0, 1, 2)
    assert len(b.historique_7j) == 7
    assert {c.type_contenu for c in b.chunks} == {"stabilite", "qualite_neige", "resume", "tendance"}
    assert set(b.pentes_exposees) == {"N", "NE", "E", "SE", "S", "SW", "W", "NW"}


def test_valeurs_exactes_queyras():
    b = parser_bulletin(QUEYRAS)
    assert b.id_massif == "QUEYRAS"
    assert b.code_meteofrance == 17
    assert b.date_bulletin == datetime(2026, 2, 7, 16, 0)
    assert b.date_validite == datetime(2026, 2, 8, 18, 0)
    assert (b.risque_maxi, b.risque_maxi_j2) == (3, 3)
    assert (b.situation_typique_1, b.situation_typique_2) == ("3", "1")
    assert [(r.zone, r.niveau_risque, r.altitude_seuil) for r in b.risques_par_altitude] == [
        ("<2000", 2, 2000),
        (">2000", 3, 2000),
    ]
    assert [n for _, n in b.historique_7j] == [3, 3, 4, 4, 3, 4, 4]
    assert b.id_chunk(b.chunks[0]) == "QUEYRAS_202602071600_stabilite"
    tendance = next(c for c in b.chunks if c.type_contenu == "tendance")
    assert tendance.texte.startswith("Poursuite de la lente stabilisation")
    assert [o for o, exposee in b.pentes_exposees.items() if exposee] == ["N", "NE", "E", "W", "NW"]
    assert b.commentaire_pentes is None


def test_commentaire_pentes():
    b = parser_bulletin(DOSSIER_XML / "Grandes-Rousses_20260207160000.xml")
    assert b.commentaire_pentes == "Altitude + crêtes"


def test_pente_valeur_inattendue_rejetee(tmp_path):
    contenu = QUEYRAS.read_text(encoding="utf-8").replace('<PENTE NE="true"', '<PENTE NE="oui"')
    fichier = tmp_path / "pente.xml"
    fichier.write_text(contenu, encoding="utf-8")
    with pytest.raises(BulletinInvalide, match="PENTE NE='oui'"):
        parser_bulletin(fichier)


def test_pente_absente_aucune_orientation(tmp_path):
    contenu = QUEYRAS.read_text(encoding="utf-8")
    debut = contenu.index("<PENTE ")
    fichier = tmp_path / "sans_pente.xml"
    fichier.write_text(contenu[:debut] + contenu[contenu.index("/>", debut) + 2:], encoding="utf-8")
    b = parser_bulletin(fichier)
    assert b.pentes_exposees == {}
    assert b.commentaire_pentes is None


def test_cartouche_niveau_unique(tmp_path):
    fichier = xml_synthetique(
        tmp_path, 'RISQUE1="2" EVOLURISQUE1="" LOC1="" ALTITUDE="" RISQUE2="" EVOLURISQUE2="" '
        'LOC2="" RISQUEMAXI="2" COMMENTAIRE="" RISQUEMAXIJ2="" DATE_RISQUE_J2=""'
    )
    b = parser_bulletin(fichier)
    assert b.risque_maxi == 2
    assert b.risque_maxi_j2 is None
    assert [(r.zone, r.niveau_risque, r.altitude_seuil) for r in b.risques_par_altitude] == [(None, 2, None)]


def test_moins_un_signifie_non_renseigne(tmp_path):
    # Cas réel : Queyras, bulletin du 04/12/2025.
    b = parser_bulletin(xml_synthetique(tmp_path, 'RISQUE1="-1" LOC1="" RISQUE2="" RISQUEMAXI="2" RISQUEMAXIJ2=""'))
    assert b.risque_maxi == 2
    assert b.risques_par_altitude == []


def test_risquemaxi_moins_un_rejete(tmp_path):
    with pytest.raises(BulletinInvalide, match="RISQUEMAXI absent"):
        parser_bulletin(xml_synthetique(tmp_path, 'RISQUE1="2" RISQUEMAXI="-1"'))


def test_deux_bulletins_le_meme_jour(tmp_path):
    # Cas réel : mise à jour du matin lors d'un épisode exceptionnel (Grandes-Rousses, 12/02/2026 à 10h).
    conn = connecter(tmp_path / "bra.sqlite")
    initialiser(conn)
    soir = parser_bulletin(QUEYRAS)
    matin = parser_bulletin(QUEYRAS)
    matin.date_bulletin = datetime(2026, 2, 7, 10, 0)
    matin.date_validite = datetime(2026, 2, 7, 18, 0)
    matin.risque_maxi = 4
    charger_bulletin(conn, soir)
    charger_bulletin(conn, matin)
    assert compter(conn, "bulletins") == 2
    assert compter(conn, "chunks_texte") == 2 * len(soir.chunks)
    assert bulletin_du_jour(conn, "QUEYRAS", date(2026, 2, 7))["risque_maxi"] == 4


def test_risquemaxi_absent_rejete(tmp_path):
    with pytest.raises(BulletinInvalide, match="RISQUEMAXI absent"):
        parser_bulletin(xml_synthetique(tmp_path, 'RISQUE1="2" RISQUEMAXI=""'))


def test_niveau_hors_echelle_rejete(tmp_path):
    with pytest.raises(BulletinInvalide, match="hors de l'échelle"):
        parser_bulletin(xml_synthetique(tmp_path, 'RISQUE1="2" RISQUEMAXI="6"'))


def test_niveau_altitude_superieur_au_maxi_rejete(tmp_path):
    with pytest.raises(BulletinInvalide, match="supérieur à RISQUEMAXI"):
        parser_bulletin(xml_synthetique(tmp_path, 'RISQUE1="4" LOC1="&gt;2000" ALTITUDE="2000" RISQUEMAXI="3"'))


def test_normalisation_id_massif():
    assert normaliser_id_massif("Embrunais Parpaillon") == "EMBRUNAIS_PARPAILLON"
    assert normaliser_id_massif("Grandes-Rousses") == "GRANDES_ROUSSES"
    assert normaliser_id_massif("Embrunnais Parpaillon") == "EMBRUNAIS_PARPAILLON"


# --- Base de données -------------------------------------------------------

@pytest.fixture
def dossier_reference(tmp_path):
    dossier = tmp_path / "xml"
    dossier.mkdir()
    for fichier in FICHIERS:
        shutil.copy(fichier, dossier)
    return dossier


@pytest.fixture
def db(tmp_path, dossier_reference):
    charges, erreurs = ingerer_dossier(dossier_reference, tmp_path / "bra.sqlite")
    assert erreurs == []
    assert charges == len(FICHIERS)
    conn = connecter(tmp_path / "bra.sqlite")
    yield conn
    conn.close()


def compter(conn, table):
    return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]


def test_ingestion_idempotente(db, tmp_path, dossier_reference):
    avant = {t: compter(db, t) for t in ("bulletins", "risques_par_altitude", "pentes_exposees", "historique_risque_7j", "chunks_texte")}
    ingerer_dossier(dossier_reference, tmp_path / "bra.sqlite")
    apres = {t: compter(db, t) for t in avant}
    assert avant == apres
    assert avant["bulletins"] == len(FICHIERS)
    assert avant["historique_risque_7j"] == 7 * len(FICHIERS)
    assert avant["chunks_texte"] == 4 * len(FICHIERS)
    assert avant["pentes_exposees"] == 8 * len(FICHIERS)


def test_chunks_resume_identique_restent_distincts_par_massif(db):
    # Queyras et Embrunais partagent le même résumé ce jour-là : le massif doit rester traçable.
    lignes = db.execute(
        """SELECT b.id_massif FROM chunks_texte c JOIN bulletins b USING (id_bulletin)
           WHERE c.type_contenu = 'resume' AND b.id_massif IN ('QUEYRAS', 'EMBRUNAIS_PARPAILLON')"""
    ).fetchall()
    assert sorted(r[0] for r in lignes) == ["EMBRUNAIS_PARPAILLON", "QUEYRAS"]


def test_massif_hors_referentiel_rejete(tmp_path):
    conn = connecter(tmp_path / "bra.sqlite")
    initialiser(conn)
    b = parser_bulletin(QUEYRAS)
    b.id_massif = "CHABLAIS"
    with pytest.raises(ValueError, match="hors du référentiel"):
        charger_bulletin(conn, b)


def test_code_meteofrance_incoherent_rejete(tmp_path):
    conn = connecter(tmp_path / "bra.sqlite")
    initialiser(conn)
    b = parser_bulletin(QUEYRAS)
    b.code_meteofrance = 20
    with pytest.raises(ValueError, match="attendu 17"):
        charger_bulletin(conn, b)


# --- Requêtes par date -----------------------------------------------------

def test_bulletin_du_jour_date_passee(db):
    b = bulletin_du_jour(db, "QUEYRAS", date(2026, 2, 8))
    assert b["risque_maxi"] == 3
    assert b["nom_affiche"] == "Queyras"
    assert b["date_bulletin"] == "2026-02-07T16:00:00"
    assert [r["niveau_risque"] for r in b["risques_par_altitude"]] == [2, 3]
    assert sorted(b["pentes_exposees"]) == ["E", "N", "NE", "NW", "W"]
    assert (b["situation_typique_1_nom"], b["situation_typique_2_nom"]) == ("couche fragile persistante", "neige fraîche")


def test_bulletin_absent_renvoie_none(db):
    assert bulletin_du_jour(db, "QUEYRAS", date(2026, 2, 7)) is None
    assert bulletin_du_jour(db, "QUEYRAS", date(2025, 12, 25)) is None


def test_evolution_semaine(db):
    serie = evolution_risque(db, "QUEYRAS", date(2026, 2, 7))
    assert [d.day for d, _ in serie] == [1, 2, 3, 4, 5, 6, 7]
    assert [n for _, n in serie] == [3, 3, 4, 4, 3, 4, 4]


def test_dernier_jour_disponible(db):
    assert dernier_jour_disponible(db, "QUEYRAS") == date(2026, 2, 8)
    assert dernier_jour_disponible(db, "MASSIF_INCONNU") is None


def test_jours_disponibles(db):
    assert jours_disponibles(db, "VANOISE") == [date(2026, 2, 8)]
