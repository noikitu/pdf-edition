# LemonPDF — image unique, sans service externe : tout tient dans le processus.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    HOST=0.0.0.0 \
    PORT=8000 \
    LEMONPDF_OPEN_BROWSER=0

WORKDIR /srv

# Les dépendances d'abord : la couche est réutilisée tant que requirements.txt
# ne change pas, donc les modifications de code se reconstruisent en quelques secondes.
COPY requirements.txt ./
RUN pip install -r requirements.txt

COPY run.py ./
COPY app ./app

# PyMuPDF n'a besoin d'aucun accès en écriture : on tourne sans privilèges.
RUN useradd --create-home --uid 10001 lemon
USER lemon

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=4s --start-period=10s --retries=3 \
  CMD ["python", "-c", "import os,urllib.request;urllib.request.urlopen('http://127.0.0.1:'+os.environ.get('PORT','8000')+'/healthz').read()"]

# Un seul processus, volontairement : les documents en cours d'édition vivent en
# mémoire et ne sont pas partagés entre workers.
CMD ["python", "run.py"]
