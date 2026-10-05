# SPEC — Plateforme de candidatures CDI Singapour

> Spécification de référence du projet. Claude Code : lis-la en entier avant toute action, puis travaille **phase par phase** (section 13). Une phase n'est terminée que lorsque ses critères d'acceptation sont remplis.

---

## 1. Objectif

Je suis jeune diplômé ingénieur et je cherche un CDI à Singapour. Je veux une **plateforme web personnelle, hébergée sur mon serveur**, qui :

1. **collecte** des offres d'emploi automatiquement depuis des sources autorisées, et accepte aussi une offre que je colle (lien ou texte) ;
2. **analyse** chaque offre : extraction structurée, compatibilité visa (Employment Pass), score de pertinence par rapport à mon profil, forces et lacunes ;
3. me laisse **décider** : postuler ou ignorer ;
4. **génère** pour les offres retenues un **CV adapté** et une **lettre de motivation adaptée** (PDF, en anglais, lisibles par les ATS), que je relis, modifie et régénère si besoin ;
5. **suit** toutes mes candidatures : statuts, dates, relances dues, notes, documents envoyés.

C'est un outil **human-in-the-loop** et mono-utilisateur : la plateforme prépare, je décide. Elle ne postule jamais à ma place.

## 2. Règles non négociables

- **Zéro invention.** Le CV et la lettre n'utilisent que des faits présents dans ma base de connaissances (`data/profile.yaml`). Chaque élément généré référence l'`id` de sa source, et un validateur le vérifie (section 8.3).
- **Sources autorisées uniquement.** Aucune collecte automatisée sur les sites dont les conditions d'utilisation l'interdisent (LinkedIn, Indeed, Glassdoor…). La collecte automatique passe par des API publiques, des flux RSS et les emails d'alertes que je reçois (section 6). Un **lien collé à la main** est récupéré une seule fois, à ma demande, quel que soit le site ; si la page est bloquée, je colle le texte.
- **Aucune action en mon nom.** Pas de candidature automatique, pas d'envoi de message ou d'email.
- **Accès protégé.** La plateforme est exposée sur un serveur : authentification obligatoire, HTTPS via reverse proxy, secrets uniquement dans `.env` (jamais commité, `.env.example` fourni).
- **Données personnelles locales.** Profil, base SQLite et documents générés restent dans `data/` (ignoré par git). Seuls les appels à l'API Anthropic sortent du serveur, sans mes coordonnées quand elles ne sont pas nécessaires.
- **Coûts maîtrisés.** Filtre gratuit avant tout appel LLM sur les offres collectées automatiquement, et budget LLM journalier configurable.
- **Seuils visa en configuration**, jamais en dur (ils changent chaque année).

## 3. Parcours utilisateur

```
          Sources automatiques                 Ajout manuel
   (API ATS, RSS, alertes email)          (lien ou texte collé)
                    │                               │
                    ▼                               │
          Pré-filtre gratuit                        │
   (lieu, titre, doublons, budget)                  │
                    └───────────────┬───────────────┘
                                    ▼
             Analyse : extraction → visa → score → avis
                                    ▼
                  Boîte de réception triée par score
                                    ▼
                 Je décide : [Postuler]  ou  [Ignorer]
                                    ▼
              Génération du CV + lettre → validation anti-invention
                                    ▼
          Je relis, j'édite, je régénère → je télécharge les PDF
                                    ▼
          Je postule moi-même → [Marquer comme envoyée]
                                    ▼
          Suivi : relances, entretiens, réponse finale
```

## 4. Environnement et stack

- **Dev** : Ubuntu sous WSL2, VS Code. **Production** : mon serveur Linux, via Docker Compose. Le code reste portable (`pathlib`, aucune commande shell propre à un OS).
- **Python 3.12**, dépendances gérées avec **uv** (`pyproject.toml`).
- **Web** : **FastAPI** + templates **Jinja2** rendus côté serveur + **HTMX** (fichier statique servi localement, pas de CDN) pour les interactions sans rechargement. Pas de framework JS, pas de build front.
- **Serveur** : **uvicorn**. Sessions signées par cookie (`SessionMiddleware`, dépendance `itsdangerous`), formulaires via `python-multipart`.
- **Données** : **SQLite + SQLModel**. **Pydantic v2** pour tous les modèles et la validation des sorties LLM.
- **LLM** : SDK **Anthropic** (`anthropic`), modèle défini par `ANTHROPIC_MODEL` dans `.env` (défaut `claude-opus-5-5`). Sorties JSON contraintes par schéma, validées par Pydantic, avec retry (max 2).
- **PDF** : **Typst** via le package Python `typst` ; relecture avec **pypdf** (test ATS).
- **Récupération web** : **httpx** + **trafilatura**.
- **Tâches de fond** : boucle asyncio dans le cycle de vie de l'application (collecte périodique, file d'analyse). Pas de broker externe.
- **CLI d'administration** : **Typer**.
- **Qualité** : pytest (client LLM toujours mocké), ruff, annotations de type partout.

## 5. Architecture et code map

Toute la logique métier est dans `src/jobapply/` et ne dépend pas de l'interface. Les routes web et la CLI ne font qu'appeler `pipeline.py` et les services.

```
application-assistant/
├── SPEC.md
├── pyproject.toml / uv.lock
├── .env.example                # ANTHROPIC_*, APP_PASSWORD_HASH, SESSION_SECRET
├── Dockerfile, compose.yaml    # phase 5
├── DEPLOY.md                   # procédure de déploiement et de sauvegarde
├── deploy/Caddyfile            # reverse proxy HTTPS (phase 5) ; nginx.conf.example
├── config/
│   ├── settings.yaml           # LLM, scoring, génération, collecte, budget
│   ├── visa.yaml               # seuils EP / COMPASS (section 9)
│   └── sources.yaml            # sources de collecte (phase 4)
├── prompts/                    # prompts versionnés (front matter `version: N`)
├── templates/                  # Typst : cv.typ, cover_letter.typ (phase 2)
├── data/                       # ignoré par git
│   ├── profile.yaml            # ma base de connaissances (section 7.1)
│   ├── app.db
│   ├── llm_calls.jsonl         # journal des appels LLM (tokens, coût)
│   └── output/<application_id>/  # PDF + JSON générés
├── src/jobapply/
│   ├── config.py               # .env + YAML -> objets Pydantic
│   ├── models/                 # profile, offer, tailored, db
│   ├── llm/                    # client (JSON, retry, coûts), prompts
│   ├── ingest/                 # fetch (URL -> texte), extract (texte -> JobOffer)
│   ├── sources/                # collecteurs : greenhouse, lever, ashby, rss, email (phase 4)
│   ├── visa/checker.py
│   ├── matching/scorer.py
│   ├── generate/               # cv, cover_letter, validator, render (phase 2)
│   ├── ats/check.py            # phase 2
│   ├── tracking/service.py     # offres, candidatures, statuts, relances
│   ├── pipeline.py             # orchestration : ajout, analyse, génération
│   ├── worker.py               # tâches de fond (phase 4)
│   ├── cli.py                  # administration
│   └── web/
│       ├── app.py              # FastAPI : création de l'app, middlewares, cycle de vie
│       ├── auth.py             # mot de passe unique, session
│       ├── routes/             # pages et actions HTMX
│       ├── templates/          # Jinja2
│       └── static/             # htmx.min.js, style.css
└── tests/
    ├── fixtures/               # profil fictif + offres d'exemple + JSON attendus
    └── test_*.py
```

## 6. Sources d'offres

| Source | Mode | Phase |
|---|---|---|
| Lien collé (tout site) | Récupération unique à ma demande ; LinkedIn compris. Texte collé en secours. | 1 |
| Texte collé | Direct | 1 |
| API publiques d'ATS : Greenhouse, Lever, Ashby | Liste d'entreprises cibles dans `config/sources.yaml`, collecte périodique | 4 |
| Flux RSS d'offres | URL de flux dans `config/sources.yaml` | 4 |
| Emails d'alertes (LinkedIn, JobStreet…) reçus sur une boîte dédiée | Lecture IMAP en lecture seule : titre, entreprise, lien. L'analyse complète se fait quand je clique sur « Analyser » (récupération unique). | 6 |

Règles de collecte : User-Agent explicite, une requête à la fois par domaine, respect des codes 429, déduplication (URL normalisée + hash du texte).

**Pré-filtre gratuit** (avant tout appel LLM, offres automatiques uniquement, configuré dans `config/sources.yaml`) :
- lieu contenant un des termes configurés (« Singapore »…) : les offres hors zone ne sont **pas conservées** (une grande entreprise publie des centaines d'offres ailleurs) ; une offre sans lieu indiqué est gardée ;
- titre contenant un mot-clé cible et aucun mot exclu (« Senior », « Manager », « Sales »…), en **mots entiers** (« intern » ne correspond pas à « Internal ») ;
- description d'au moins 200 caractères, sinon l'offre est gardée sans texte et « Réanalyser » récupère sa page une fois ;
- pas de doublon (même identifiant source, même lien normalisé ou même texte).

Les offres écartées par le titre ou la description restent visibles (statut `filtered`, filtre « Filtrée » de la page Offres) avec la raison. Les offres retenues sont analysées par le worker dans la limite de `budget.daily_usd` et de `budget.max_analyses_per_run` ; au-delà, elles restent `new` jusqu'à la collecte suivante. Chaque collecte d'une source est enregistrée (`SourceRun` : offres lues, nouvelles, filtrées, doublons, hors zone, erreur).

## 7. Modèles de données

### 7.1 Base de connaissances (`data/profile.yaml`)

Rédigée à partir de mon CV et des informations que je fournis. Chaque élément réutilisable a un `id` stable (`kebab-case`, unique dans tout le fichier). Le chargement valide strictement le fichier et affiche des erreurs claires.

```yaml
identity:
  name: "..."
  email: "..."
  phone: "..."
  location: "Marseille, France — open to relocate to Singapore"
  links: { linkedin: "...", github: "..." }
  birth_year: 2002            # seuil EP uniquement, jamais affiché

headline_variants:
  - { id: hl-data, text: "...", tags: [data, ml] }

education:
  - id: edu-xxx
    school: "..."
    degree: "..."
    dates: "2022–2026"
    bullets: [ { id: edu-xxx-1, text: "...", tags: [...] } ]

experiences:
  - id: exp-xxx
    title: "..."
    company: "..."
    location: "..."
    dates: "..."
    bullets:
      - { id: exp-xxx-1, text: "Built X that reduced Y by 30%", tags: [python], metrics: true }

projects:
  - id: proj-xxx
    name: "..."
    description: "..."
    bullets: [ { id: proj-xxx-1, text: "...", tags: [...] } ]

skills:
  - { id: sk-python, name: "Python", level: advanced, tags: [dev] }

languages:
  - { name: "French", level: native }
  - { name: "English", level: "C1" }

motivations:                  # briques pour les lettres
  - { id: mot-sg, text: "..." }

constraints:
  min_salary_sgd: 5600
  target_roles: ["Data Engineer", "Software Engineer"]
  available_from: "2026-09"
  years_experience: 0         # temps plein, hors stages
```

### 7.2 Offre structurée (`JobOffer`)

`title`, `company`, `location`, `url`, `raw_text`, `seniority`, `required_skills` (compétences techniques uniquement, nom court), `nice_to_have`, `keywords` (vocabulaire exact de l'offre, pour l'ATS), `responsibilities`, `min_years_experience` (0 si ouvert aux jeunes diplômés, `null` si non précisé), `salary_min_sgd` / `salary_max_sgd` (mensuels ; `null` si absent ou dans une autre devise), `sector` (`financial_services` | `other`), `contact_email`, `language`.

### 7.3 Sorties générées

- `TailoredCV` : `headline_id`, sections ordonnées ; chaque bullet = `{ source_id, text }` ; `skills` (ids) ; `keywords_covered`.
- `CoverLetter` : `subject`, `greeting`, `paragraphs: [{ text, source_ids }]`, `closing`.

### 7.4 Base SQLite

- `Offer` : JobOffer sérialisée, `source` (`manual` | `greenhouse` | `lever` | `ashby` | `rss` | `email`), `external_id`, `url`, `text_hash`, `status`, `filter_reason`, dates.
  - Statuts : `new` → `analyzing` → `analyzed` → `accepted` | `dismissed` ; `filtered` (pré-filtre) ; `error` (avec message).
- `Analysis` : `offer_id`, verdict visa (JSON), score détaillé (JSON), `model`, `prompt_versions`, `cost_usd`, `created_at`. Une offre peut être réanalysée (on garde l'historique).
- `Application` : créée quand j'accepte une offre.
  - Statuts : `preparing` → `ready` → `applied` → `interview` → `offer` | `rejected` | `ghosted` | `withdrawn`.
  - Champs : `applied_at`, `next_follow_up_at`, `notes`.
- `Document` : `application_id`, `kind` (`cv` | `letter`), `path`, `json_payload`, `model`, `prompt_version`, `created_at`, `sent` (version effectivement envoyée).
- `Event` : historique horodaté (changement de statut, génération, note).

## 8. Fonctionnement technique

### 8.1 Analyse d'une offre
1. **fetch** (si lien) → texte brut. Échec ou page vide → statut `error` avec un message invitant à coller le texte.
2. **extract** (LLM, `prompts/extract_offer.md`) → `JobOffer`. Le schéma est injecté depuis Pydantic ; les salaires sont convertis en SGD mensuels, sans supposition.
3. **visa** → verdict (section 9).
4. **score** → score 0–100 (section 8.2).
5. Résultat enregistré dans `Analysis` ; l'offre passe en `analyzed`.

Les analyses lancées depuis l'interface s'exécutent en tâche de fond ; la page se met à jour (HTMX polling) quand elles sont terminées.

### 8.2 Score
- (a) Recouvrement déterministe entre les compétences requises / mots-clés de l'offre et les compétences, tags et textes du profil (70 % compétences requises, 30 % mots-clés).
- (b) Jugement LLM court (`prompts/score_match.md`) : score, 3 forces, 3 lacunes, avis en une phrase. Le profil est envoyé sans identité, coordonnées ni salaire.
- Score final = moyenne pondérée configurable, moins une pénalité par année d'expérience manquante.
- Recommandation affichée : « à poursuivre » ou « déconseillé », avec les raisons (visa incompatible, score sous le seuil). La décision reste la mienne.

### 8.3 Génération et anti-invention (phase 2)
- **CV** (`prompts/select_cv_content.md`) : le LLM reçoit le profil (sans coordonnées) et la `JobOffer`, choisit l'accroche, sélectionne et réordonne les bullets, reformule légèrement avec le vocabulaire de l'offre **sans changer les faits**. Anglais, verbes d'action, 1 page par défaut (2 max), nombre de bullets plafonné (`cv.max_bullets`, `cv.max_bullets_per_entry`), ni photo, ni âge, ni nationalité. Titres, entreprises, écoles, dates et coordonnées ne sont jamais générés : le rendu les reprend du profil.
- **Lettre** (`prompts/write_cover_letter.md`) : 3–4 paragraphes, 250–350 mots (tolérance de 10 %), anglais. Accroche spécifique à l'entreprise, 2 preuves tirées du profil, motivation pour Singapour (briques `motivations`), conclusion. Ton direct, sans formules creuses.
- **Validateur** :
  - chaque `source_id` existe dans le profil ;
  - les chiffres (nombres, %) d'un texte reformulé figurent dans sa source ;
  - les noms d'entreprises, d'écoles et de technologies cités existent dans le profil, ou dans l'offre pour l'entreprise ciblée.

  En cas de rejet, une régénération avec la liste des erreurs, puis échec explicite.
- **Rendu** : JSON → template Typst (une colonne, polices standard) → PDF dans `data/output/<application_id>/`.
- **Contrôle ATS** : relecture pypdf ; texte extractible et dans l'ordre, compétences requises que je possède présentes, nombre de pages respecté.
- **Édition** : je peux modifier le texte de chaque bullet ou paragraphe dans l'interface. Une modification manuelle repasse par le validateur, qui affiche un avertissement mais ne bloque pas : c'est moi qui décide. Puis le PDF est re-rendu.

### 8.4 Suivi (phase 3)
- « Marquer comme envoyée » (date choisie, jamais dans le futur) passe en `applied`, enregistre la date et marque comme envoyées les versions actuelles du CV et de la lettre.
- Relance due `follow_up_days` jours après l'envoi (configurable), puis tous les `follow_up_days` jours tant que la candidature n'a pas avancé.
- Une relance due se traite en « Relance faite » (compteur + prochaine relance) ou « Reporter » (1 à 60 jours). Fixer une date d'entretien passe la candidature en `interview` et suspend les relances.
- Passage automatique en `ghosted` après `ghosted_after_days` jours sans nouvelles depuis le dernier contact (envoi ou relance), uniquement pour les candidatures `applied` (configurable, vérifié à l'ouverture du tableau de bord et des candidatures). Je peux toujours changer le statut à la main, y compris revenir en arrière.
- Notes libres par candidature, et événements ajoutés à la main dans l'historique (appel, email reçu…).

### 8.5 Coûts LLM
Chaque appel est journalisé (tokens d'entrée, de sortie et de cache, coût estimé d'après `settings.yaml`, version du prompt). Le tableau de bord affiche le coût du jour et du mois. La collecte automatique s'arrête d'analyser quand le budget journalier est atteint : les offres restent en `new`.

## 9. Visa (`config/visa.yaml`)

```yaml
# À mettre à jour chaque année depuis le site du MOM.
effective_from: "2026-01-01"
ep:
  min_salary_sgd:
    other: { base: 5600, at_45_plus: 10700 }
    financial_services: { base: 6200, at_45_plus: 11800 }
  upcoming:
    effective_from: "2027-01-01"
    other: { base: 6000 }
    financial_services: { base: 6600 }
  age_curve: { base_until_age: 23, max_from_age: 45 }
  compass_pass_mark: 40
  compass_exempt_salary_sgd: 22500
```

- Le seuil applicable dépend du secteur, de mon âge au démarrage (année de démarrage − année de naissance, valeur prudente) et de la date de démarrage : le plus tard entre `available_from` et aujourd'hui. Les seuils `upcoming` s'appliquent à partir de leur date.
- Âge : interpolation linéaire entre `base` et `at_45_plus`. TODO : remplacer par la table officielle du MOM.
- Verdicts :
  - `incompatible` : salaire max sous le seuil ;
  - `at_risk` : la fourchette chevauche le seuil, ou une seule borne est connue et ne suffit pas à trancher ;
  - `ok` : salaire min au-dessus du seuil ;
  - `unknown` : salaire non indiqué.
- COMPASS : on indique seulement l'exemption salariale. Le score COMPASS n'est pas calculé.

## 10. Interface web

| Page | Contenu |
|---|---|
| Connexion | Mot de passe unique. Session de 30 jours, cookie `HttpOnly`/`Secure`/`SameSite=Lax`, limitation des tentatives. |
| Tableau de bord | Offres à trier, candidatures en cours par statut, relances dues aujourd'hui, coût LLM du jour et du mois. |
| Offres | Liste filtrable (statut, source, verdict visa, score min) triée par score, avec un badge score et un badge visa. |
| Ajouter une offre | Champ lien **ou** zone de texte, puis analyse en tâche de fond et redirection vers le détail. |
| Détail d'une offre | Résumé structuré, verdict visa détaillé, score détaillé (compétences couvertes et manquantes, forces, lacunes, avis), texte d'origine. Actions **Postuler** (crée la candidature et lance la génération du CV et de la lettre), **Ignorer**, **Réanalyser**. |
| Candidature | Aperçu du CV et de la lettre, édition des textes, **Régénérer** (CV ou lettre), téléchargement PDF, **Marquer comme envoyée**, statut, prochaine relance, notes, historique. |
| Candidatures | Tableau de suivi : entreprise, poste, statut, date d'envoi, prochaine relance. |
| Sources (phase 4) | Sources configurées, dernière collecte, nombre d'offres, bouton « Collecter maintenant ». |
| Profil | État de validation de `profile.yaml`, avec les erreurs éventuelles, et résumé du contenu. |

Interface et messages en français. Les documents générés sont en anglais. Mise en page sobre et responsive, lisible sur téléphone.

**Sécurité** : toutes les routes sauf `/login` et `/static` exigent une session. Les formulaires POST sont protégés contre le CSRF (jeton de session). Le mot de passe est stocké en `.env` sous forme de hash scrypt (`jobapply hash-password`). `SESSION_SECRET` est aléatoire et propre à chaque installation.

## 11. CLI d'administration

```
jobapply serve [--host 127.0.0.1] [--port 8000] [--reload]   # lance la plateforme
jobapply hash-password                                      # produit APP_PASSWORD_HASH
jobapply profile check [--path F]                           # valide profile.yaml
jobapply offer add --url URL | --file F                     # ajoute + analyse (debug)
jobapply offer score OFFER_ID [--no-llm]                    # visa + score (debug)
jobapply sources sync                                       # collecte manuelle (phase 4)
jobapply backup [--keep 14]                                 # archive data/ dans data/backups/
```

## 12. Déploiement (phase 5)

Procédure complète dans `DEPLOY.md`.

- `Dockerfile` : image `uv` + Python 3.12 slim, polices Liberation (templates Typst) et tzdata, installation non éditable du package, utilisateur non-root (uid 1000), healthcheck sur `/healthz`, **un seul processus** (les analyses en tâche de fond et la planification de la collecte vivent dedans).
- `compose.yaml` : service `app` (volume `./data`, `config/` en lecture seule, `.env`, port 8000 lié à `127.0.0.1` uniquement, `COOKIE_SECURE` forcé à `true`, en-têtes `X-Forwarded-For` du proxy acceptés pour la limitation des connexions par IP) et service `caddy` (profil `caddy`) qui fournit le HTTPS automatique pour `$DOMAIN`. Exemple nginx fourni pour un serveur qui a déjà un reverse proxy.
- L'image ne contient ni `.env` ni `data/` (`.dockerignore`).
- Sauvegarde : `jobapply backup` (copie SQLite cohérente via l'API de backup, profil, journal LLM, PDF ; rotation des archives), à lancer par cron sur le serveur. La restauration consiste à extraire l'archive dans `data/`, application arrêtée.
- Mise à jour : `git pull && docker compose --profile caddy up -d --build`. Le schéma de la base est complété automatiquement au démarrage.

## 13. Plan de développement

**Phase 0 — Fondations (fait).** Config, profil validé, extraction d'offre (LLM + retry + coûts), visa, score, base SQLite, CLI de debug, tests.

**Phase 1 — Plateforme web minimale (codée, à valider en réel).** FastAPI, authentification, mise en page, ajout d'une offre par lien ou texte, analyse en tâche de fond, liste et détail des offres, Postuler / Ignorer / Réanalyser, page Profil. Les tables passent au modèle de la section 7.4.
*Critère :* avec `jobapply serve`, je me connecte, je colle le lien d'une offre, je vois son analyse complète, et je peux l'accepter ou l'ignorer.

**Phase 2 — CV et lettre (codée, à valider en réel).** Génération, validateur, templates Typst, rendu PDF, contrôle ATS, page Candidature avec aperçu, édition, régénération et téléchargement.
*Critère :* pour une offre acceptée, j'obtiens 2 PDF propres ; un test montre que le validateur rejette un bullet inventé et un chiffre modifié.

**Phase 3 — Suivi (codée, à valider en réel).** Statuts et historique, relances, passage en `ghosted`, tableau de bord, page Candidatures.
*Critère :* tout le cycle de vie d'une candidature se gère depuis l'interface ; les relances dues apparaissent sur le tableau de bord.

**Phase 4 — Collecte automatique (codée, à valider en réel).** Collecteurs Greenhouse, Lever, Ashby et RSS, `config/sources.yaml`, pré-filtre, budget, worker périodique, page Sources.
*Critère :* une collecte sur des entreprises configurées ajoute des offres filtrées et analysées, sans doublon et dans le budget.

**Phase 5 — Déploiement (codée, à valider sur le serveur).** Docker, Compose, Caddy, documentation de sauvegarde.
*Critère :* la plateforme tourne sur mon serveur en HTTPS, derrière l'authentification.

**Phase 6 (optionnelle) — Alertes email.** Lecture IMAP d'une boîte dédiée aux alertes, création de pistes, analyse à la demande.

**Hors périmètre pour l'instant :** préparation d'entretien, messages d'approche, statistiques de taux de réponse, multi-utilisateur.

## 14. Consignes de travail pour Claude Code

- Demande avant d'ajouter une dépendance qui n'est pas listée en section 4.
- Tests écrits en même temps que le code ; ils n'appellent jamais l'API réelle (client LLM mocké) ni de site externe (`httpx.MockTransport`).
- Prompts dans `prompts/` avec un numéro de version, jamais en chaînes dans le code.
- Code et commentaires en anglais ; interface et messages en français.
- En fin de phase : ce qui a été fait, ce qui reste, et les commandes pour tester.
