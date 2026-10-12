"""Tests des parties déterministes de l'assistant (aucun appel à l'API Claude)."""
from datetime import date

import pytest

from bra.assistant import (RAPPEL_PISTES, comparer_faits, expliciter_dates, formater_faits, niveaux_autorises,
                           niveaux_cites_hors_faits, orientations_selon_bulletin, recommandation_de_sortie)
from bra.requetes import bulletin_du_jour
from tests.test_bra import db, dossier_reference  # noqa: F401  (fixtures)


def test_faits_affiches_viennent_de_sql(db):
    b = bulletin_du_jour(db, "QUEYRAS", date(2026, 2, 8))
    texte = formater_faits(b)
    assert "**Queyras** : bulletin émis le 7 février 2026 à 16h, valable le 8 février 2026" in texte
    assert "Risque maximal : **3 (marqué)**" in texte
    assert "Détail : 2 (limité) sous 2000 m, 3 (marqué) au-dessus de 2000 m" in texte
    # Ordre de la rose des vents, notation française (W -> O).
    assert "Pentes les plus dangereuses : N, NE, E, O, NO" in texte
    assert "couche fragile persistante, neige fraîche" in texte


def test_pentes_toutes_ou_aucune(db):
    b = bulletin_du_jour(db, "QUEYRAS", date(2026, 2, 8))
    b["pentes_exposees"] = ["N", "NE", "E", "SE", "S", "SW", "W", "NW"]
    assert "Pentes les plus dangereuses : toutes les orientations" in formater_faits(b)

    b["pentes_exposees"] = []
    assert "aucune orientation signalée par le bulletin" in formater_faits(b)

    b["pentes_renseignees"] = False  # balise PENTE absente : on n'affiche rien plutôt que « aucune »
    assert "Pentes les plus dangereuses" not in formater_faits(b)


def test_detail_secteur_et_evolution(db):
    b = bulletin_du_jour(db, "QUEYRAS", date(2026, 2, 8))
    # Cas réel Queyras 22/12/2025 : découpage géographique est/ouest.
    b["risques_par_altitude"] = [
        {"niveau_risque": 3, "altitude_seuil": None, "zone": "E", "evolution": None},
        {"niveau_risque": 2, "altitude_seuil": None, "zone": "W", "evolution": None},
    ]
    assert "Détail : 3 (marqué) est du massif, 2 (limité) ouest du massif" in formater_faits(b)
    # Cas réel Vanoise 10/04/2026 : « faible évoluant en marqué ».
    b["risques_par_altitude"] = [{"niveau_risque": 1, "altitude_seuil": None, "zone": None, "evolution": 3}]
    assert "Détail : 1 (faible) évoluant en 3 (marqué) dans la journée" in formater_faits(b)
    assert niveaux_autorises([b], []) >= {1, 3}


def test_risque_moindre_permis_seulement_si_le_bulletin_l_emploie():
    # Texte réel d'un BRA Haute-Tarentaise : « au dessus bon regel par ciel clair, risque réduit ».
    bulletin = "Mer de nuages vers 2000 m la nuit jusqu'au matin : au dessus bon regel par ciel clair, risque réduit."
    texte = "Au-dessus de la mer de nuages, le bulletin indique un risque réduit grâce au bon regel."
    assert recommandation_de_sortie(texte, bulletin) is None
    assert recommandation_de_sortie(texte, "Plaques sensibles au-dessus de 2000 m.") == "risque réduit"


@pytest.mark.parametrize("texte", [
    "Le bulletin note qu'en faces Sud neige presque de névé, plus stable.",
    "Selon le bulletin : « en faces Sud neige presque de névé, plus stable ».",
    "En Sud bien raide, on peut faire du ski de printemps jusqu'à haute altitude, 3200/3400 m.",
])
def test_citation_du_bulletin_permise(texte):
    # Phrases réelles de BRA : citées mot pour mot, elles ne sont pas un feu vert de l'assistant.
    bulletin = ("En faces Sud neige presque de névé, plus stable. Déclenchements skieurs : peu probables. "
                "En Sud bien raide, on peut faire du ski de printemps jusqu'à haute altitude, 3200/3400 m.")
    assert recommandation_de_sortie(texte) is not None  # sans le bulletin, la tournure est bien détectée
    assert recommandation_de_sortie(texte, bulletin) is None


@pytest.mark.parametrize("texte", [
    "Les faces sud sont plus stables aujourd'hui.",            # reformulation : pas une citation
    "Cette pente est sans danger.",                             # « sans danger » isolé dans la source
    "En faces Sud, la neige est plus stable : vous pouvez y aller.",
])
def test_reformulation_ou_fragment_isole_reste_detecte(texte):
    bulletin = ("En faces Sud neige presque de névé, plus stable. "
                "Le guide rappelle qu'aucune pente n'est sans danger.")
    assert recommandation_de_sortie(texte, bulletin)


def test_orientations_non_signalees_affichees_si_question_sur_pentes(db):
    b = bulletin_du_jour(db, "QUEYRAS", date(2026, 2, 8))
    assert "non signalées" not in formater_faits(b)
    texte = formater_faits(b, avec_non_signalees=True)
    assert "Orientations non signalées parmi les plus dangereuses : SE, S, SO" in texte
    assert recommandation_de_sortie(texte) is None


def test_comparaison_identique_et_differente(db):
    queyras = bulletin_du_jour(db, "QUEYRAS", date(2026, 2, 8))
    embrunais = bulletin_du_jour(db, "EMBRUNAIS_PARPAILLON", date(2026, 2, 8))
    assert "ont les mêmes niveaux" in comparer_faits(queyras, embrunais)

    vanoise = bulletin_du_jour(db, "VANOISE", date(2026, 2, 8))
    texte = comparer_faits(queyras, vanoise)
    assert "diffèrent sur" in texte
    assert "niveaux par zone ou dans la journée" in texte  # seuil 2000 m contre 1800 m

    # Cas s15 : le code dit lui-même quel massif a le risque le plus élevé.
    embrunais["risque_maxi"] = 2
    assert "risque maximal plus élevé en Queyras (3 (marqué)) qu'en Embrunais Parpaillon (2 (limité))" in \
        comparer_faits(queyras, embrunais)


def test_garde_fou_niveaux(db):
    b = bulletin_du_jour(db, "QUEYRAS", date(2026, 2, 8))
    autorises = niveaux_autorises([b], [])
    assert autorises == {2, 3}
    assert niveaux_cites_hors_faits("Le risque reste à 3 au-dessus de 2000 m.", autorises) == set()
    assert niveaux_cites_hors_faits("Le risque passe au niveau 4 demain.", autorises) == {4}
    # Les altitudes et tailles d'avalanche ne sont pas prises pour des niveaux.
    assert niveaux_cites_hors_faits("Risque au-dessus de 2200/2300 m, avalanches de taille 2.", autorises) == set()


def test_expliciter_dates():
    # Cas réel s14 : posée le 12/02, « demain » doit devenir le 13/02 avant d'être envoyée à Claude.
    assert expliciter_dates("Et demain aux Arcs, ça va se calmer ?", date(2026, 2, 13)) == \
        "Et le 13 février 2026 aux Arcs, ça va se calmer ?"
    assert expliciter_dates("Il a neigé hier à Vars ?", date(2026, 2, 18)) == "Il a neigé le 18 février 2026 à Vars ?"
    assert expliciter_dates("Risque aujourd’hui ?", date(2026, 1, 15)) == "Risque le 15 janvier 2026 ?"
    assert expliciter_dates("Risque en Vanoise ?", date(2026, 1, 15)) == "Risque en Vanoise ?"


# Règle n° 1 : jamais de feu vert ni de feu rouge. Phrases bloquées : conclusions de l'assistant sur une sortie ou une pente.
@pytest.mark.parametrize("texte", [
    "Les pentes sud sont donc les moins dangereuses.",                       # cas réel u1 (v1)
    "C'est cette évolution qui rend les pentes ensoleillées plus stables.",  # cas réel u1 (baseline)
    "Avec un risque 5, mieux vaut renoncer au hors-piste.",
    "Vous pouvez y aller sans problème en versant sud.",
    "N'y allez pas aujourd'hui.",
    "Je vous déconseille cette sortie.",
    "Le versant est est sans danger ce matin.",
    "Évitez de sortir avant midi.",
    "Deux zones offrent une stabilité relative : près des crêtes et en pentes sud.",  # cas réel s02 (baseline)
    "Deux secteurs offrent un risque réduit : près des crêtes et dans les pentes ensoleillées.",  # cas réel s25 (v2)
    "Le danger est moindre en versant sud.",
    "Les pentes ensoleillées présentent un moindre risque.",
    "Le versant est est moins risqué ce matin.",
    "En dessous de 2000 m, le risque reste modéré.",
])
def test_feu_vert_ou_rouge_detecte(texte):
    assert recommandation_de_sortie(texte)


@pytest.mark.parametrize("texte", [
    "Oui, c'est très chaud aux Arcs le 19 février : risque très fort.",      # description du danger, autorisée
    "Le guide conseille de renoncer si les conditions sont trop douteuses (p. 20).",
    "Je vous recommande de consulter directement le guide Météo-France.",    # renvoi vers une source
    "Les plaques restent sensibles au passage d'un skieur dans les pentes raides.",
    "Le risque est plus élevé au-dessus de 2000 m.",
    # Les noms de niveaux et les comparaisons de danger par altitude décrivent le bulletin : permis.
    "Risque faible sous 2000 m, risque limité au-dessus.",
    "Le risque est plus faible en dessous de 1800 m.",
    "Les plaques sont présentes dans les secteurs moins exposés au vent.",
    "Le bulletin ne dit pas si tu peux y aller : il décrit la situation.",
    "Le bulletin ne classe pas les pentes du plus au moins dangereux.",          # cas réel u1 (v3, essai)
    RAPPEL_PISTES,
])
def test_description_ou_citation_autorisee(texte):
    assert recommandation_de_sortie(texte) is None


def test_orientations_selon_bulletin(db):
    b = bulletin_du_jour(db, "QUEYRAS", date(2026, 2, 8))
    texte = orientations_selon_bulletin(b)
    assert "les orientations N, NE, E, O, NO" in texte and "il ne cite pas SE, S, SO" in texte
    # Ce texte du code ne doit pas lui-même déclencher le garde-fou.
    assert recommandation_de_sortie(texte) is None
    b["pentes_exposees"] = ["N", "NE", "E", "SE", "S", "SW", "W", "NW"]
    assert orientations_selon_bulletin(b) == ""
