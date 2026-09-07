FROM python:3.12-slim

LABEL org.opencontainers.image.title="Hermes Agent Équipe"
LABEL org.opencontainers.image.description="Application web multi-employés devant le WebUI Hermes — isolation par profil"

WORKDIR /app

# Aucune dépendance externe — stdlib uniquement
COPY server.py manage_users.py index.html app.html app.js style.css README.md ./

# Utilisateur non-root
RUN useradd -r -s /usr/sbin/nologin teamapp \
    && mkdir -p /data \
    && chown -R teamapp:teamapp /app /data

USER teamapp

# Les comptes et sessions vivent dans /data (volume persistant)
ENV TEAM_APP_HOST=0.0.0.0 \
    TEAM_APP_PORT=8890

HEALTHCHECK --interval=30s --timeout=5s --start-period=5s \
  CMD python3 -c "import urllib.request,sys; sys.exit(0) if b'ok' in urllib.request.urlopen('http://127.0.0.1:8890/health', timeout=3).read() else sys.exit(1)"

CMD ["python3", "server.py"]