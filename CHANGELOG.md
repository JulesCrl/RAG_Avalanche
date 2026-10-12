# Historique des versions

Les versions baseline à v3 ont été développées avant la publication du dépôt, puis regroupées en un seul commit
(`9224de9`, tag `v3`). Leur code intermédiaire n'est pas conservé. Ce qui l'est : les notes de chaque réponse
évaluée ([`evals/assistant/resultats/`](evals/assistant/resultats/)), d'où se recalculent les scores ci-dessous
(`uv run python evals/assistant/historique.py recalculer`). Depuis, chaque modification fait l'objet d'un commit.

Toutes les versions sont notées par le même juge (Claude Sonnet 5.5, consignes v3) sur les 48 questions de
[`evals/assistant/cas.json`](evals/assistant/cas.json). Intervalles de confiance à 95 % par bootstrap sur les questions.

## Après v3 (non évalué)

- **Garde-fou sur les nombres** : une explication qui contient un nombre absent des documents, du contexte et de la
  question est masquée. Rejoué sur les réponses enregistrées, il ne masque aucune réponse v3.
- **Intervalle de confiance** calculé par bootstrap sur les 48 questions au lieu de Wilson sur 144 réponses
  supposées indépendantes : v3 passe de 76-88 % à 74-90 %.
- **Option `--cas`** de l'évaluation, pour un jeu de questions caché.
- **Indexation des bulletins dans Chroma** rendue optionnelle (`--bulletins`) : l'assistant ne l'utilise pas.
- README corrigé là où il affirmait plus que le code ne garantit.

## v3 — 07/10/2026 · 82,6 % (IC 74-90 %), 3 passages

- Questions sur les pentes : le code affiche les orientations que le bulletin ne signale pas, plutôt que de laisser
  Claude désigner une pente « moins dangereuse » (cas u1).
- Comparaison de deux massifs (Vars, Risoul) conclue par le code ; Claude explique chaque massif séparément (cas s15).
- Demandes de feu vert : réponse structurée en trois temps (pourquoi ce niveau, risques décrits, guide p. 10) (cas s35).
- Garde-fou feu vert contextuel : une tournure citée mot pour mot du bulletin reste permise (cas s25).

Résultat : les cas visés passent de 1 à 7 réussites sur 9, mais s14 et s34 régressent (de 6 à 1 réussite sur 6).
L'écart avec v2 reste dans l'intervalle de confiance.

## v2 — 05/10/2026 · 78,5 % (IC 69-88 %), 3 passages

- Garde-fou feu vert / feu rouge en code (expressions régulières), en plus de la consigne.
- Une rédaction par massif : un seul texte pour deux bulletins mélangeait leurs contenus.
- Consignes de fidélité renforcées ; intention « observation » restreinte aux faits physiques passés.
- Premier passage en 3 répétitions par question.

## v1 — 05/10/2026 · 68,8 % (IC 54-81 %), 1 passage

- Surnoms de stations, questions qui citent plusieurs lieux.
- Classification revue ; « demain », « hier » remplacés par une date explicite avant l'envoi à Claude.
- Rappels ajoutés par le code (pistes balisées, invitation à préciser la station).

Mesurée sur un seul passage : l'écart avec la baseline (3 questions) n'est pas significatif.

## baseline — 05/10/2026 · 62,5 % (IC 48-77 %), 1 passage

Première version complète : classification, résolution du lieu et de la date par le code, faits SQL, rédaction avec
citations.
