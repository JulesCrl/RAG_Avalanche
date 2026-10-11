"""Indexation dans Chroma (embedding + stockage) et recherche sémantique.

Deux collections :
- bra_chunks      : les chunks des bulletins (table SQLite chunks_texte), filtrés par massif et date ;
- guide_avalanche : les sections du guide Météo-France, pour les questions générales.

L'indexation est incrémentale : seuls les documents nouveaux ou modifiés sont ré-encodés,
ceux qui ont disparu de la source sont supprimés.

Usage : uv run python -m bra.index [--db data/bra.sqlite] [--chroma data/chroma]
"""
from __future__ import annotations

import argparse
import hashlib
import re
import sqlite3
import sys
from datetime import date
from functools import lru_cache
from pathlib import Path
from typing import Protocol

import chromadb

from .guide import CHEMIN_GUIDE, SOURCE, lire_sections

MODELE = "intfloat/multilingual-e5-base"
COLLECTION = "bra_chunks"
COLLECTION_GUIDE = "guide_avalanche"
MAX_TOKENS = 500  # limite du modèle : 512, préfixe et tokens spéciaux compris
TAILLE_LOT = 64


class Encodeur(Protocol):
    nom: str

    def compter_tokens(self, texte: str) -> int: ...
    def encoder_passages(self, textes: list[str]) -> list[list[float]]: ...
    def encoder_question(self, question: str) -> list[float]: ...


class EncodeurE5:
    """multilingual-e5 attend les préfixes 'passage: ' (documents) et 'query: ' (questions)."""

    nom = MODELE

    def __init__(self):
        from sentence_transformers import SentenceTransformer

        self.modele = SentenceTransformer(MODELE)

    def compter_tokens(self, texte: str) -> int:
        return len(self.modele.tokenizer("passage: " + texte)["input_ids"])

    def encoder_passages(self, textes: list[str]) -> list[list[float]]:
        vecteurs = self.modele.encode(["passage: " + t for t in textes], normalize_embeddings=True)
        return vecteurs.tolist()

    def encoder_question(self, question: str) -> list[float]:
        return self.modele.encode("query: " + question, normalize_embeddings=True).tolist()


@lru_cache(maxsize=1)
def encodeur_par_defaut() -> EncodeurE5:
    return EncodeurE5()


# --- Découpage des chunks trop longs ---------------------------------------

def decouper(texte: str, compter_tokens, max_tokens: int = MAX_TOKENS) -> list[str]:
    """Découpe aux paragraphes (puis aux phrases si besoin) pour rester sous la limite du modèle."""
    if compter_tokens(texte) <= max_tokens:
        return [texte]
    morceaux = []
    for paragraphe in (p.strip() for p in texte.split("\n") if p.strip()):
        if compter_tokens(paragraphe) <= max_tokens:
            morceaux.append(paragraphe)
        else:
            morceaux.extend(re.split(r"(?<=[.!?])\s+", paragraphe))
    parties, courante = [], ""
    for morceau in morceaux:
        candidate = f"{courante}\n{morceau}" if courante else morceau
        if courante and compter_tokens(candidate) > max_tokens:
            parties.append(courante)
            courante = morceau
        else:
            courante = candidate
    parties.append(courante)
    return parties


# --- Indexation ------------------------------------------------------------

def _empreinte(texte: str) -> str:
    return hashlib.sha1(texte.encode()).hexdigest()


def documents_attendus(conn: sqlite3.Connection, encodeur: Encodeur) -> dict[str, tuple[str, dict]]:
    """id Chroma -> (texte, métadonnées) pour tous les chunks de la base SQLite."""
    documents = {}
    lignes = conn.execute(
        """SELECT c.id_chunk, c.texte, c.type_contenu, b.id_bulletin, b.id_massif,
                  b.date_bulletin, b.date_validite
           FROM chunks_texte c JOIN bulletins b USING (id_bulletin)"""
    )
    for id_chunk, texte, type_contenu, id_bulletin, id_massif, date_bulletin, date_validite in lignes:
        parties = decouper(texte, encodeur.compter_tokens)
        jour = date_validite[:10]
        for i, partie in enumerate(parties, start=1):
            id_doc = id_chunk if len(parties) == 1 else f"{id_chunk}#{i}"
            documents[id_doc] = (
                partie,
                {
                    "id_chunk": id_chunk,
                    "partie": i,
                    "nb_parties": len(parties),
                    "id_bulletin": id_bulletin,
                    "id_massif": id_massif,
                    "type_contenu": type_contenu,
                    "date_bulletin": date_bulletin,
                    "date_validite": jour,
                    # Chroma ne compare que des nombres : la date en entier permet les filtres de période.
                    "jour_validite": int(jour.replace("-", "")),
                    "empreinte": _empreinte(partie),
                },
            )
    return documents


def documents_guide(chemin: str | Path, encodeur: Encodeur) -> dict[str, tuple[str, dict]]:
    """id Chroma -> (texte, métadonnées) pour les sections du guide. Le titre est encodé avec le texte."""
    documents = {}
    for section in lire_sections(chemin):
        parties = decouper(f"{section.titre}\n{section.texte}", encodeur.compter_tokens)
        for i, partie in enumerate(parties, start=1):
            id_doc = section.id_section if len(parties) == 1 else f"{section.id_section}#{i}"
            documents[id_doc] = (
                partie,
                {"source": SOURCE, "titre": section.titre, "page": section.page, "empreinte": _empreinte(partie)},
            )
    return documents


def ouvrir_collection(dossier_chroma: str | Path, encodeur: Encodeur, nom: str = COLLECTION):
    client = chromadb.PersistentClient(path=str(dossier_chroma))
    collection = client.get_or_create_collection(
        nom, metadata={"hnsw:space": "cosine", "modele": encodeur.nom}
    )
    if collection.metadata.get("modele") != encodeur.nom:
        raise SystemExit(
            f"Collection encodée avec {collection.metadata.get('modele')}, pas {encodeur.nom} : "
            f"supprimer {dossier_chroma} et réindexer."
        )
    return collection


def synchroniser(collection, attendus: dict[str, tuple[str, dict]], encodeur: Encodeur) -> dict:
    """Aligne la collection sur `attendus` en n'encodant que ce qui a changé."""
    existants = collection.get(include=["metadatas"])
    empreintes = {i: m["empreinte"] for i, m in zip(existants["ids"], existants["metadatas"])}

    a_supprimer = [i for i in empreintes if i not in attendus]
    a_encoder = [i for i, (_, meta) in attendus.items() if empreintes.get(i) != meta["empreinte"]]

    if a_supprimer:
        collection.delete(ids=a_supprimer)
    for debut in range(0, len(a_encoder), TAILLE_LOT):
        lot = a_encoder[debut : debut + TAILLE_LOT]
        textes = [attendus[i][0] for i in lot]
        collection.upsert(
            ids=lot,
            documents=textes,
            embeddings=encodeur.encoder_passages(textes),
            metadatas=[attendus[i][1] for i in lot],
        )
        print(f"  {min(debut + TAILLE_LOT, len(a_encoder))}/{len(a_encoder)} encodés", file=sys.stderr)

    return {"encodes": len(a_encoder), "supprimes": len(a_supprimer), "total": collection.count()}


def indexer(chemin_db: str | Path, dossier_chroma: str | Path, encodeur: Encodeur | None = None) -> dict:
    encodeur = encodeur or encodeur_par_defaut()
    conn = sqlite3.connect(chemin_db)
    attendus = documents_attendus(conn, encodeur)
    conn.close()
    return synchroniser(ouvrir_collection(dossier_chroma, encodeur), attendus, encodeur)


def indexer_guide(
    dossier_chroma: str | Path, chemin_guide: str | Path = CHEMIN_GUIDE, encodeur: Encodeur | None = None
) -> dict:
    encodeur = encodeur or encodeur_par_defaut()
    attendus = documents_guide(chemin_guide, encodeur)
    return synchroniser(ouvrir_collection(dossier_chroma, encodeur, COLLECTION_GUIDE), attendus, encodeur)


# --- Recherche -------------------------------------------------------------

def rechercher(
    question: str,
    dossier_chroma: str | Path,
    id_massif: str | None = None,
    debut: date | None = None,
    fin: date | None = None,
    types_contenu: list[str] | None = None,
    k: int = 4,
    encodeur: Encodeur | None = None,
) -> list[dict]:
    """Recherche sémantique. Les filtres (massif, période de validité, type) s'appliquent avant la similarité."""
    encodeur = encodeur or encodeur_par_defaut()
    conditions = []
    if id_massif:
        conditions.append({"id_massif": id_massif})
    if debut:
        conditions.append({"jour_validite": {"$gte": int(f"{debut:%Y%m%d}")}})
    if fin:
        conditions.append({"jour_validite": {"$lte": int(f"{fin:%Y%m%d}")}})
    if types_contenu:
        conditions.append({"type_contenu": {"$in": types_contenu}})
    where = None if not conditions else conditions[0] if len(conditions) == 1 else {"$and": conditions}

    resultats = ouvrir_collection(dossier_chroma, encodeur).query(
        query_embeddings=[encodeur.encoder_question(question)],
        n_results=k,
        where=where,
        include=["documents", "metadatas", "distances"],
    )
    return [
        {"id": i, "texte": texte, "distance": distance, **meta}
        for i, texte, meta, distance in zip(
            resultats["ids"][0], resultats["documents"][0], resultats["metadatas"][0], resultats["distances"][0]
        )
    ]


def rechercher_guide(
    question: str, dossier_chroma: str | Path, k: int = 3, encodeur: Encodeur | None = None
) -> list[dict]:
    """Recherche dans le guide Météo-France (questions générales, sans massif ni date)."""
    encodeur = encodeur or encodeur_par_defaut()
    resultats = ouvrir_collection(dossier_chroma, encodeur, COLLECTION_GUIDE).query(
        query_embeddings=[encodeur.encoder_question(question)],
        n_results=k,
        include=["documents", "metadatas", "distances"],
    )
    return [
        {"id": i, "texte": texte, "distance": distance, **meta}
        for i, texte, meta, distance in zip(
            resultats["ids"][0], resultats["documents"][0], resultats["metadatas"][0], resultats["distances"][0]
        )
    ]


def main() -> int:
    parseur = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parseur.add_argument("--db", type=Path, default=Path("data/bra.sqlite"))
    parseur.add_argument("--chroma", type=Path, default=Path("data/chroma"))
    parseur.add_argument("--guide", type=Path, default=CHEMIN_GUIDE)
    ns = parseur.parse_args()
    bilans = [(COLLECTION, indexer(ns.db, ns.chroma))]
    if ns.guide.exists():
        bilans.append((COLLECTION_GUIDE, indexer_guide(ns.chroma, ns.guide)))
    else:
        print(f"{ns.guide} absent : guide non indexé, les questions générales ne trouveront aucun passage")
    for nom, bilan in bilans:
        print(f"{nom} : {bilan['encodes']} encodé(s), {bilan['supprimes']} supprimé(s), {bilan['total']} au total")
    return 0


if __name__ == "__main__":
    sys.exit(main())
