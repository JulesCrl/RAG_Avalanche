from datetime import date

import pytest

from bra.analyse import DateInvalide, ExpressionDate, resoudre_date, resoudre_lieu, resoudre_lieux

AUJOURD_HUI = date(2026, 9, 28)
DERNIER = date(2026, 6, 8)


# --- Lieu ------------------------------------------------------------------

@pytest.mark.parametrize(
    "texte, massifs",
    [
        ("Les Arcs", ["HAUTE_TARENTAISE"]),
        ("les arcs", ["HAUTE_TARENTAISE"]),
        ("Val Thorens", ["VANOISE"]),
        ("Alpe d'Huez", ["GRANDES_ROUSSES"]),
        ("L'Alpe-d'Huez", ["GRANDES_ROUSSES"]),
        ("Vanoise", ["VANOISE"]),
        ("Haute Tarentaise", ["HAUTE_TARENTAISE"]),
        ("Embrunais-Parpaillon", ["EMBRUNAIS_PARPAILLON"]),
        ("Queyras", ["QUEYRAS"]),
        # Préposition ou article recopiés par Claude (cas réel : « aux Arcs »).
        ("aux Arcs", ["HAUTE_TARENTAISE"]),
        ("à l'Alpe d'Huez", ["GRANDES_ROUSSES"]),
        ("en Vanoise", ["VANOISE"]),
        ("dans le Queyras", ["QUEYRAS"]),
        ("massif du Queyras", ["QUEYRAS"]),
        ("à Val Thorens", ["VANOISE"]),
        # Surnoms et villages (cas s25-s27 de l'évaluation).
        ("Val Tho", ["VANOISE"]),
        ("Arc 1950", ["HAUTE_TARENTAISE"]),
        ("Arcs 1800", ["HAUTE_TARENTAISE"]),
        ("l'Alpe", ["GRANDES_ROUSSES"]),
    ],
)
def test_lieux_connus(texte, massifs):
    assert resoudre_lieu(texte).massifs == massifs


@pytest.mark.parametrize("station", ["Vars", "Risoul"])
def test_vars_risoul_comparaison_des_deux_massifs(station):
    lieu = resoudre_lieu(station)
    assert lieu.massifs == ["QUEYRAS", "EMBRUNAIS_PARPAILLON"]
    assert lieu.comparaison


def test_un_seul_massif_pas_de_comparaison():
    assert not resoudre_lieu("Les Arcs").comparaison


@pytest.mark.parametrize("texte", [None, "", "Chamonix", "Tarentaise", "Arc 1700", "Mont-Blanc"])
def test_lieu_inconnu_ou_approximatif_refuse(texte):
    # Pas d'approximation : « Tarentaise » pourrait être Haute-Tarentaise ou Vanoise.
    assert resoudre_lieu(texte) is None


# --- Date ------------------------------------------------------------------

def test_sans_date_dernier_bulletin():
    assert resoudre_date(None, AUJOURD_HUI, DERNIER) == DERNIER
    assert resoudre_date(ExpressionDate(), AUJOURD_HUI, DERNIER) == DERNIER


def test_sans_date_et_base_vide():
    assert resoudre_date(None, AUJOURD_HUI, None) is None


@pytest.mark.parametrize("decalage, attendu", [(0, date(2026, 9, 28)), (1, date(2026, 9, 29)), (-1, date(2026, 9, 27))])
def test_dates_relatives(decalage, attendu):
    assert resoudre_date(ExpressionDate(decalage_jours=decalage), AUJOURD_HUI, DERNIER) == attendu


def test_date_sans_annee_prend_la_plus_recente():
    assert resoudre_date(ExpressionDate(jour=12, mois=2), AUJOURD_HUI, DERNIER) == date(2026, 2, 12)
    # En janvier 2027, « le 12 février » désigne février 2026 (pas de bulletin dans le futur).
    assert resoudre_date(ExpressionDate(jour=12, mois=2), date(2027, 1, 10), DERNIER) == date(2026, 2, 12)
    # « demain » reste accessible sans année : bulletin émis aujourd'hui, valable demain.
    assert resoudre_date(ExpressionDate(jour=29, mois=9), AUJOURD_HUI, DERNIER) == date(2026, 9, 29)


def test_date_avec_annee():
    assert resoudre_date(ExpressionDate(jour=12, mois=2, annee=2025), AUJOURD_HUI, DERNIER) == date(2025, 2, 12)


@pytest.mark.parametrize("expression", [ExpressionDate(jour=31, mois=2), ExpressionDate(jour=12), ExpressionDate(mois=2)])
def test_date_invalide(expression):
    with pytest.raises(DateInvalide):
        resoudre_date(expression, AUJOURD_HUI, DERNIER)


def test_plusieurs_lieux():
    lieu, inconnu = resoudre_lieux(["Risoul", "Vars"])
    assert (lieu.massifs, inconnu) == (["QUEYRAS", "EMBRUNAIS_PARPAILLON"], None)
    lieu, _ = resoudre_lieux(["Les Arcs", "Val Thorens"])
    assert lieu.massifs == ["HAUTE_TARENTAISE", "VANOISE"] and lieu.comparaison
    assert resoudre_lieux(["Les Arcs", "Chamonix"]) == (None, "Chamonix")
    assert resoudre_lieux([]) == (None, None)
