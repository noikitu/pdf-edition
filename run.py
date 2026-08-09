#!/usr/bin/env python3
"""Lance l'éditeur PDF en local : python run.py [--port 8000]"""

import argparse
import webbrowser

import uvicorn


def main() -> None:
    parser = argparse.ArgumentParser(description="Éditeur PDF")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--reload", action="store_true", help="rechargement auto (développement)")
    parser.add_argument("--no-browser", action="store_true", help="ne pas ouvrir le navigateur")
    args = parser.parse_args()

    url = f"http://{args.host}:{args.port}"
    print(f"\n  Éditeur PDF  →  {url}\n  Ctrl+C pour arrêter.\n")
    if not args.no_browser and not args.reload:
        webbrowser.open(url)
    uvicorn.run(
        "app.main:app",
        host=args.host,
        port=args.port,
        reload=args.reload,
        # Par défaut uvicorn ferme les connexions inactives au bout de 5 s. Le
        # navigateur, qui ne rejoue jamais un POST, échouerait alors sur la
        # première modification faite après un temps de lecture.
        timeout_keep_alive=120,
    )


if __name__ == "__main__":
    main()
