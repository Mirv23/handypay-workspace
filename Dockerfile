# HandyPay · Espace d'équipe — image de production.
# Aucune variable secrète ici : tout est fourni par l'hébergeur (Render, Railway…).
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /srv
COPY requirements.txt .
RUN pip install -r requirements.txt

COPY app ./app
COPY sql ./sql
COPY manage.py ia_cli.py ./

# Utilisateur sans privilèges.
RUN useradd --create-home --uid 10001 espace && chown -R espace /srv
USER espace

ENV PORT=8000
EXPOSE 8000
# --proxy-headers : schéma (https) et adresse du client vus derrière le proxy de l'hébergeur. La limite de
# connexion par adresse IP ne s'y fie pas (première adresse de X-Forwarded-For, écrite par le client) :
# elle lit elle-même les en-têtes (app/auth.py, client_ip).
CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT} --proxy-headers --forwarded-allow-ips='*'"]
