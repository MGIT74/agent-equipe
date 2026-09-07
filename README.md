# Agent Équipe — Application web multi-employés pour Hermes

Mini-application web qui permet à plusieurs employés de discuter **simultanément**
avec Hermes, chacun sur **son propre profil isolé** (mémoire, skills, historique).

```
Navigateur employé ──> team-app (port 8890) ──> Hermes WebUI (port 8787)
                      (auth par employé,      (routage par profil via
                       proxy isolant)           le cookie hermes_profile)
```

## Fichiers

| Fichier | Rôle |
|---|---|
| `server.py` | Serveur proxy : authentification employés + isolation des profils |
| `manage_users.py` | Administration des comptes (créer, lister, supprimer, mot de passe) |
| `index.html` | Page de connexion |
| `app.html` + `app.js` + `style.css` | Interface de chat (sessions, streaming en direct) |
| `team_users.json` | Comptes employés (créé automatiquement, mots de passe hashés PBKDF2) |
| `team_sessions.json` | Sessions de connexion (tokens, expiration 7 jours) |

## Démarrage

```bash
cd /workspace/team-app
TEAM_ADMIN_PASSWORD='votre-mot-de-passe-admin' python3 server.py
```

Le serveur écoute sur `http://0.0.0.0:8890`.
Au premier lancement, un compte `admin` est créé automatiquement.

**Variables d'environnement :**

| Variable | Défaut | Rôle |
|---|---|---|
| `TEAM_APP_PORT` | `8890` | Port d'écoute |
| `TEAM_APP_HOST` | `0.0.0.0` | Adresse d'écoute |
| `TEAM_ADMIN_PASSWORD` | `changeme` | Mot de passe du compte admin initial |
| `TEAM_UPSTREAM_HOST` / `TEAM_UPSTREAM_PORT` | `127.0.0.1` / `8787` | Adresse du WebUI Hermes |
| `HERMES_WEBUI_PASSWORD` | (héritée) | Mot de passe du WebUI — le proxy se connecte automatiquement |

## Gestion des employés

```bash
# Créer un employé (crée aussi son profil Hermes automatiquement)
python3 manage_users.py add <identifiant> <mot-de-passe> <profil> [nom affiché]

# Exemples
python3 manage_users.py add marie 'S3cret!' marie "Marie Dupont"
python3 manage_users.py add paul  'Paul2026!' paul "Paul Martin"

# Lister, changer un mot de passe, supprimer
python3 manage_users.py list
python3 manage_users.py passwd marie 'NouveauMdp!'
python3 manage_users.py remove marie
```

Chaque employé **doit** avoir son propre profil Hermes (minuscules, chiffres, `-`/`_`).
Le profil est créé automatiquement s'il n'existe pas.

## Comment ça marche

1. L'employé se connecte sur `http://<serveur>:8890` avec ses identifiants.
2. Le proxy vérifie le mot de passe (PBKDF2) et crée une session de connexion (cookie httpOnly, 7 jours).
3. À chaque requête, le proxy injecte le cookie `hermes_profile=<profil de l'employé>`
   vers le WebUI Hermes, qui route la requête vers **le profil de cet employé**
   (mémoire, skills, sessions et clés API du profil).
4. Les listes de sessions sont filtrées par profil ; toute action sur une
   session d'un autre profil renvoie `403 forbidden`.

## Sécurité

- Mots de passe jamais stockés en clair (PBKDF2-SHA256, 300 000 itérations)
- Cookies de connexion httpOnly, expiration 7 jours
- Isolation stricte : un employé ne peut pas lire/modifier/supprimer les
  sessions d'un autre, ni changer de profil
- Le proxy se connecte au WebUI avec le mot de passe d'env (`HERMES_WEBUI_PASSWORD`)
  et se ré-authentifie automatiquement en cas d'expiration

## Limitations connues

- Les attachments de fichiers ne sont pas encore supportés dans l'interface
  (l'agent du profil reste libre d'utiliser ses outils sur le serveur).
- Un tour à la fois par session de chat (l'envoi est bloqué pendant une réponse).
- Pas de HTTPS intégré : en production, placez un reverse proxy (Caddy, nginx)
  avec TLS devant le port 8890.