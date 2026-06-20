"""
backend/paper_worker.py — Worker de paper trading en vivo 24/7 (segundo plano).

QUÉ HACE: un hilo daemon que cada N minutos, INDEPENDIENTE de si alguien tiene
el dashboard abierto, ejecuta el ciclo completo:
  1. Recoge DXY + noticias en tiempo real
  2. Analiza los 7 pares (técnico + noticias + COT proxy + calendario económico)
  3. Evalúa los trades en papel abiertos contra el precio real (¿TP/SL?)
  4. Abre trades en papel nuevos para las señales operables (≥ umbral)
  5. Registra todo y escribe un heartbeat para que el dashboard muestre su estado

POR QUÉ EN VIVO Y EN PAPEL (no real):
  El backtest histórico NO tiene noticias ni calendario — son lo que más nos importa.
  Solo en vivo podemos medir si esas señales suben el win rate por encima del 25%
  de breakeven que exige el 1:3 RR. Es un EXPERIMENTO para recoger evidencia con
  dinero ficticio. Cero riesgo de capital. Cuando haya 200+ operaciones cerradas,
  el panel de aprendizaje dirá si hay edge real o no — con honestidad, no con deseo.

PRINCIPIOS (CLAUDE.md):
  - Fail-closed: cualquier error en un ciclo se captura; el worker nunca muere.
  - El worker NO toca dinero real ni envía órdenes a ningún broker. Solo papel.
  - open_paper_trade es idempotente (un trade por par y vela 15m) → sin duplicados.
"""
from __future__ import annotations

import json
import logging
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

_log = logging.getLogger(__name__)

_BASE_DIR = Path(__file__).parent.parent
from backend.storage import data_path
HEARTBEAT = data_path("worker_heartbeat.json")
RESULTS   = data_path("worker_results.json")    # snapshot del último análisis (para el dashboard)
BACKTEST  = data_path("worker_backtest.json")   # snapshot del backtest histórico (caro, cada 6h)
CONFIG    = data_path("worker_config.json")     # config compartida dashboard↔worker (RR, min_score)


def read_config() -> dict:
    """Config compartida: el dashboard la escribe, el worker la lee cada ciclo."""
    cfg = {"rr": 3.0, "min_score": DEFAULT_MIN_SCORE}
    try:
        if CONFIG.exists():
            with open(CONFIG, "r", encoding="utf-8") as f:
                cfg.update(json.load(f))
    except Exception:
        pass
    return cfg


def write_config(rr: float | None = None, min_score: int | None = None) -> None:
    """El dashboard ajusta la config; el worker la aplica en el siguiente ciclo."""
    cfg = read_config()
    if rr is not None:        cfg["rr"] = float(rr)
    if min_score is not None: cfg["min_score"] = int(min_score)
    try:
        with open(CONFIG, "w", encoding="utf-8") as f:
            json.dump(cfg, f)
    except Exception as e:
        _log.debug("config write: %s", e)


def apply_rr(d: dict, rr: float) -> None:
    """
    Recalcula el TP a un RR objetivo manteniendo el SL (basado en liquidez).
    Permite probar 1:1, 1:2, 1:3 — el aprendizaje dirá cuál tiene más expectancy.
    """
    price = d.get("price"); sl = d.get("sl"); direction = d.get("direction")
    if not (price and sl) or direction not in ("LONG", "SHORT"):
        return
    risk = abs(price - sl)
    if risk <= 0:
        return
    d["tp1"] = round(price + rr * risk, 6) if direction == "LONG" else round(price - rr * risk, 6)
    d["rr"]  = round(rr, 2)

DEFAULT_PAIRS = ["EURUSD", "GBPUSD", "USDJPY", "USDCHF", "AUDUSD", "USDCAD", "NZDUSD"]
DEFAULT_INTERVAL = 600        # 10 min — equilibra frescura vs límites de yfinance
DEFAULT_MIN_SCORE = 70

# Singleton: un solo worker por proceso
_LOCK = threading.Lock()
_STARTED = False


def _write_heartbeat(payload: dict) -> None:
    try:
        payload["ts"] = datetime.now(timezone.utc).isoformat()
        with open(HEARTBEAT, "w", encoding="utf-8") as f:
            json.dump(payload, f, default=str)
    except Exception as e:
        _log.debug("heartbeat write: %s", e)


def read_heartbeat() -> dict:
    """Lee el último latido del worker (para mostrar estado en el dashboard)."""
    try:
        if HEARTBEAT.exists():
            with open(HEARTBEAT, "r", encoding="utf-8") as f:
                return json.load(f)
    except Exception:
        pass
    return {}


def _write_results(results: dict, dxy_dir: str) -> None:
    """Persiste el último análisis completo para que el dashboard lo LEA (no recalcule)."""
    try:
        payload = {"ts": datetime.now(timezone.utc).isoformat(),
                   "dxy": dxy_dir, "results": results}
        with open(RESULTS, "w", encoding="utf-8") as f:
            json.dump(payload, f, default=str)
    except Exception as e:
        _log.debug("results write: %s", e)


def read_results(max_age_secs: int = 900) -> dict | None:
    """
    Lee el snapshot del último análisis del worker si es reciente.
    Devuelve {ts, dxy, results} o None si no existe / está obsoleto.
    """
    try:
        if not RESULTS.exists():
            return None
        with open(RESULTS, "r", encoding="utf-8") as f:
            blob = json.load(f)
        ts = datetime.fromisoformat(blob["ts"])
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        age = (datetime.now(timezone.utc) - ts).total_seconds()
        if age > max_age_secs:
            return None
        blob["age_secs"] = int(age)
        return blob
    except Exception:
        return None


def _run_one_cycle(pairs: list[str], min_score: int) -> dict:
    """Un ciclo: analizar, evaluar trades abiertos, abrir nuevos. Devuelve resumen."""
    import backend.orchestrator as orch
    import backend.learning as L

    # 0. Config compartida (RR, min_score) — el dashboard la puede cambiar en vivo
    cfg = read_config()
    rr        = float(cfg.get("rr", 3.0))
    min_score = int(cfg.get("min_score", min_score))

    # 1. Contexto de mercado en vivo
    dxy_dir = ""
    try:
        from backend.signals import get_dxy_yf
        dxy_dir = (get_dxy_yf("1h") or {}).get("direction", "")
    except Exception as e:
        _log.debug("dxy: %s", e)
    news = []
    try:
        from backend.signals import get_rss_news
        news = get_rss_news() or []
    except Exception as e:
        _log.debug("news: %s", e)

    # 2. Evaluar trades en papel abiertos (cerrar los que tocaron TP/SL)
    try:
        ev = L.evaluate_open_trades()
    except Exception as e:
        _log.warning("evaluate_open_trades: %s", e)
        ev = {}

    # 3. Analizar los pares
    results = orch.run_all_pairs_analysis(
        pairs=pairs, analysis_mode="intraday", dxy_dir=dxy_dir, news_list=news,
    )

    # 4. Aprendizaje + registro + apertura de trades en papel
    opened = 0
    operable = 0
    for sym, d in results.items():
        d["trading_mode"] = "paper"
        d["min_score"] = min_score
        # Aplicar RR configurado (1:1 / 1:2 / 1:3) — recalcula TP manteniendo SL
        apply_rr(d, rr)
        # Ajuste de score aprendido (acotado)
        try:
            adj, why = L.edge_adjustment(d)
            if adj:
                d["score"] = max(0, min(98, int(d.get("score") or 0) + adj))
                d.setdefault("vote_log", []).append(
                    f"🧠 Aprendizaje {adj:+d}: " + "; ".join(why[:3]))
                d["learn_adj"] = adj
        except Exception:
            pass
        try:
            orch.log_decision(d)
        except Exception:
            pass
        if d.get("direction") in ("LONG", "SHORT") and int(d.get("score") or 0) >= min_score:
            operable += 1
            try:
                if L.open_paper_trade(d):
                    opened += 1
            except Exception:
                pass

    # 5. Snapshot para el dashboard (lee esto en vez de recalcular)
    _write_results(results, dxy_dir)

    summary = {
        "pairs": len(results),
        "operable": operable,
        "opened": opened,
        "eval": ev,
        "dxy": dxy_dir,
        "news_count": len(news),
        "rr": rr,
        "min_score": min_score,
    }
    return summary


def _loop(pairs: list[str], min_score: int, interval: int) -> None:
    _write_heartbeat({"status": "starting", "interval": interval})
    _last_bt = 0.0
    while True:
        t0 = time.time()
        try:
            summary = _run_one_cycle(pairs, min_score)
            summary["status"] = "ok"
            summary["cycle_secs"] = round(time.time() - t0, 1)
            _write_heartbeat(summary)
            _log.info("paper_worker cycle: %s", summary)
        except Exception as e:
            _log.warning("paper_worker cycle error: %s", e)
            _write_heartbeat({"status": "error", "error": str(e)})
        # Backtest histórico: caro (~1-2 min) → recalcular solo cada 6h en background.
        # El dashboard lo LEE (no bloquea su primera apertura).
        if time.time() - _last_bt > 6 * 3600:
            try:
                _compute_backtest_snapshot()
                _last_bt = time.time()
            except Exception as e:
                _log.warning("backtest snapshot error: %s", e)
        # Dormir hasta el próximo ciclo
        time.sleep(max(60, interval))


def _compute_backtest_snapshot() -> None:
    """Calcula backtest_multiperiod y lo guarda para que el dashboard lo lea."""
    import backend.orchestrator as orch
    # Usar el último análisis para pasar contexto de noticias/COT en vivo
    news_dir, news_score, cot_dir = "", 0.0, ""
    snap = read_results()
    if snap:
        eu = (snap.get("results") or {}).get("EURUSD", {})
        news_dir   = eu.get("news_sentiment", {}).get("direction", "NEUTRAL")
        news_score = 1.0 if news_dir == "LONG" else (-1.0 if news_dir == "SHORT" else 0.0)
        cot_dir    = eu.get("dxy_signal_dir", "")
    res = orch.backtest_multiperiod(live_news_score=news_score,
                                    live_news_dir=news_dir, live_cot_dir=cot_dir)
    try:
        payload = {"ts": datetime.now(timezone.utc).isoformat(), "backtest": res}
        with open(BACKTEST, "w", encoding="utf-8") as f:
            json.dump(payload, f, default=str)
        _log.info("backtest snapshot escrito")
    except Exception as e:
        _log.debug("backtest write: %s", e)


def read_backtest(max_age_secs: int = 8 * 3600) -> dict | None:
    """Lee el snapshot de backtest del worker si es reciente. Devuelve el dict de resultados."""
    try:
        if not BACKTEST.exists():
            return None
        with open(BACKTEST, "r", encoding="utf-8") as f:
            blob = json.load(f)
        ts = datetime.fromisoformat(blob["ts"])
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        if (datetime.now(timezone.utc) - ts).total_seconds() > max_age_secs:
            return None
        return blob.get("backtest")
    except Exception:
        return None


def start_background_worker(pairs: list[str] | None = None,
                           min_score: int = DEFAULT_MIN_SCORE,
                           interval: int = DEFAULT_INTERVAL) -> bool:
    """
    Arranca el worker en un hilo daemon, UNA sola vez por proceso.
    Devuelve True si lo arrancó ahora, False si ya estaba corriendo.
    """
    global _STARTED
    with _LOCK:
        if _STARTED:
            return False
        _STARTED = True
    th = threading.Thread(
        target=_loop,
        args=(pairs or DEFAULT_PAIRS, min_score, interval),
        daemon=True,
        name="paper_worker",
    )
    th.start()
    _log.info("paper_worker arrancado (interval=%ss, min_score=%s)", interval, min_score)
    return True


# ── Entry point como PROCESO dedicado (24/7, independiente del dashboard) ─────
# Uso: python -m backend.paper_worker   (lo lanza start.sh junto a Streamlit)
if __name__ == "__main__":
    import os
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [paper_worker] %(levelname)s: %(message)s",
    )
    _interval = int(os.environ.get("PAPER_WORKER_INTERVAL", DEFAULT_INTERVAL))
    _min_sc   = int(os.environ.get("PAPER_WORKER_MIN_SCORE", DEFAULT_MIN_SCORE))
    _log.info("Arrancando worker dedicado (interval=%ss, min_score=%s)", _interval, _min_sc)
    # Bloqueante (este proceso solo existe para el worker)
    _loop(DEFAULT_PAIRS, _min_sc, _interval)
