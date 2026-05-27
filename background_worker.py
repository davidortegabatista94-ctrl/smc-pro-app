"""
background_worker.py — Análisis autónomo 24/7 sin usuarios conectados.

Ciclo cada 3 minutos:
  1. Señal rápida EUR/USD via yfinance (precio + EMA + RSI + régimen)
  2. Snapshot → DB (para que el usuario vea datos frescos al entrar)
  3. Observación de mercado → AI pattern mining
  4. FRED macro data (cacheado 4h en DB)
  5. Self-heal cycle (máx 1 vez/hora)
  6. Telegram horario + alertas urgentes (score ≥ 80)
"""

import threading
import time
import logging
import os
from datetime import datetime, timezone

_log = logging.getLogger("smc.bg")

_CYCLE_SECS      = 180    # 3 minutos
_STARTED         = False
_LOCK            = threading.Lock()
_BOT_MIN_SCORE   = 70     # score mínimo para ejecutar orden automática
_MT5_SERVICE_URL = os.environ.get("MT5_SERVICE_URL", "").rstrip("/")
_MT5_API_TOKEN   = os.environ.get("MT5_API_TOKEN", "")
_SYMBOL          = "EURUSD"
_BOT_VOLUME      = float(os.environ.get("BOT_DEFAULT_VOLUME", "0.01"))

TELEGRAM_TOKEN   = os.environ.get(
    "TELEGRAM_BOT_TOKEN",
    "7967414683:AAGmyLDjobQOvpU_OVzlwHJ-Tf1o9GjbIlE"
).strip()
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "1442582228").strip()


# ─────────────────────────────────────────────────────────────────────────────
# Arranque (idempotente)
# ─────────────────────────────────────────────────────────────────────────────

def start_if_needed() -> None:
    """Inicia el worker en un hilo daemon. Seguro llamar múltiples veces."""
    global _STARTED
    with _LOCK:
        if _STARTED:
            return
        _STARTED = True
    t = threading.Thread(target=_loop, daemon=True, name="smc-bg")
    t.start()
    _log.info("Background worker iniciado (ciclo cada %ds)", _CYCLE_SECS)


# ─────────────────────────────────────────────────────────────────────────────
# Bucle principal
# ─────────────────────────────────────────────────────────────────────────────

def _loop() -> None:
    time.sleep(25)          # espera a que la app termine de arrancar
    while True:
        try:
            _cycle()
        except Exception as exc:
            _log.warning("BG cycle error: %s", exc)
        time.sleep(_CYCLE_SECS)


def _cycle() -> None:
    signal = _quick_signal()
    if not signal:
        return

    price  = signal.get("price", 0)
    score  = int(signal.get("score", 0))
    final  = signal.get("final_signal", "NEUTRAL")
    sess   = signal.get("session", "")
    regime = signal.get("regime", "")
    direction = signal.get("direction") or ""

    # 0 — Selector de estrategias (caché 8h, sin bloquear)
    _sel_result: dict = {}
    try:
        import strategy_selector as _ss
        _ss.ensure_ready()   # solo descarga si el caché expiró
        _sel_result = _ss.select_for_signal(
            regime=regime, direction=direction,
            score=score, session=sess,
        )
        # Ajustar score con el consenso estratégico
        _boost = _sel_result.get("score_boost", 0)
        score  = max(0, min(100, score + _boost))
        signal["score"]            = score
        signal["strategy_sel"]     = _sel_result.get("recommended", "")
        signal["strategy_support"] = _sel_result.get("supporting", [])
        signal["strategy_boost"]   = _boost
        signal["strategy_detail"]  = _sel_result.get("detail", "")
    except Exception as _se:
        _log.debug("strategy_selector error: %s", _se)

    # 1 — Snapshot en DB
    try:
        import db as _db
        _db.save_snapshot(
            price=price, signal=final, score=score,
            dxy_trend="N/A", regime=regime,
            strategy=_sel_result.get("recommended", "bg_worker"),
            extra={
                "session": sess, "source": "bg",
                "strategy_support": _sel_result.get("supporting", []),
                "strategy_boost":   _sel_result.get("score_boost", 0),
                "veto":             _sel_result.get("veto", False),
            },
            user_id="system",
        )
    except Exception:
        pass

    # 2 — Observación para mining de patrones
    try:
        import self_improve as _si
        _si.store_market_observation(
            signal=signal, score=score,
            session=sess, dxy_dir=signal.get("dxy_dir", ""),
        )
    except Exception:
        pass

    # 3 — Fundamental: FRED + 20 fuentes RSS de noticias (caché 30 min)
    _fund: dict = {}
    try:
        import data_feeds as _df
        _fund = _df.get_news_fundamental()   # descarga noticias y guarda en DB
        _df.get_fred_indicators()            # FRED (caché 4h)

        # Ajustar score con señal fundamental
        _f_adj, _f_reasons = _df.get_fundamental_score_bonus(_fund, direction)
        if _f_adj != 0:
            score = max(0, min(100, score + _f_adj))
            signal["score"]            = score
            signal["fundamental_adj"]  = _f_adj
            signal["fundamental_dir"]  = _fund.get("direction", "NEUTRAL")
            signal["fundamental_news"] = _fund.get("hi_impact", 0)
    except Exception as _fe:
        _log.debug("fundamental feed error: %s", _fe)

    # 4 — Self-heal (máx 1/h, comprueba internamente)
    try:
        import db as _db
        import self_improve as _si
        if _si.should_run_heal():
            _dna = {}
            try:
                _dna = _db.load_active_strategy() or {}
            except Exception:
                pass
            _si.run_heal_cycle(active_dna=_dna, current_user="system")
    except Exception:
        pass

    # 4b — Meta-aprendizaje: estrategia maestra adaptativa (máx 1 vez/6h)
    try:
        import strategy_learner as _sl
        if _sl.should_run_learning():
            _sl.run_learning_cycle()
    except Exception as _sle:
        _log.debug("strategy_learner error: %s", _sle)

    # 5 — Bot autónomo (ejecuta orden si está activado en DB y score ≥ umbral)
    _bot_trade_if_due(signal, score)

    # 6 — Telegram
    _telegram_if_due(signal, score)


# ─────────────────────────────────────────────────────────────────────────────
# Bot autónomo 24/7
# ─────────────────────────────────────────────────────────────────────────────

def _bot_trade_if_due(signal: dict, score: int) -> None:
    """Ejecuta una orden automática si el bot está activo en DB y hay señal suficiente."""
    if not _MT5_SERVICE_URL:
        return  # Sin servicio remoto no hay trading autónomo
    if score < _BOT_MIN_SCORE:
        return
    direction = signal.get("direction")
    if direction not in ("LONG", "SHORT"):
        return

    # Verificar si el bot está habilitado en la DB
    try:
        import db as _db
        bot_on = _db.get_setting("bg_bot_enabled")
        if str(bot_on).lower() not in ("1", "true", "yes"):
            return
        # Evitar doble entrada en la misma dirección
        last_dir = _db.get_setting("bg_bot_last_direction") or ""
        if last_dir == direction:
            return
        # Verificar que no hay posiciones abiertas ya
        _pos = _service_get("/positions")
        if _pos and isinstance(_pos, list) and len(_pos) > 0:
            return
    except Exception as e:
        _log.debug("bot_check db error: %s", e)
        return

    # Obtener precio real del servicio
    price = signal.get("price", 0)
    try:
        tick = _service_get(f"/tick/{_SYMBOL}")
        if tick and "bid" in tick:
            price = tick["bid"] if direction == "LONG" else tick["ask"]
    except Exception:
        pass
    if not price:
        return

    # Calcular SL/TP sencillos (12 pips SL, 30 pips TP)
    pip   = 0.0001
    sl_p  = 12 * pip
    tp_p  = 30 * pip
    sl    = round(price - sl_p if direction == "LONG" else price + sl_p, 5)
    tp    = round(price + tp_p if direction == "LONG" else price - tp_p, 5)

    try:
        result = _service_post("/trade", {
            "symbol":    _SYMBOL,
            "direction": "BUY" if direction == "LONG" else "SELL",
            "volume":    _BOT_VOLUME,
            "price":     price,
            "sl":        sl,
            "tp":        tp,
            "comment":   f"SMC-BG score={score}",
        })
        if result and result.get("success"):
            _log.info("BG bot orden ejecutada: %s score=%d ticket=%s",
                      direction, score, result.get("ticket"))
            import db as _db
            _db.set_setting("bg_bot_last_direction", direction)
            # Notificar por Telegram
            _send_tg(
                f"🤖 *Bot autónomo ejecutó orden*\n"
                f"{'🟢 COMPRA' if direction=='LONG' else '🔴 VENTA'} | "
                f"Score: *{score}/100* | Precio: `{price:.5f}`\n"
                f"SL: `{sl:.5f}` · TP: `{tp:.5f}` · Vol: {_BOT_VOLUME}"
            )
        else:
            _log.warning("BG bot orden fallida: %s", result)
    except Exception as e:
        _log.warning("BG bot trade error: %s", e)


def _service_get(path: str) -> dict | list | None:
    """GET al MT5 service remoto."""
    try:
        import requests
        headers = {"Authorization": f"Bearer {_MT5_API_TOKEN}"} if _MT5_API_TOKEN else {}
        r = requests.get(f"{_MT5_SERVICE_URL}{path}", headers=headers, timeout=8)
        return r.json() if r.ok else None
    except Exception:
        return None


def _service_post(path: str, body: dict) -> dict | None:
    """POST al MT5 service remoto."""
    try:
        import requests
        headers = {"Content-Type": "application/json"}
        if _MT5_API_TOKEN:
            headers["Authorization"] = f"Bearer {_MT5_API_TOKEN}"
        r = requests.post(f"{_MT5_SERVICE_URL}{path}", json=body, headers=headers, timeout=10)
        return r.json() if r.ok else None
    except Exception:
        return None


# ─────────────────────────────────────────────────────────────────────────────
# Señal rápida (sin Streamlit, sin MT5)
# ─────────────────────────────────────────────────────────────────────────────

def _quick_signal() -> dict:
    """EUR/USD 1H via yfinance: precio, EMA21/50, RSI, régimen, score."""
    try:
        import yfinance as yf
        import pandas as pd

        df = yf.download(
            "EURUSD=X", period="5d", interval="1h",
            progress=False, auto_adjust=True,
        )
        if df is None or df.empty or len(df) < 30:
            return {}

        close = df["Close"].squeeze()
        high  = df["High"].squeeze()
        low   = df["Low"].squeeze()
        price = float(close.iloc[-1])

        ema21 = float(close.ewm(span=21).mean().iloc[-1])
        ema50 = float(close.ewm(span=50).mean().iloc[-1])

        diff  = close.diff()
        gain  = diff.clip(lower=0).rolling(14).mean()
        loss  = (-diff.clip(upper=0)).rolling(14).mean()
        rs    = gain.iloc[-1] / (loss.iloc[-1] + 1e-9)
        rsi   = float(100 - 100 / (1 + rs))

        tr = pd.concat([
            high - low,
            (high - close.shift()).abs(),
            (low  - close.shift()).abs(),
        ], axis=1).max(axis=1)
        atr_pips = round(float(tr.rolling(14).mean().iloc[-1]) / 0.0001, 1)

        buy_sigs = sell_sigs = 0
        if price > ema21:  buy_sigs  += 1
        else:              sell_sigs += 1
        if ema21 > ema50:  buy_sigs  += 1
        else:              sell_sigs += 1
        if rsi < 40:       buy_sigs  += 1
        elif rsi > 60:     sell_sigs += 1

        dominant = max(buy_sigs, sell_sigs)
        score = min(100, max(0, dominant * 22 + int(abs(rsi - 50) / 2)))

        if buy_sigs > sell_sigs and score >= 50:
            final, direction = "🟢 COMPRA", "LONG"
        elif sell_sigs > buy_sigs and score >= 50:
            final, direction = "🔴 VENTA", "SHORT"
        else:
            final, direction = "⚪ NEUTRAL", None

        h = datetime.now(timezone.utc).hour
        session = ("London" if 7 <= h < 12
                   else "NY"   if 12 <= h < 17
                   else "Asia" if 2 <= h < 7
                   else "Off")

        spread = (ema21 - ema50) / (ema50 + 1e-9) * 10000
        regime = ("trending_up"   if spread > 15
                  else "trending_down" if spread < -15
                  else "ranging"       if abs(spread) < 5
                  else "neutral")

        return {
            "price": round(price, 5),
            "final_signal": final,
            "direction": direction,
            "buy_signals": buy_sigs,
            "sell_signals": sell_sigs,
            "score": score,
            "session": session,
            "regime": regime,
            "rsi": round(rsi, 1),
            "ema21": round(ema21, 5),
            "ema50": round(ema50, 5),
            "atr_1h_pips": atr_pips,
            "dxy_dir": "",
        }
    except Exception as exc:
        _log.debug("Quick signal error: %s", exc)
        return {}


# ─────────────────────────────────────────────────────────────────────────────
# Telegram
# ─────────────────────────────────────────────────────────────────────────────

def _send_tg(msg: str) -> bool:
    try:
        import requests
        r = requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            json={"chat_id": TELEGRAM_CHAT_ID, "text": msg, "parse_mode": "Markdown"},
            timeout=10,
        )
        return r.status_code == 200
    except Exception:
        return False


def _telegram_if_due(signal: dict, score: int) -> None:
    try:
        import db as _db
        now  = datetime.now(timezone.utc)
        final = signal.get("final_signal", "NEUTRAL")
        sess  = signal.get("session", "—")
        rsi   = signal.get("rsi", 0)
        price = signal.get("price", 0)
        regime = signal.get("regime", "—")

        # Urgente: score ≥ 80, max 1 cada 30 min
        is_urgent = (score >= 80 and signal.get("direction") in ("LONG", "SHORT"))
        if is_urgent:
            last_u = _db.get_setting("last_bg_urgent_tg")
            if last_u:
                lu_dt = datetime.fromisoformat(last_u)
                if not lu_dt.tzinfo:
                    lu_dt = lu_dt.replace(tzinfo=timezone.utc)
                is_urgent = (now - lu_dt).total_seconds() >= 1800

        # Horario: cada hora
        should_hourly = False
        last_h = _db.get_setting("last_bg_hourly_tg")
        if not last_h:
            should_hourly = True
        else:
            lh_dt = datetime.fromisoformat(last_h)
            if not lh_dt.tzinfo:
                lh_dt = lh_dt.replace(tzinfo=timezone.utc)
            should_hourly = (now - lh_dt).total_seconds() >= 3600

        if not is_urgent and not should_hourly:
            return

        icon = "🟢" if "COMPRA" in final else ("🔴" if "VENTA" in final else "⚪")
        prefix = "🚨 *SEÑAL URGENTE*" if is_urgent else "📊 *Análisis Horario*"
        msg = (
            f"{prefix} — SMC Bot\n"
            f"━━━━━━━━━━━━━━━━\n"
            f"💱 EUR/USD: `{price:.5f}`\n"
            f"{icon} Señal: *{final}*\n"
            f"🎯 Score: *{score}/100*\n"
            f"⏰ Sesión: {sess}  |  Régimen: {regime}\n"
            f"📈 RSI: {rsi:.1f}\n"
            f"🕐 UTC {now.strftime('%H:%M')}  |  Bot autónomo"
        )

        if _send_tg(msg):
            key = "last_bg_urgent_tg" if is_urgent else "last_bg_hourly_tg"
            _db.set_setting(key, now.isoformat())

    except Exception as exc:
        _log.debug("BG telegram error: %s", exc)
