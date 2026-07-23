"""
backend/worker_once.py — Un ciclo del bot, para GitHub Actions (worker 24/7 gratis).

En vez de un proceso siempre encendido (Railway), GitHub Actions ejecuta este
script cada 15 min (cron). Cada ejecución:
  1. Asegura las tablas del bot en Neon Postgres.
  2. Corre UN ciclo: analiza 7 pares, evalúa trades abiertos, abre nuevos.
  3. Cada ~6h recalcula el backtest histórico (snapshot para el dashboard).

Todo el estado vive en Neon (DATABASE_URL) → persiste entre ejecuciones y lo lee
el dashboard de Streamlit Cloud. Coste: 0 €.
"""
import logging
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [worker_once] %(levelname)s: %(message)s")
_log = logging.getLogger("worker_once")


def main() -> int:
    from backend.store import ensure_ready, use_db
    from backend import paper_worker as pw

    if not use_db():
        _log.warning("Sin DATABASE_URL — el estado NO persistirá. Configura el secret.")
    ensure_ready()

    # Un ciclo (lee config de la BD: rr, min_score, interval)
    summary = pw._run_one_cycle(pw.DEFAULT_PAIRS, pw.DEFAULT_MIN_SCORE)
    _log.info("Ciclo OK: %s", summary)

    # Backtest histórico: caro (~1-2 min) → solo si el snapshot tiene > 6h
    try:
        if pw.read_backtest(max_age_secs=6 * 3600) is None:
            _log.info("Recalculando snapshot de backtest (cada ~6h)...")
            pw._compute_backtest_snapshot()
    except Exception as e:
        _log.warning("backtest snapshot: %s", e)

    return 0


if __name__ == "__main__":
    sys.exit(main())
