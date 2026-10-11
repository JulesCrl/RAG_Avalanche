"""Chaîne question -> réponse de l'assistant BRA.

1. Claude classe la question et en extrait le lieu et la date (sortie structurée).
2. Le code résout le lieu (référentiel) et la date (calendrier), ou demande une précision.
3. Les faits chiffrés (niveau, altitude, pentes, situations typiques) sont lus en SQL et
   affichés tels quels : ils ne passent jamais par le LLM.
4. Claude rédige l'explication à partir des seuls textes fournis, avec citations.
   Un contrôle vérifie qu'il ne cite aucun niveau de risque absent des faits SQL.

Usage : uv run python -m bra.assistant "Quel est le risque aux Arcs demain ?"
"""
from __future__ import annotations

import re
import sqlite3
import sys
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path
from typing import Literal

import anthropic
from pydantic import BaseModel, Field

from .analyse import DateInvalide, ExpressionDate, resoudre_date, resoudre_lieux
from .config import MODELE_CLASSIFICATION, MODELE_REPONSE
from .database import MASSIFS, connecter
from .guide import lire_sections
from .index import rechercher_guide
from .requetes import bulletin_du_jour, dernier_jour_disponible, evolution_risque

NOMS_NIVEAUX = {1: "faible", 2: "limité", 3: "marqué", 4: "fort", 5: "très fort"}
ORIENTATIONS_FR = {"N": "N", "NE": "NE", "E": "E", "SE": "SE", "S": "S", "SW": "SO", "W": "O", "NW": "NO"}
AVERTISSEMENT = (
    "Le BRA est un outil d'aide à la décision, valable hors pistes balisées et ouvertes : "
    "il ne constitue ni un feu vert ni un feu rouge (Guide avalanche Météo-France 2025, p. 10)."
)


# --- 1. Classification -----------------------------------------------------

class DateQuestion(BaseModel):
    decalage_jours: int | None = Field(None, description="0 aujourd'hui, 1 demain, -1 hier ; sinon null")
    jour: int | None = Field(None, description="jour du mois si date explicite")
    mois: int | None = Field(None, description="mois (1-12) si date explicite")
    annee: int | None = Field(None, description="année si précisée, sinon null")


class Classification(BaseModel):
    type: Literal["bulletin", "generale", "ambigue", "hors_sujet"]
    intention: Literal["niveau", "evolution", "explication", "observation"] = Field(
        description="pour type=bulletin : niveau du jour, évolution sur plusieurs jours, explication (pourquoi, plaques, conditions), "
                    "ou observation (UNIQUEMENT un fait physique passé : quantité de neige tombée, avalanches survenues ; "
                    "une question sur le niveau de risque d'un jour passé, « c'était quel niveau ? », est \"niveau\")"
    )
    lieux: list[str] = Field(description="massifs ou stations cités, tels qu'écrits dans la question ; liste vide si aucun")
    date: DateQuestion | None = Field(description="date telle que formulée, sinon null")
    precision_a_demander: str | None = Field(description="pour type=ambigue : question courte à poser à l'utilisateur")
    question_sur_pistes: bool = Field(description="la question porte sur des pistes balisées / ouvertes ou cite un nom de piste")
    question_sur_pentes: bool = Field(
        description="la question porte sur les pentes ou les orientations (« quelle pente », « quels versants », « la face nord »)"
    )
    demande_feu_vert: bool = Field(
        description="l'utilisateur demande s'il peut y aller, si c'est tranquille, sûr, ou un bon jour pour sortir"
    )
    lien_conditions_du_jour: bool = Field(
        description="pour type=generale : la question évoque aussi la situation actuelle de l'utilisateur (« il a neigé hier », « en ce moment »)"
    )


CONSIGNES_CLASSIFICATION = """Tu classes des questions posées à un assistant sur les bulletins d'estimation du risque d'avalanche (BRA) de Météo-France.

Types :
- "bulletin" : la réponse dépend des conditions d'un jour et d'un lieu (niveau de risque, plaques présentes, évolution, pentes dangereuses, enneigement...), même si le lieu ou la date ne sont pas précisés : ce n'est pas une ambiguïté, le système demandera lui-même le massif, et sans date il prend le dernier bulletin.
  Une question qui demande à la fois la situation du jour et une explication (« quelles situations aujourd'hui et qu'est-ce que ça veut dire ? ») est de type "bulletin" : le système fournit aussi les définitions.
  « Je peux y aller ? », « c'est tranquille ? » avec un lieu ou un jour (« risque 2 aux Arcs aujourd'hui, je peux y aller ? ») est de type "bulletin" : la réponse s'appuie sur le bulletin du jour.
- "generale" : la réponse ne dépend ni d'un lieu ni d'un jour (définition, fonctionnement de l'échelle de risque ou du bulletin, sa validité, conseils de sécurité, que faire en cas d'accident).
- "ambigue" : à n'utiliser qu'en dernier recours, quand aucune des deux lectures ne permet une réponse utile ; remplis precision_a_demander. Une question sur le fonctionnement du bulletin n'est pas ambiguë.
- "hors_sujet" : sans rapport avec la neige, les avalanches ou la sécurité en montagne.

Lieux : recopie chaque nom de massif, de station ou de lieu cité, sans préposition ni article (« aux Arcs » -> « Arcs », « Risoul ou Vars » -> ["Risoul", "Vars"]), sans le corriger ni le deviner.
Date : ne calcule aucune date. « aujourd'hui » = decalage_jours 0, « demain » = 1, « hier » = -1 ; « le 12 février » = jour 12, mois 2. Pas de date dans la question = null."""


def _appel(etape: str, reponse) -> dict:
    """Trace d'un appel API : modèle réellement servi et tokens, pour le suivi du coût et l'évaluation."""
    return {"etape": etape, "model": reponse.model, "stop_reason": reponse.stop_reason,
            "input_tokens": reponse.usage.input_tokens, "output_tokens": reponse.usage.output_tokens}


def classer(client: anthropic.Anthropic, question: str) -> tuple[Classification, dict]:
    reponse = client.messages.parse(
        model=MODELE_CLASSIFICATION,
        max_tokens=512,
        system=CONSIGNES_CLASSIFICATION,
        messages=[{"role": "user", "content": question}],
        output_format=Classification,
    )
    return reponse.parsed_output, _appel("classification", reponse)


# --- 3. Faits déterministes ------------------------------------------------

def _date_fr(jour: date) -> str:
    mois = ["janvier", "février", "mars", "avril", "mai", "juin", "juillet", "août",
            "septembre", "octobre", "novembre", "décembre"][jour.month - 1]
    return f"{jour.day} {mois} {jour.year}"


SECTEURS = {"E": "est du massif", "W": "ouest du massif", "O": "ouest du massif",
            "N": "nord du massif", "S": "sud du massif"}


def _libelle_zone(zone: str | None) -> str:
    """LOC du XML : altitude (« <2000 », « >2000 »), secteur (E/W, N/S) ou vide (massif entier)."""
    if not zone:
        return ""
    if zone.startswith("<"):
        return f" sous {zone[1:]} m"
    if zone.startswith(">"):
        return f" au-dessus de {zone[1:]} m"
    return f" {SECTEURS.get(zone, zone)}"


def _niveau(n: int) -> str:
    return f"{n} ({NOMS_NIVEAUX[n]})"


def _orientations_non_signalees(b: dict) -> list[str]:
    return [ORIENTATIONS_FR[o] for o in ORIENTATIONS_FR if o not in b["pentes_exposees"]]


def formater_faits(b: dict, avec_non_signalees: bool = False) -> str:
    """Bloc affiché tel quel à l'utilisateur : tous les chiffres viennent de SQL.

    avec_non_signalees : question sur les pentes (« la moins dangereuse ? »). La meilleure réponse possible,
    validée par l'utilisateur, est la liste des orientations que le bulletin ne signale pas : le code la donne.
    """
    emis = b["date_bulletin"]
    lignes = [
        f"**{b['nom_affiche']}** : bulletin émis le {_date_fr(date.fromisoformat(emis[:10]))} à {emis[11:13]}h, "
        f"valable le {_date_fr(date.fromisoformat(b['date_validite'][:10]))}",
        f"- Risque maximal : **{b['risque_maxi']} ({NOMS_NIVEAUX[b['risque_maxi']]})**",
    ]
    zones = b["risques_par_altitude"]
    # Détail affiché s'il y a un découpage (altitude ou secteur) ou une évolution dans la journée.
    if any(z["zone"] or z["evolution"] for z in zones):
        detail = ", ".join(
            _niveau(z["niveau_risque"])
            + (f" évoluant en {_niveau(z['evolution'])} dans la journée" if z["evolution"] else "")
            + _libelle_zone(z["zone"])
            for z in zones
        )
        lignes.append(f"- Détail : {detail}")
    commentaire = f" ({b['commentaire_pentes']})" if b.get("commentaire_pentes") else ""
    if len(b["pentes_exposees"]) == len(ORIENTATIONS_FR):
        # Cas fréquent à risque 4-5 : le bulletin coche toutes les orientations.
        lignes.append(f"- Pentes les plus dangereuses : toutes les orientations{commentaire}")
    elif b["pentes_exposees"]:
        pentes = ", ".join(ORIENTATIONS_FR[o] for o in ORIENTATIONS_FR if o in b["pentes_exposees"])
        lignes.append(f"- Pentes les plus dangereuses : {pentes}{commentaire}")
        if avec_non_signalees:
            lignes.append(f"- Orientations non signalées parmi les plus dangereuses : "
                          f"{', '.join(_orientations_non_signalees(b))} (ce qui ne signifie pas une absence de danger)")
    elif b.get("pentes_renseignees"):
        # Cas fréquent à risque 1 : balise présente mais aucune orientation cochée.
        lignes.append(f"- Pentes les plus dangereuses : aucune orientation signalée par le bulletin{commentaire}")
    situations = [s for s in (b["situation_typique_1_nom"], b["situation_typique_2_nom"]) if s]
    if situations:
        lignes.append(f"- Situations avalancheuses typiques : {', '.join(situations)}")
    if b["risque_maxi_j2"]:
        lendemain = date.fromisoformat(b["date_validite"][:10]) + timedelta(days=1)
        lignes.append(f"- Tendance pour le {_date_fr(lendemain)} : {_niveau(b['risque_maxi_j2'])}")
    return "\n".join(lignes)


def comparer_faits(a: dict, b: dict) -> str:
    """Différences entre deux bulletins (Vars / Risoul), calculées sur les données SQL."""
    def resume(x):
        return {
            "risque maximal": x["risque_maxi"],
            "niveaux par zone ou dans la journée": [(z["zone"], z["niveau_risque"], z["evolution"]) for z in x["risques_par_altitude"]],
            "pentes les plus dangereuses": sorted(x["pentes_exposees"]),
            "situations typiques": sorted(s for s in (x["situation_typique_1_nom"], x["situation_typique_2_nom"]) if s),
            "tendance du lendemain": x["risque_maxi_j2"],
        }
    ra, rb = resume(a), resume(b)
    differences = [cle for cle in ra if ra[cle] != rb[cle]]
    if not differences:
        return (f"**Comparaison** : {a['nom_affiche']} et {b['nom_affiche']} ont les mêmes niveaux, "
                "pentes dangereuses, situations typiques et tendance ce jour-là.")
    # Lequel est le plus risqué : conclu par le code, jamais par Claude (cas s15).
    if a["risque_maxi"] == b["risque_maxi"]:
        niveaux = f"même risque maximal ({_niveau(a['risque_maxi'])})"
    else:
        haut, bas = (a, b) if a["risque_maxi"] > b["risque_maxi"] else (b, a)
        niveaux = (f"risque maximal plus élevé en {haut['nom_affiche']} ({_niveau(haut['risque_maxi'])}) "
                   f"qu'en {bas['nom_affiche']} ({_niveau(bas['risque_maxi'])})")
    return (f"**Comparaison** : {niveaux}. {a['nom_affiche']} et {b['nom_affiche']} diffèrent sur : "
            f"{', '.join(differences)}.")


def niveaux_autorises(bulletins: list[dict], series: list[list[tuple[date, int]]]) -> set[int]:
    niveaux = set()
    for b in bulletins:
        niveaux |= {b["risque_maxi"], *(z["niveau_risque"] for z in b["risques_par_altitude"]),
                    *(z["evolution"] for z in b["risques_par_altitude"] if z["evolution"])}
        if b["risque_maxi_j2"]:
            niveaux.add(b["risque_maxi_j2"])
    for serie in series:
        niveaux |= {n for _, n in serie}
    return niveaux


# Règle n° 1 du projet : jamais de feu vert ni de feu rouge. Interdit : classer une pente, une orientation ou une
# sortie comme plus sûre (sans danger, la moins dangereuse...), ou recommander d'y aller ou de renoncer.
# Autorisé : décrire le niveau de danger, citer un conseil du guide ou du bulletin en l'attribuant.
MOTIFS_FEU = [
    # « le bulletin ne dit pas si tu peux y aller » rapporte la question : ce n'est pas un feu vert.
    r"(?<!si )\b(?:vous pouvez|tu peux|on peut|vous pourrez|tu pourras)\s+(?:y aller|sortir|partir|skier|faire|y monter|vous y rendre)",
    r"\b(?:n'y allez pas|n'y va pas|ne sortez pas|ne sors pas|restez chez vous|reste chez toi)\b",
    r"\b(?:évitez|évite)\s+de\s+(?:sortir|partir|y aller|skier)",
    r"\bmieux vaut\s+(?:renoncer|rester|éviter|ne pas|attendre|reporter)",
    # « je vous conseille de consulter le guide » renvoie vers une source : ce n'est pas une décision de sortie.
    r"\b(?:je|nous)\s+(?:vous |te )?(?:recommande|conseille|déconseille|suggère)"
    r"(?!\s+(?:de\s+)?(?:consulter|vous renseigner|te renseigner|lire|vous former|te former|vérifier|suivre|regarder))",
    r"\b(?:il est|c'est)\s+(?:préférable|recommandé|conseillé|déconseillé)\s+de\s+(?:sortir|partir|y aller|renoncer|skier|rester)",
    r"\b(?:les?|la)\s+(?:pentes?|orientations?|versants?|faces?)?\s*(?:\w+\s+){0,3}(?:les?|la)\s+moins\s+dangereu",
    r"(?<!au )\bmoins\s+dangereu\w*\b",  # « ne classe pas du plus au moins dangereux » ne désigne aucune pente
    r"\b(?:pentes?|orientations?|versants?|faces?|itinéraires?|sortie|secteurs?|zones?)\b[^.]{0,50}?\b(?:sûres?|sans danger|sans risque|plus stables?|en sécurité|stabilité relative)\b",
    r"\bstabilité relative\b",
    r"\b(?:sans (?:aucun )?danger|sans (?:aucun )?risque)\b",
]
RE_FEU = re.compile("|".join(MOTIFS_FEU), re.I)

# Risque moindre pour un secteur (cas réel s25 v2 : « deux secteurs offrent un risque réduit »). Les noms de niveaux
# (faible, limité...) restent permis ; tout autre qualificatif n'est permis que si le bulletin fourni l'emploie lui-même
# (« risque réduit » figure dans un BRA Haute-Tarentaise, pas dans celui de Vanoise de s25) : choix de l'utilisateur, 07/10/2026.
QUALIFICATIFS_RISQUE_MOINDRE = (r"réduite?s?|moindres?|modérée?s?|minimes?|minimale?s?|bas|basses?|atténuée?s?|diminuée?s?|"
                                r"négligeables?|acceptables?|raisonnables?|maîtrisée?s?|contenue?s?|inférieure?s?")
RE_RISQUE_MOINDRE = re.compile(
    r"\b(?:risques?|dangers?)\s+(?:(?:est|reste|devient|semble|paraît|y est|y reste)\s+)?"
    r"(?:(?:un peu|bien|nettement|plus|très|assez)\s+)?(?:" + QUALIFICATIFS_RISQUE_MOINDRE + r")\b"
    r"|\bmoindres?\s+(?:risques?|dangers?)\b"
    r"|\bmoins\s+(?:risqué|à risque)\w*",
    re.I,
)
MASQUE_FEU = ("(Explication masquée : elle contenait une recommandation de sortie ou présentait des pentes comme plus sûres, "
              "ce que l'assistant ne fait jamais. La décision t'appartient : appuie-toi sur le bulletin complet et le terrain.)")
MASQUE_NIVEAU = "(Explication masquée : elle mentionnait un niveau de risque absent du bulletin.)"


def orientations_selon_bulletin(b: dict) -> str:
    """Ce que le bulletin dit des orientations, calculé par le code : affiché quand l'explication est masquée."""
    signalees = [ORIENTATIONS_FR[o] for o in ORIENTATIONS_FR if o in b["pentes_exposees"]]
    autres = [ORIENTATIONS_FR[o] for o in ORIENTATIONS_FR if o not in b["pentes_exposees"]]
    if not signalees or not autres:
        return ""
    return (f"\nLe bulletin signale comme les plus dangereuses les orientations {', '.join(signalees)} ; "
            f"il ne cite pas {', '.join(autres)} parmi elles, ce qui ne signifie pas une absence de danger.")


def _normaliser_citation(texte: str) -> str:
    texte = texte.lower().replace("’", "'")
    texte = re.sub(r"[«»\"“”]", " ", texte)
    return re.sub(r"\s+", " ", texte).strip()


LONGUEUR_CITATION = 40


def _est_citation(texte: str, debut: int, fin: int, sources: str) -> bool:
    """La tournure vient-elle mot pour mot des documents fournis ?

    On l'élargit à des mots voisins de la même phrase jusqu'à LONGUEUR_CITATION caractères : « sans danger » seul
    pourrait venir d'une phrase négative du guide, alors que « En faces Sud neige presque de névé, plus stable »
    est une citation réelle du bulletin.
    """
    phrase_debut = max(texte.rfind(c, 0, debut) for c in ".!?\n«»") + 1
    fins = [i for i in (texte.find(c, fin) for c in ".!?\n«»") if i != -1]
    phrase_fin = min(fins) if fins else len(texte)
    while fin - debut < LONGUEUR_CITATION and (debut > phrase_debut or fin < phrase_fin):
        if fin < phrase_fin:  # mot suivant
            m = re.match(r"\s*\S+", texte[fin:phrase_fin])
            fin += m.end() if m else phrase_fin - fin
        else:  # mot précédent
            m = re.search(r"\S+\s*$", texte[phrase_debut:debut])
            debut = phrase_debut + m.start() if m else phrase_debut
    return _normaliser_citation(texte[debut:fin]).strip(" ,;:") in sources


def recommandation_de_sortie(texte: str, sources: str = "") -> str | None:
    """Renvoie la tournure de feu vert / feu rouge trouvée dans le texte du LLM, ou None.

    sources : textes fournis à Claude. Ce que le bulletin ou le guide disent eux-mêmes reste permis (choix de
    l'utilisateur, 07/10/2026) : une tournure citée mot pour mot, ou un qualificatif de risque moindre qu'ils emploient.
    """
    sources_norm = _normaliser_citation(sources)
    for m in RE_FEU.finditer(texte):
        if not (sources and _est_citation(texte, m.start(), m.end(), sources_norm)):
            return m.group(0)
    for m in RE_RISQUE_MOINDRE.finditer(texte):
        if _normaliser_citation(m.group(0)) not in sources_norm:
            return m.group(0)
    return None


MOTIF_NIVEAU = re.compile(r"(?:risque|niveau|indice)[^.\d]{0,20}?\b([1-5])\b", re.I)


def niveaux_cites_hors_faits(texte: str, autorises: set[int]) -> set[int]:
    """Niveaux de risque mentionnés par le LLM qui ne figurent pas dans les faits SQL."""
    return {int(n) for n in MOTIF_NIVEAU.findall(texte)} - autorises


# --- 4. Rédaction avec citations -------------------------------------------

CONSIGNES_REPONSE = """Tu es l'assistant avalanche d'un club de ski. Tu réponds en français, de façon concise, à partir des seuls documents fournis (bulletins Météo-France ou guide avalanche Météo-France).

Règle n° 1, prioritaire sur tout le reste : tu ne donnes jamais de feu vert ni de feu rouge.
- Ne qualifie jamais une pente, une orientation, un itinéraire ou une sortie de sûr, stable, sans danger, « moins dangereux » ou recommandé, et ne les classe pas du plus au moins dangereux si le bulletin ne le fait pas.
- Ne recommande jamais d'y aller, de sortir, de renoncer ou de rester : la décision appartient à l'utilisateur.
- Tu peux décrire le niveau de danger et rapporter ce que dit le bulletin ou un conseil du guide, en indiquant sa source.
- Si on te demande « je peux y aller ? » ou « quelle est la pente la moins dangereuse ? », rapporte ce que le bulletin dit des pentes et des orientations, sans conclure à la place de l'utilisateur.

Règles :
- Le correspondant entre lieu demandé et massif t'est indiqué avant la question : c'est le bon bulletin, ne le remets pas en cause.
- N'utilise que les informations des documents. Ne cite aucune station ou lieu qui n'y figure pas. Si elles ne permettent pas de répondre, dis-le clairement (« le bulletin ne le précise pas »). N'invente rien.
- N'ajoute aucune déduction, explication causale, généralisation ou comparaison que le document ne contient pas. Reformule au plus près du texte ; dans le doute, cite-le entre guillemets.
- Chaque document indique le jour qu'il décrit. Réponds sur le jour demandé ; n'utilise la tendance du lendemain que si la question porte sur le lendemain ou l'évolution, en précisant qu'il s'agit du lendemain.
- Les niveaux de risque, altitudes et orientations sont déjà affichés à l'utilisateur au-dessus de ta réponse : ne les répète pas, explique plutôt le pourquoi (types de départs, couches fragiles, évolution, conseils du bulletin).
- Explique le pourquoi en 3 à 6 phrases, en t'appuyant d'abord sur la stabilité du manteau neigeux.
- Si les faits affichés indiquent une évolution du risque dans la journée, explique-la.
- Si la question contient une affirmation (« c'était plutôt faible ? »), confirme-la ou corrige-la d'après les faits affichés.
- Les définitions du guide ne servent qu'à expliquer les termes du bulletin, pas à décrire la situation du jour.
- Tu ne traites que le massif indiqué dans le contexte : n'emprunte rien au bulletin d'un autre massif.
- Ne donne jamais de feu vert ou de feu rouge pour une sortie."""


def _document(titre: str, texte: str) -> dict:
    return {
        "type": "document",
        "source": {"type": "text", "media_type": "text/plain", "data": texte},
        "title": titre,
        "citations": {"enabled": True},
    }


@dataclass
class Explication:
    texte: str
    sources: list[str]
    appel: dict


def rediger(client: anthropic.Anthropic, question: str, documents: list[dict], contexte: str = "") -> Explication:
    reponse = client.messages.create(
        model=MODELE_REPONSE,
        max_tokens=2048,
        system=CONSIGNES_REPONSE,
        messages=[{"role": "user", "content": [*documents, {"type": "text", "text": f"{contexte}Question : {question}"}]}],
    )
    if reponse.stop_reason == "refusal":
        return Explication("", [], _appel("redaction", reponse))
    morceaux, sources = [], []
    for bloc in reponse.content:
        if bloc.type != "text":
            continue
        morceaux.append(bloc.text)
        for citation in bloc.citations or []:
            if citation.document_title not in sources:
                sources.append(citation.document_title)
    return Explication("".join(morceaux).strip(), sources, _appel("redaction", reponse))


# --- Orchestration ---------------------------------------------------------

@dataclass
class Reponse:
    type: str
    texte: str
    sources: list[str] = field(default_factory=list)
    classification: Classification | None = None
    alerte: str | None = None
    # Pour l'évaluation et le suivi : ce qui a été utilisé pour répondre.
    bulletins: list[dict] = field(default_factory=list)
    documents: list[dict] = field(default_factory=list)
    appels: list[dict] = field(default_factory=list)


ALERTE_SAISON = "Pas de bulletin plus récent en base : la saison est peut-être terminée."
RAPPEL_PISTES = ("Le bulletin ne concerne pas les pistes balisées et ouvertes, sécurisées par la station : "
                 "il ne vaut qu'en dehors (Guide avalanche Météo-France 2025, p. 10).")
INVITATION_STATION = ("Pour savoir ce qu'il en est aujourd'hui, indique-moi ta station ou ton massif : "
                      "je te donnerai le bulletin du jour.")
TITRES_GUIDE_SAT = {"aucune situation avalancheuse typique prédominante": "aucune situation prédominante"}


def _definitions_situations(bulletins: list[dict]) -> list[dict]:
    """Définitions du guide pour les situations typiques du jour, pour expliquer les termes du bulletin."""
    sections = {s.titre: s for s in lire_sections()}
    noms = dict.fromkeys(n for b in bulletins for n in (b["situation_typique_1_nom"], b["situation_typique_2_nom"]) if n)
    documents = []
    for nom in noms:
        section = sections.get(f"Situation avalancheuse typique : {TITRES_GUIDE_SAT.get(nom, nom)}")
        if section:
            documents.append(_document(f"Guide avalanche Météo-France 2025, p. {section.page} - {section.titre}", section.texte))
    return documents


SECTIONS_SEUIL = ("Le BERA : contenu du bulletin", "Le BERA : limites d'utilisation")


def _guide_seuil_de_decision() -> list[dict]:
    """Guide p. 10 : ne pas décider sur le seul indice chiffré, ni feu vert ni feu rouge (demandes de feu vert)."""
    return [_document(f"Guide avalanche Météo-France 2025, p. {s.page} - {s.titre}", s.texte)
            for s in lire_sections() if s.titre in SECTIONS_SEUIL]


# Réponse attendue à « je peux y aller ? », définie par l'utilisateur (cas s35, 07/10/2026).
CONSIGNE_FEU_VERT = (
    "L'utilisateur demande s'il peut y aller : ne réponds ni oui ni non, et ne reprends pas sa question. "
    "Réponds en trois temps :\n"
    "1. pourquoi le bulletin annonce ce niveau de risque, d'après ce qu'il dit du manteau neigeux ;\n"
    "2. les risques que décrit le bulletin : types de départs, où (altitudes, orientations, terrain), déclencheurs ;\n"
    "3. le niveau de risque ne suffit pas à décider : rapporte ce que dit le guide (p. 10) en le citant.\n"
)


# Cas u1 v3 : Claude reprenait « la moins dangereuse » pour dire que le bulletin ne tranche pas, et l'explication était masquée.
CONSIGNE_PENTES = (
    "La question porte sur les pentes : les orientations non signalées sont déjà affichées. N'emploie pas les mots "
    "« moins dangereux » ou « plus sûr », même pour dire que le bulletin ne classe pas les pentes : explique où et "
    "pourquoi le bulletin situe les risques (orientations, altitudes, terrain).\n"
)


MOTS_RELATIFS = re.compile(r"\b(?:après-demain|apres-demain|aujourd['’]hui|demain|hier|avant-hier)\b", re.I)


def expliciter_dates(question: str, jour: date) -> str:
    """Remplace le mot relatif de la question par la date calculée par le code.

    Claude interprétait « demain » par rapport au bulletin (et prenait la tendance du surlendemain) :
    une date explicite ne laisse rien à interpréter.
    """
    return MOTS_RELATIFS.sub(f"le {_date_fr(jour)}", question)


def _resume_documents(documents: list[dict]) -> list[dict]:
    return [{"titre": d["title"], "texte": d["source"]["data"]} for d in documents]


def stations_du_massif(id_massif: str) -> list[str]:
    return next(stations for i, _, _, stations in MASSIFS if i == id_massif)


def _liste_lieux() -> str:
    return ", ".join(f"{nom} ({', '.join(stations)})" for _, nom, _, stations in MASSIFS)


ORDRE_SECTIONS = ("stabilite", "resume", "qualite_neige", "tendance")


def _textes_bulletin(conn: sqlite3.Connection, b: dict) -> list[dict]:
    """Chaque titre dit quel jour le texte décrit : la tendance porte sur le lendemain du jour de validité."""
    jour = date.fromisoformat(b["date_validite"][:10])
    lendemain = jour + timedelta(days=1)
    source = f"BRA {b['nom_affiche']} émis le {b['date_bulletin'][8:10]}/{b['date_bulletin'][5:7]} à {b['date_bulletin'][11:13]}h"
    titres = {
        "stabilite": f"{source} - stabilité du manteau neigeux, prévision pour le {jour:%d/%m/%Y}",
        "resume": f"{source} - résumé des risques, prévision pour le {jour:%d/%m/%Y}",
        "qualite_neige": f"{source} - qualité de la neige, situation au {jour:%d/%m/%Y}",
        "tendance": f"{source} - tendance pour le {lendemain:%d/%m/%Y} (lendemain, PAS le {jour:%d/%m/%Y})",
    }
    textes = dict(conn.execute("SELECT type_contenu, texte FROM chunks_texte WHERE id_bulletin = ?", (b["id_bulletin"],)))
    return [_document(titres[t], textes[t]) for t in ORDRE_SECTIONS if t in textes]


def repondre(
    question: str,
    chemin_db: str | Path = "data/bra.sqlite",
    dossier_chroma: str | Path = "data/chroma",
    aujourd_hui: date | None = None,
    client: anthropic.Anthropic | None = None,
) -> Reponse:
    client = client or anthropic.Anthropic()
    aujourd_hui = aujourd_hui or date.today()
    c, appel = classer(client, question)
    appels = [appel]

    if c.type == "hors_sujet":
        return Reponse("hors_sujet", "Je réponds uniquement aux questions sur la neige et le risque d'avalanche.", classification=c, appels=appels)
    if c.type == "ambigue":
        return Reponse("precision", c.precision_a_demander or "Peux-tu préciser ta question ?", classification=c, appels=appels)

    if c.type == "generale":
        passages = rechercher_guide(question, dossier_chroma, k=3)
        documents = [_document(f"Guide avalanche Météo-France 2025, p. {p['page']} - {p['titre']}", p["texte"]) for p in passages]
        if c.demande_feu_vert:
            titres = {d["title"] for d in documents}
            documents += [d for d in _guide_seuil_de_decision() if d["title"] not in titres]
        explication = rediger(client, question, documents)
        sources_texte = "\n".join(d["source"]["data"] for d in documents)
        texte = MASQUE_FEU if recommandation_de_sortie(explication.texte, sources_texte) else explication.texte
        texte += f"\n\n{INVITATION_STATION}" if c.lien_conditions_du_jour else ""
        return Reponse("generale", texte, explication.sources, c,
                       documents=_resume_documents(documents), appels=[*appels, explication.appel])

    # Question sur un bulletin : lieu obligatoire.
    lieu, inconnu = resoudre_lieux(c.lieux)
    if lieu is None:
        prefixe = f"Je ne connais pas « {inconnu} ». " if inconnu else ""
        return Reponse("precision", f"{prefixe}Pour quel massif ou quelle station ? Couverts : {_liste_lieux()}.", classification=c, appels=appels)
    if len(lieu.massifs) > 2:
        return Reponse("precision", "Je compare au plus deux massifs à la fois : lesquels t'intéressent ?", classification=c, appels=appels)

    conn = connecter(chemin_db)
    try:
        expression = ExpressionDate(**c.date.model_dump()) if c.date else None
        bulletins, series, alerte = [], [], None
        for id_massif in lieu.massifs:
            try:
                jour = resoudre_date(expression, aujourd_hui, dernier_jour_disponible(conn, id_massif, aujourd_hui))
            except DateInvalide:
                return Reponse("precision", "Je n'ai pas compris la date. Peux-tu la préciser (par exemple « le 12 février ») ?", classification=c, appels=appels)
            jour_question = jour
            if jour and expression is not None and c.intention == "observation":
                # Ce qui s'est passé un jour J (neige tombée...) est observé dans le bulletin émis ce soir-là, valable J+1.
                jour += timedelta(days=1)
            b = bulletin_du_jour(conn, id_massif, jour) if jour else None
            if b is None:
                dernier = dernier_jour_disponible(conn, id_massif, aujourd_hui)
                suggestion = f" Le dernier bulletin disponible est valable le {_date_fr(dernier)}." if dernier else ""
                return Reponse("absent", f"Aucun bulletin pour ce massif le {_date_fr(jour)}.{suggestion}", classification=c, appels=appels)
            bulletins.append(b)
            if c.intention == "evolution":
                series.append(evolution_risque(conn, id_massif, jour))
        if expression is None and date.fromisoformat(bulletins[0]["date_validite"][:10]) < aujourd_hui:
            alerte = ALERTE_SAISON

        avec_non_signalees = c.question_sur_pentes
        faits = "\n\n".join(formater_faits(b, avec_non_signalees) for b in bulletins)
        if lieu.comparaison:
            faits += "\n\n" + comparer_faits(*bulletins)
        if series:
            for b, serie in zip(bulletins, series):
                faits += f"\n\nÉvolution {b['nom_affiche']} (risque maximal observé) : " + ", ".join(
                    f"{d.day:02d}/{d.month:02d} : {n}" for d, n in serie)
        guide_feu_vert = _guide_seuil_de_decision() if c.demande_feu_vert else []
        documents_par_bulletin = [_textes_bulletin(conn, b) + _definitions_situations([b]) + guide_feu_vert
                                  for b in bulletins]
    finally:
        conn.close()

    jour_demande = date.fromisoformat(bulletins[0]["date_validite"][:10])
    if jour_question != jour_demande:
        # Question d'observation : le jour demandé est la veille du bulletin utilisé.
        quand = (f"La question porte sur ce qui s'est passé le {_date_fr(jour_question)} : c'est observé dans "
                 f"le bulletin émis ce jour-là, valable le {_date_fr(jour_demande)}.")
    else:
        quand = (f"La question porte sur le {_date_fr(jour_demande)} : le bulletin fourni est la prévision "
                 f"valable ce jour-là.")
    question_datee = expliciter_dates(question, jour_question) if expression and expression.decalage_jours is not None else question
    autorises = niveaux_autorises(bulletins, series)
    utilises = [{k: b[k] for k in ("id_massif", "date_bulletin", "date_validite", "risque_maxi")} for b in bulletins]
    type_reponse = "comparaison" if lieu.comparaison else "bulletin"

    # Une rédaction par massif : un seul texte pour deux bulletins mélangeait leurs contenus (cas Vars / Risoul).
    parties, sources, tous_documents = [], [], []
    for b, documents in zip(bulletins, documents_par_bulletin):
        tous_documents += documents
        lendemain = jour_demande + timedelta(days=1)
        # Repères datés : sans eux, Claude prend la tendance du lendemain pour le jour demandé.
        repere = f"le {_date_fr(jour_demande)} (jour demandé) risque {_niveau(b['risque_maxi'])}"
        if b["risque_maxi_j2"]:
            repere += f" ; le {_date_fr(lendemain)} (jour suivant, tendance du bulletin) risque {_niveau(b['risque_maxi_j2'])}"
        non_signalees = _orientations_non_signalees(b)
        if b.get("pentes_renseignees") and b["pentes_exposees"] and non_signalees:
            repere += (f"\n- Orientations non signalées comme les plus dangereuses : {', '.join(non_signalees)} "
                       f"(cela ne signifie pas qu'elles sont sûres)")
        if lieu.comparaison:
            # Repère supprimé par erreur en v2 : sans lui, Claude écrivait que le Queyras « ne concerne pas » Vars et Risoul.
            autre = next(x for x in bulletins if x is not b)
            stations = ", ".join(s for s in stations_du_massif(b["id_massif"]) if s in stations_du_massif(autre["id_massif"]))
            repere += (f"\n- {stations} dépendent de deux massifs, {b['nom_affiche']} et {autre['nom_affiche']} : "
                       f"ce bulletin s'applique bien à ces stations. La comparaison des niveaux est déjà affichée : "
                       f"ne conclus pas toi-même lequel est le plus risqué.")
        contexte = (
            f"Lieu demandé : {lieu.libelle}. Tu rédiges uniquement la partie consacrée au massif {b['nom_affiche']}"
            + (", l'autre massif est traité à part" if lieu.comparaison else "") + ".\n"
            f"Nous sommes le {_date_fr(aujourd_hui)}. {quand}\n"
            f"Repères {b['nom_affiche']} :\n- {repere}\n"
            f"Faits déjà affichés à l'utilisateur (base de données, exacts) :\n{formater_faits(b, avec_non_signalees)}\n\n"
            + (CONSIGNE_PENTES if avec_non_signalees else "")
            + (CONSIGNE_FEU_VERT if c.demande_feu_vert else "")
        )
        if lieu.comparaison:
            # Avec un seul bulletin sous les yeux, Claude répondait « je ne peux pas comparer » (cas s15 v3) :
            # la comparaison est faite par le code, Claude explique son massif.
            question_massif = (f"Que dit le bulletin {b['nom_affiche']} pour le {_date_fr(jour_demande)} : "
                               f"pourquoi ce niveau de risque, et quels risques décrit-il ?")
        else:
            question_massif = question_datee
        explication = rediger(client, question_massif, documents, contexte)
        appels.append(explication.appel)
        if niveaux_cites_hors_faits(explication.texte, autorises):
            texte_massif = MASQUE_NIVEAU
        elif recommandation_de_sortie(explication.texte, "\n".join(d["source"]["data"] for d in documents)):
            texte_massif = MASQUE_FEU + ("" if avec_non_signalees else orientations_selon_bulletin(b))  # déjà dans les faits
        else:
            texte_massif = re.sub(r"^\s*#+[^\n]*\n+", "", explication.texte)  # titre ajouté par Claude, déjà affiché
            sources += [s for s in explication.sources if s not in sources]
        if texte_massif:
            parties.append(f"**{b['nom_affiche']}** — {texte_massif}" if lieu.comparaison else texte_massif)

    texte = "\n\n".join([faits, *parties])
    if c.question_sur_pistes:
        texte += f"\n\n{RAPPEL_PISTES}"
    return Reponse(type_reponse, texte, sources, c, alerte, utilises, _resume_documents(tous_documents), appels)


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 1
    r = repondre(" ".join(sys.argv[1:]))
    if r.alerte:
        print(f"⚠ {r.alerte}\n")
    print(r.texte)
    if r.sources:
        print("\nSources :\n" + "\n".join(f"- {s}" for s in r.sources))
    if r.type in ("bulletin", "comparaison", "generale"):
        print(f"\n{AVERTISSEMENT}")
    print(f"\n[type : {r.type}]", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
