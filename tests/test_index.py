"""Tests de l'indexation avec un encodeur factice (pas de téléchargement de modèle)."""
import hashlib
from datetime import date, datetime

import pytest

from bra.database import charger_bulletin, connecter, initialiser
from bra.guide import lire_sections
from bra.index import decouper, indexer, indexer_guide, rechercher, rechercher_guide
from bra.parser import Chunk, parser_bulletin
from tests.test_bra import FICHIERS, QUEYRAS

pytestmark = pytest.mark.skipif(not FICHIERS, reason="aucun XML dans BRA_DB/")


class EncodeurFactice:
    """Un « token » par mot ; vecteur = sac de mots haché, pour que des textes proches soient proches."""

    nom = "factice"

    def compter_tokens(self, texte):
        return len(texte.split())

    def _vecteur(self, texte):
        v = [0.0] * 64
        for mot in texte.lower().split():
            v[int(hashlib.md5(mot.encode()).hexdigest(), 16) % 64] += 1
        norme = sum(x * x for x in v) ** 0.5 or 1
        return [x / norme for x in v]

    def encoder_passages(self, textes):
        return [self._vecteur(t) for t in textes]

    def encoder_question(self, question):
        return self._vecteur(question)


@pytest.fixture
def base(tmp_path):
    conn = connecter(tmp_path / "bra.sqlite")
    initialiser(conn)
    for fichier in FICHIERS:
        charger_bulletin(conn, parser_bulletin(fichier))
    yield conn, tmp_path / "bra.sqlite", tmp_path / "chroma"
    conn.close()


def test_decouper_texte_court_intact():
    assert decouper("un deux trois", lambda t: len(t.split()), max_tokens=5) == ["un deux trois"]


def test_decouper_respecte_la_limite_et_garde_tout_le_texte():
    texte = "a b c d.\ne f g h.\ni j k l m n. o p q r."
    parties = decouper(texte, lambda t: len(t.split()), max_tokens=6)
    assert all(len(p.split()) <= 6 for p in parties)
    assert " ".join(" ".join(parties).split()) == " ".join(texte.split())


def test_indexation_complete_puis_incrementale(base):
    conn, db, chroma = base
    enc = EncodeurFactice()
    premier = indexer(db, chroma, enc)
    assert premier["encodes"] == premier["total"] == 4 * len(FICHIERS)

    second = indexer(db, chroma, enc)
    assert (second["encodes"], second["supprimes"]) == (0, 0)


def test_chunk_modifie_reencode_et_chunk_disparu_supprime(base):
    conn, db, chroma = base
    enc = EncodeurFactice()
    indexer(db, chroma, enc)

    b = parser_bulletin(QUEYRAS)
    b.chunks = [Chunk("stabilite", "Texte corrigé par un amendement.")]
    charger_bulletin(conn, b)
    bilan = indexer(db, chroma, enc)
    assert bilan["encodes"] == 1
    assert bilan["supprimes"] == 3  # qualite_neige, resume, tendance du Queyras


def test_chunk_long_decoupe_en_parties(base):
    conn, db, chroma = base
    b = parser_bulletin(QUEYRAS)
    b.chunks = [Chunk("stabilite", "\n".join(f"Paragraphe {i} " + "mot " * 300 for i in range(4)))]
    charger_bulletin(conn, b)
    indexer(db, chroma, EncodeurFactice())
    resultats = rechercher("Paragraphe", chroma, id_massif="QUEYRAS", encodeur=EncodeurFactice(), k=10)
    parties = [r for r in resultats if r["id_chunk"] == "QUEYRAS_202602071600_stabilite"]
    assert sorted(r["partie"] for r in parties) == [1, 2, 3, 4]
    assert all(r["nb_parties"] == 4 for r in parties)


def test_filtres_massif_date_type(base):
    conn, db, chroma = base
    enc = EncodeurFactice()
    indexer(db, chroma, enc)

    resultats = rechercher(
        "plaques neige récente", chroma, id_massif="VANOISE", debut=date(2026, 2, 8), fin=date(2026, 2, 8),
        types_contenu=["stabilite"], encodeur=enc,
    )
    assert len(resultats) == 1
    assert resultats[0]["id_massif"] == "VANOISE"
    assert resultats[0]["date_validite"] == "2026-02-08"
    assert resultats[0]["type_contenu"] == "stabilite"

    assert rechercher("plaques", chroma, id_massif="VANOISE", debut=date(2026, 3, 1), encodeur=enc) == []


def test_modele_different_refuse(base):
    conn, db, chroma = base
    indexer(db, chroma, EncodeurFactice())
    autre = EncodeurFactice()
    autre.nom = "autre-modele"
    with pytest.raises(SystemExit, match="supprimer"):
        indexer(db, chroma, autre)


def test_guide_sections_et_pages():
    sections = lire_sections()
    titres = {s.titre: s.page for s in sections}
    assert titres["Échelle de risque : indice 5 très fort"] == 8
    assert titres["Situation avalancheuse typique : neige ventée"] == 13
    assert all(s.texte for s in sections)
    assert len({s.id_section for s in sections}) == len(sections)


def test_guide_titre_sans_page_refuse(tmp_path):
    fichier = tmp_path / "guide.md"
    fichier.write_text("## Titre | page 3\ntexte\n## Titre sans page\ntexte\n", encoding="utf-8")
    with pytest.raises(ValueError, match="sans numéro de page"):
        lire_sections(fichier)


def test_guide_indexe_dans_sa_propre_collection(base, tmp_path):
    conn, db, chroma = base
    enc = EncodeurFactice()
    indexer(db, chroma, enc)
    bilan = indexer_guide(chroma, encodeur=enc)
    assert bilan["total"] == len(lire_sections())
    assert indexer_guide(chroma, encodeur=enc)["encodes"] == 0

    resultat = rechercher_guide("neige ventée plaques vent", chroma, k=1, encodeur=enc)[0]
    assert resultat["source"] == "Guide avalanche Météo-France 2025"
    assert resultat["page"] == 13
    # Les bulletins ne contiennent jamais de section du guide, et inversement.
    assert all("id_massif" in r for r in rechercher("neige ventée", chroma, encodeur=enc, k=20))
