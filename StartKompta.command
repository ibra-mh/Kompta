#!/bin/bash
# macOS launcher (double-click in Finder); equivalent of StartKompta.bat.
cd "$(dirname "$0")" || exit 1

if [ ! -x ".venv/bin/python" ]; then
  echo "Environnement .venv introuvable. Installez-le une fois avec :"
  echo "  python3 -m venv .venv && .venv/bin/pip install -r requirements.txt"
  read -r -p "Appuyez sur Entrée pour fermer..."
  exit 1
fi

(sleep 2; open index.html) &
exec .venv/bin/python run.py
