#!/usr/bin/env python3
"""Lance LemonPDF : python run.py [--port 8000]

Les valeurs par défaut viennent de l'environnement (voir app/config.py), ce qui
permet de démarrer sans argument aussi bien en local qu'en conteneur.
"""

import argparse
import webbrowser

import uvicorn

from app import config


def main() -> None:
    parser = argparse.ArgumentParser(description="LemonPDF")
    parser.add_argument("--host", default=config.HOST)
    parser.add_argument("--port", type=int, default=config.PORT)
    parser.add_argument("--reload", action="store_true", help="rechargement auto (développement)")
    parser.add_argument("--no-browser", action="store_true", help="ne pas ouvrir le navigateur")
    args = parser.parse_args()

    shown = "127.0.0.1" if args.host in ("0.0.0.0", "::") else args.host
    url = f"http://{shown}:{args.port}"
    print(f"\n  LemonPDF  →  {url}\n  Ctrl+C pour arrêter.\n")
    if config.OPEN_BROWSER and not args.no_browser and not args.reload:
        webbrowser.open(url)
    uvicorn.run(
        "app.main:app",
        host=args.host,
        port=args.port,
        reload=args.reload,
        # Un seul worker : les documents ouverts vivent dans la mémoire du
        # processus, un second worker ne les verrait pas.
        workers=1,
        # Par défaut uvicorn ferme les connexions inactives au bout de 5 s. Le
        # navigateur, qui ne rejoue jamais un POST, échouerait alors sur la
        # première modification faite après un temps de lecture.
        timeout_keep_alive=120,
    )


if __name__ == "__main__":
    main()
