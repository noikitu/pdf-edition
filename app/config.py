"""Réglages de LemonPDF, surchargeables par variables d'environnement.

Les valeurs par défaut correspondent à un usage local. En déploiement, tout se
règle sans toucher au code : `PORT`, `HOST`, `LEMONPDF_MAX_UPLOAD_MB`,
`LEMONPDF_SESSION_TTL`, `LEMONPDF_MAX_SESSIONS`, `LEMONPDF_OPEN_BROWSER`.
"""

from __future__ import annotations

import os


def _int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name) or default)
    except ValueError:
        return default


def _flag(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() not in ("0", "false", "no", "off", "")


# Adresse d'écoute. `PORT` est la variable standard des hébergeurs (Render,
# Railway, Fly, Cloud Run…), on la respecte telle quelle.
HOST = os.environ.get("HOST", "127.0.0.1")
PORT = _int("PORT", 8000)

# Taille maximale d'un fichier envoyé (PDF, PDF à fusionner, image).
MAX_UPLOAD_MB = _int("LEMONPDF_MAX_UPLOAD_MB", 40)

# Les documents vivent en mémoire : on borne leur durée de vie et leur nombre
# pour qu'un serveur partagé ne finisse pas par saturer.
SESSION_TTL = _int("LEMONPDF_SESSION_TTL", 6 * 3600)
MAX_SESSIONS = _int("LEMONPDF_MAX_SESSIONS", 40)

# Ouvrir le navigateur au démarrage : pratique en local, à couper en conteneur.
OPEN_BROWSER = _flag("LEMONPDF_OPEN_BROWSER", True)

# Sauvegarde des documents en cours sur disque, pour survivre à un redémarrage.
# Couper cette option ramène l'application à un fonctionnement strictement en
# mémoire : rien n'est écrit, mais tout est perdu à l'arrêt du serveur.
AUTOSAVE = _flag("LEMONPDF_AUTOSAVE", True)
DATA_DIR = os.environ.get("LEMONPDF_DATA_DIR") or os.path.join(
    os.path.expanduser("~"), ".lemonpdf"
)
