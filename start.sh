#!/usr/bin/env bash
#
# start.sh — Démarre l'application Agent Équipe en production.
#
# Usage :
#   ./start.sh                    # démarre (arrière-plan, logs dans team-app.log)
#   TEAM_ADMIN_PASSWORD='xxx' ./start.sh   # avec mot de passe admin explicite
#
set -euo pipefail
cd "$(dirname "$0")"

# ── Configuration (personnalisez ici) ─────────────────────────────────────────
# Mot de passe admin initial (utilisé seulement si team_users.json est absent)
export TEAM_ADMIN_PASSWORD="${TEAM_ADMIN_PASSWORD:-changeme}"

# Mot de passe du WebUI Hermes — le proxy doit se connecter au WebUI.
# Récupérez-le depuis votre installation Hermes (variable d'environnement du
# conteneur hermes-webui : HERMES_WEBUI_PASSWORD).
export HERMES_WEBUI_PASSWORD="${HERMES_WEBUI_PASSWORD:-$(printenv HERMES_WEBUI_PASSWORD || true)}"

# Ports
export TEAM_APP_PORT="${TEAM_APP_PORT:-8890}"
export TEAM_APP_HOST="${TEAM_APP_HOST:-0.0.0.0}"
export TEAM_UPSTREAM_HOST="${TEAM_UPSTREAM_HOST:-127.0.0.1}"
export TEAM_UPSTREAM_PORT="${TEAM_UPSTREAM_PORT:-8787}"

# ── Vérifications ────────────────────────────────────────────────────────────
if ! command -v python3 >/dev/null; then
  echo "✗ python3 introuvable — installez Python 3.10+" >&2
  exit 1
fi

# Arrête une instance existante (redémarrage propre)
if [ -f team-app.pid ]; then
  OLD_PID="$(cat team-app.pid 2>/dev/null || true)"
  if [ -n "${OLD_PID}" ] && kill -0 "${OLD_PID}" 2>/dev/null; then
    echo "→ Arrêt de l'ancienne instance (PID ${OLD_PID})…"
    kill "${OLD_PID}" 2>/dev/null || true
    sleep 1
    kill -9 "${OLD_PID}" 2>/dev/null || true
  fi
  rm -f team-app.pid
fi

# ── Lancement ────────────────────────────────────────────────────────────────
echo "→ Démarrage de l'Agent Équipe sur le port ${TEAM_APP_PORT}…"
nohup python3 server.py >> team-app.log 2>&1 &
echo $! > team-app.pid

sleep 1.5

# ── Vérification santé ────────────────────────────────────────────────────────
if curl -s -o /dev/null -w "" "http://127.0.0.1:${TEAM_APP_PORT}/health" 2>/dev/null; then
  HEALTH=$(curl -s "http://127.0.0.1:${TEAM_APP_PORT}/health" 2>/dev/null || true)
else
  HEALTH=""
fi

if echo "${HEALTH}" | grep -q '"ok": true'; then
  echo "✓ Application démarrée (PID $(cat team-app.pid))"
  echo ""
  echo "  URL locale :   http://127.0.0.1:${TEAM_APP_PORT}"
  echo "  Admin :        identifiant 'admin' + TEAM_ADMIN_PASSWORD"
  echo "  Logs :         tail -f team-app.log"
  echo "  Arrêt :        kill \$(cat team-app.pid)"
  echo ""
else
  echo "✗ L'application ne répond pas au health-check — derniers logs :" >&2
  tail -20 team-app.log >&2
  exit 1
fi