# Déployer JobApply SG sur ton serveur

La plateforme tourne dans Docker. Caddy fournit le HTTPS automatiquement (certificat Let's Encrypt). Tes données (`data/`) et tes secrets (`.env`) restent sur le serveur, en dehors de l'image.

```
Internet ──443──▶ Caddy (HTTPS) ──▶ app:8000 (FastAPI)
                                        │
                     ./data   (base SQLite, profil, PDF, sauvegardes)
                     ./config (réglages, sources — lecture seule)
```

## 1. Prérequis

- Un serveur Linux avec **Docker** et le plugin **Compose** : `docker compose version` doit répondre.
- Un **nom de domaine** ou sous-domaine (ex. `jobs.mondomaine.fr`) dont l'enregistrement DNS **A** pointe vers l'IP du serveur.
- Les ports **80** et **443** ouverts. Avec ufw : `sudo ufw allow 80,443/tcp`. Laisse le port 8000 fermé : l'application n'écoute que sur `127.0.0.1`.

## 2. Récupérer le code sur le serveur

Le dépôt n'a pas encore de remote. Deux options :

- **Dépôt Git privé** (recommandé, les mises à jour se font avec `git pull`) : pousse le projet sur un dépôt **privé** GitHub ou GitLab, puis `git clone` sur le serveur. `.env` et `data/` sont ignorés par Git : ils ne partent jamais sur GitHub.
- **Copie directe** depuis ton PC :
  ```bash
  rsync -av --exclude .venv --exclude data --exclude .env ./ user@serveur:~/application-assistant/
  ```

## 3. Configurer

Sur le serveur, dans le dossier du projet :

```bash
cp .env.example .env
nano .env
```

| Variable | Valeur |
|---|---|
| `ANTHROPIC_API_KEY` | ta clé API |
| `ANTHROPIC_MODEL` | `claude-opus-5-5` (ou `claude-sonnet-5-5`, moins cher) |
| `APP_PASSWORD_HASH` | sortie de `jobapply hash-password` (voir ci-dessous) |
| `SESSION_SECRET` | une valeur aléatoire **nouvelle**, différente de celle du dev |
| `COOKIE_SECURE` | `true` (Compose le force de toute façon) |
| `DOMAIN` | ton domaine, ex. `jobs.mondomaine.fr` |

Générer le hash du mot de passe et le secret (l'image doit être construite, voir étape 4) :

```bash
docker compose run --rm app jobapply hash-password
python3 -c "import secrets; print(secrets.token_urlsafe(48))"
```

Copie ensuite ton profil, ta base et tes documents depuis ton PC :

```bash
rsync -av data/ user@serveur:~/application-assistant/data/
```

Le conteneur tourne avec l'utilisateur **uid 1000**. Si ton utilisateur sur le serveur a un autre uid (`id -u`), donne-lui le dossier :

```bash
sudo chown -R 1000:1000 data
```

## 4. Lancer

```bash
docker compose --profile caddy up -d --build
docker compose ps                  # "app" doit passer en "healthy"
docker compose logs -f app         # Ctrl+C pour quitter
```

Ouvre `https://ton-domaine`. Le premier accès peut prendre quelques secondes, le temps que Caddy obtienne le certificat. Connecte-toi avec ton mot de passe.

La **collecte automatique** démarre une minute après le lancement, puis toutes les `interval_hours` (6 h par défaut). Les analyses ne dépassent jamais le budget LLM journalier de `config/sources.yaml`.

### Tu as déjà un reverse proxy (nginx…)

Lance l'application seule, sans le profil `caddy`. Elle écoute sur `127.0.0.1:8000`. Un exemple de configuration est fourni dans [deploy/nginx.conf.example](deploy/nginx.conf.example).

```bash
docker compose up -d --build
```

## 5. Modifier les réglages

- **Sources, filtres, budget** : modifie `config/sources.yaml`, puis `docker compose restart app`.
- **Réglages généraux** (relances, scoring…) : `config/settings.yaml`, puis redémarrage.
- **Profil** : modifie `data/profile.yaml`. La prise en compte est immédiate, sans redémarrage.

## 6. Mettre à jour

```bash
git pull
docker compose --profile caddy up -d --build
```

Le schéma de la base est mis à jour automatiquement au démarrage : les nouvelles colonnes sont ajoutées, tes données sont conservées.

## 7. Sauvegardes

`jobapply backup` crée dans `data/backups/` une archive qui contient une copie cohérente de la base, faite même pendant l'utilisation, ainsi que le profil, le journal des coûts et les PDF. Les 14 dernières archives sont conservées.

```bash
docker compose exec app jobapply backup
```

Sauvegarde automatique chaque nuit à 3 h, via `crontab -e` sur le serveur :

```
0 3 * * * cd /home/USER/application-assistant && docker compose exec -T app jobapply backup >> data/backups/cron.log 2>&1
```

Copie régulièrement `data/backups/` **hors du serveur**, par exemple vers ton PC :

```bash
rsync -av user@serveur:~/application-assistant/data/backups/ ./sauvegardes-jobapply/
```

**Restaurer** une archive :

```bash
docker compose stop app
tar -xzf data/backups/jobapply-AAAAMMJJ-HHMMSS.tar.gz -C data/
docker compose start app
```

## 8. Dépannage

| Symptôme | Piste |
|---|---|
| `app` ne démarre pas | `docker compose logs app` : un `APP_PASSWORD_HASH` ou `SESSION_SECRET` manquant est signalé clairement. |
| Pas de certificat HTTPS | Le DNS pointe-t-il vers le serveur ? Les ports 80 et 443 sont-ils ouverts ? Voir `docker compose logs caddy`. |
| `Permission denied` sur `data/` | `sudo chown -R 1000:1000 data` |
| Connexion qui boucle sur la page de login | Le site doit être ouvert en `https://` : le cookie de session est marqué `Secure`. |
| « Trop de tentatives » | 5 échecs en 15 minutes bloquent l'IP concernée. Attends, ou redémarre l'app. |

## Sécurité, en résumé

- L'application n'est jamais exposée directement : seul Caddy (ou ton proxy) écoute sur Internet.
- Mot de passe stocké en hash scrypt, sessions signées de 30 jours, protection CSRF, limitation des tentatives de connexion.
- L'image ne contient ni `.env` ni `data/`.
- Garde ton serveur à jour (`apt upgrade`) et l'accès SSH par clé.
