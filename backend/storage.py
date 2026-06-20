"""
backend/storage.py — Directorio de datos PERSISTENTE.

PROBLEMA QUE RESUELVE: el sistema de archivos de Railway es EFÍMERO. Cada deploy
o reinicio del contenedor borra todo lo escrito en disco. El bot guardaba sus
paper-trades, decisiones y aprendizaje en ficheros de la raíz del repo → cada
redeploy le borraba la memoria y el panel de Aprendizaje volvía a 0.

SOLUCIÓN: escribir TODO el estado de runtime en un directorio persistente:
  - En Railway: un VOLUMEN montado (p.ej. /data) vía la variable DATA_DIR.
  - En local: la raíz del repo (comportamiento de siempre).

CÓMO ACTIVARLO EN RAILWAY (una sola vez):
  1. En el servicio → Settings → Volumes → New Volume, mount path = /data
  2. Variables → DATA_DIR = /data
  Con eso, paper_trades.jsonl y el resto sobreviven a los redeploys.
"""
from __future__ import annotations

import os
from pathlib import Path

_REPO = Path(__file__).parent.parent


def data_dir() -> Path:
    """Devuelve el directorio persistente para el estado de runtime."""
    env = os.environ.get("DATA_DIR")
    if env:
        try:
            p = Path(env)
            p.mkdir(parents=True, exist_ok=True)
            return p
        except Exception:
            pass
    return _REPO


def data_path(filename: str) -> Path:
    """Ruta a un fichero de estado dentro del directorio persistente."""
    return data_dir() / filename


def is_persistent() -> bool:
    """True si hay un DATA_DIR configurado (volumen persistente)."""
    return bool(os.environ.get("DATA_DIR"))
