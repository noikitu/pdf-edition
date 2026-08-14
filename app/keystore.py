"""Conservation des clés API des fournisseurs LLM, sur la machine locale.

Trois principes, dans cet ordre :

1. **La clé ne revient jamais au navigateur.** L'interface n'apprend que son
   existence, jamais sa valeur. Une clé qui ne transite pas ne peut être ni lue
   dans la page, ni retrouvée dans le stockage du navigateur, ni exposée par une
   extension. C'est la raison pour laquelle elle est gardée côté serveur alors
   même que l'application est locale.

2. **Le trousseau du système d'abord.** Quand le paquet `keyring` est présent, la
   clé est confiée au trousseau de l'OS, donc chiffrée au repos et protégée par la
   session de l'utilisateur. À défaut seulement, elle tombe dans un fichier dont
   les permissions sont réduites à son propriétaire.

3. **Rien de tout cela hors de la machine.** Enregistrer ou utiliser une clé
   stockée est refusé si la requête ne vient pas de localhost : sur une instance
   partagée, une clé enregistrée serait une clé offerte à tous les visiteurs.
"""

from __future__ import annotations

import json
import os
from importlib.util import find_spec
from pathlib import Path

from .config import DATA_DIR

SERVICE = "LemonPDF"
FILE = Path(DATA_DIR).expanduser() / "keys.json"

# Clés éventuellement fournies par l'environnement : la source la plus sûre,
# puisque rien n'est écrit par nous.
ENV_VARS = {
    "anthropic": ("ANTHROPIC_API_KEY",),
    "openai": ("OPENAI_API_KEY",),
    "gemini": ("GOOGLE_API_KEY", "GEMINI_API_KEY"),
}


def _keyring():
    """Le trousseau du système, s'il est disponible et fonctionnel."""
    if find_spec("keyring") is None:
        return None
    try:
        import keyring
        from keyring.backends.fail import Keyring as FailBackend

        # Sur une machine sans trousseau utilisable, keyring installe un backend
        # qui lève à chaque appel : autant s'en rendre compte tout de suite.
        if isinstance(keyring.get_keyring(), FailBackend):
            return None
        return keyring
    except Exception:
        return None


def from_env(provider: str) -> str:
    for name in ENV_VARS.get(provider, ()):
        value = os.environ.get(name)
        if value:
            return value.strip()
    return ""


def _read_file() -> dict:
    try:
        return json.loads(FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _write_file(data: dict) -> None:
    FILE.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(FILE.parent, 0o700)
    except OSError:
        pass
    # Le fichier est créé avec des permissions restreintes *avant* d'être écrit :
    # le créer puis le protéger laisserait une fenêtre où il serait lisible.
    fd = os.open(FILE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump(data, handle)


def save(provider: str, key: str) -> str:
    """Enregistre une clé. Renvoie l'emplacement retenu : trousseau ou fichier."""
    ring = _keyring()
    if ring is not None:
        try:
            ring.set_password(SERVICE, provider, key)
            return "trousseau"
        except Exception:
            pass
    data = _read_file()
    data[provider] = key
    _write_file(data)
    return "fichier"


def get(provider: str) -> str:
    """Clé utilisable : d'abord l'environnement, puis le trousseau, puis le fichier."""
    value = from_env(provider)
    if value:
        return value
    ring = _keyring()
    if ring is not None:
        try:
            stored = ring.get_password(SERVICE, provider)
            if stored:
                return stored
        except Exception:
            pass
    return _read_file().get(provider, "")


def forget(provider: str) -> None:
    ring = _keyring()
    if ring is not None:
        try:
            ring.delete_password(SERVICE, provider)
        except Exception:
            pass
    data = _read_file()
    if data.pop(provider, None) is not None:
        _write_file(data)


def status() -> dict:
    """Ce que l'interface a le droit de savoir : l'existence d'une clé, pas sa valeur."""
    ring = _keyring()
    where = "trousseau du système" if ring is not None else "fichier protégé"
    out = {}
    for provider in ENV_VARS:
        env = bool(from_env(provider))
        out[provider] = {
            "stored": bool(get(provider)),
            "from_env": env,
            # Une clé venue de l'environnement n'est pas la nôtre : ni à effacer,
            # ni à remplacer depuis l'interface.
            "editable": not env,
        }
    return {"providers": out, "where": where}
