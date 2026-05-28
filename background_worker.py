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

_CYCLE_SECS           = 180       # 3 minutos
_STARTED              = False
_LOCK                 = threading.Lock()
_BOT_MIN_SCORE        = 70        # score mínimo para ejecutar orden automática
_MT5_SERVICE_URL      = os.environ.get("MT5_SERVICE_URL", "").rstrip("/")
_MT5_API_TOKEN        = os.environ.get("MT5_API_TOKEN", "")
_SYMBOL               = "EURUSD"
_BOT_VOLUME           = float(os.environ.get("BOT_DEFAULT_VOLUME", "0.01"))
_STRAT_ALERT_COOLDOWN = 4 * 3600  # 4h entre alertas de la misma estrategia+dirección

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
    signal, df_1h = _quick_signal()
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

    # 4c — Análisis de patrones (máx 1 vez/6h)
    _pattern_analysis_if_due()

    # 4d — COT + Calendario económico (máx 1 vez/6h)
    _cot_calendar_if_due()

    # 5 — Bot autónomo (ejecuta orden si está activado en DB y score ≥ umbral)
    _bot_trade_if_due(signal, score)

    # 6 — Monitor posiciones: TP/SL/BE
    _monitor_positions()

    # 7 — Señal PREMIUM: entra solo cuando TODO se alinea (score ≥ 82, 5+ confluencias)
    _check_premium_entry(df_1h, signal, price)

    # 8 — Alertas Telegram por estrategia individual
    _check_strategy_alerts(df_1h, price)

    # 9 — Telegram horario / urgente
    _telegram_if_due(signal, score)


# ─────────────────────────────────────────────────────────────────────────────
# Tareas periódicas autónomas
# ─────────────────────────────────────────────────────────────────────────────

_PATTERN_INTERVAL_SECS  = 6 * 3600   # cada 6h
_COT_CAL_INTERVAL_SECS  = 6 * 3600   # cada 6h


def _pattern_analysis_if_due() -> None:
    """Ejecuta análisis de patrones con IA y guarda resultado en DB (cada 6h)."""
    try:
        import db as _db
        rows = _db.get_metrics(name="pattern_report", limit=1) or []
        if rows:
            from datetime import datetime, timezone
            last_ts = rows[0].get("created_at")
            if last_ts:
                # Parsear timestamp si es string
                if isinstance(last_ts, str):
                    last_ts = datetime.fromisoformat(last_ts.replace("Z", "+00:00"))
                if not last_ts.tzinfo:
                    last_ts = last_ts.replace(tzinfo=timezone.utc)
                elapsed = (datetime.now(timezone.utc) - last_ts).total_seconds()
                if elapsed < _PATTERN_INTERVAL_SECS:
                    return

        _log.info("BG: ejecutando análisis de patrones...")
        import self_improve as _si
        report = _si.get_pattern_report(limit=200)
        if report and not report.startswith("⚠️"):
            _db.save_metric(
                name="pattern_report",
                value=0.0,
                context={"report": report[:3000], "obs_limit": 200},
            )
            _log.info("BG: análisis de patrones guardado (%d chars)", len(report))
    except Exception as _e:
        _log.debug("pattern_analysis_if_due: %s", _e)


def _cot_calendar_if_due() -> None:
    """Descarga COT (CFTC) y calendario económico, guarda en DB (cada 6h)."""
    try:
        import db as _db
        from datetime import datetime, timezone

        # ── COT ──────────────────────────────────────────────────────────────
        cot_rows = _db.get_metrics(name="cot_data", limit=1) or []
        run_cot  = True
        if cot_rows:
            last_ts = cot_rows[0].get("created_at")
            if isinstance(last_ts, str):
                last_ts = datetime.fromisoformat(last_ts.replace("Z", "+00:00"))
            if last_ts and not last_ts.tzinfo:
                last_ts = last_ts.replace(tzinfo=timezone.utc)
            if last_ts and (datetime.now(timezone.utc) - last_ts).total_seconds() < _COT_CAL_INTERVAL_SECS:
                run_cot = False

        if run_cot:
            try:
                from backend.market_context import get_cot_data
                cot = get_cot_data()
                if cot:
                    _db.save_metric(
                        name="cot_data",
                        value=float(cot.get("net_position", 0)),
                        context={"data": cot},
                    )
                    _log.info("BG: COT actualizado — bias=%s", cot.get("bias_direction"))
            except Exception as _ce:
                _log.debug("cot update: %s", _ce)

        # ── Calendario económico ──────────────────────────────────────────────
        cal_rows = _db.get_metrics(name="economic_calendar", limit=1) or []
        run_cal  = True
        if cal_rows:
            last_ts = cal_rows[0].get("created_at")
            if isinstance(last_ts, str):
                last_ts = datetime.fromisoformat(last_ts.replace("Z", "+00:00"))
            if last_ts and not last_ts.tzinfo:
                last_ts = last_ts.replace(tzinfo=timezone.utc)
            if last_ts and (datetime.now(timezone.utc) - last_ts).total_seconds() < _COT_CAL_INTERVAL_SECS:
                run_cal = False

        if run_cal:
            try:
                from backend.market_context import get_economic_calendar
                cal = get_economic_calendar()
                if cal:
                    hi = sum(1 for e in cal if e.get("impact", "").upper() == "HIGH")
                    _db.save_metric(
                        name="economic_calendar",
                        value=float(len(cal)),
                        context={"events": cal, "high_impact": hi},
                    )
                    _log.info("BG: Calendario actualizado — %d eventos (%d alto impacto)", len(cal), hi)
            except Exception as _cale:
                _log.debug("calendar update: %s", _cale)

    except Exception as _e:
        _log.debug("cot_calendar_if_due: %s", _e)


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

def _quick_signal() -> tuple:
    """EUR/USD 1H via yfinance: precio, EMA21/50, RSI, régimen, score.
    Retorna (signal_dict, df) — df tiene 20d de datos para las estrategias.
    """
    try:
        import yfinance as yf
        import pandas as pd

        df = yf.download(
            "EURUSD=X", period="20d", interval="1h",
            progress=False, auto_adjust=True,
        )
        if df is None or df.empty or len(df) < 30:
            return {}, None

        # Flatten MultiIndex columns (yfinance multi-ticker format)
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)

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
        }, df
    except Exception as exc:
        _log.debug("Quick signal error: %s", exc)
        return {}, None


def _enrich_volume_bg(df):
    """Intenta patchear df con OANDA tick volume (background worker)."""
    try:
        if not _MT5_SERVICE_URL:
            return df
        import requests as _req
        import pandas as _pd
        r = _req.get(
            f"{_MT5_SERVICE_URL}/candles/EURUSD",
            params={"tf": "1h", "count": len(df) + 10},
            timeout=6,
        )
        if r.status_code != 200:
            return df
        candles = r.json().get("candles", [])
        if not candles:
            return df
        cv = _pd.DataFrame(candles)
        cv["time"] = _pd.to_datetime(cv["time"], utc=True).dt.tz_localize(None)
        cv = cv.set_index("time")["volume"].rename("Volume_oanda")
        idx = df.index.tz_localize(None) if df.index.tz is not None else df.index
        merged = cv.reindex(idx)
        if merged.sum() > 0:
            df = df.copy()
            df["Volume_oanda"] = merged.values
    except Exception as _e:
        _log.debug(f"BG volume enrich: {_e}")
    return df


# ─────────────────────────────────────────────────────────────────────────────
# Monitor de posiciones — TP / SL / Break Even
# ─────────────────────────────────────────────────────────────────────────────

def _monitor_positions() -> None:
    """Revisa posiciones abiertas cada ciclo y avisa por Telegram de:
      - TP o SL alcanzado (posición desaparecida)
      - Momento de mover SL a Break Even (precio = entrada + 1× riesgo)
      - Cercanía al TP (a 3 pips o menos)
    """
    if not _MT5_SERVICE_URL:
        return

    try:
        import db as _db
        import json
    except Exception:
        return

    positions = _service_get("/positions")
    if not isinstance(positions, list):
        return

    now     = datetime.now(timezone.utc)
    pip     = 0.0001
    current: dict = {}

    for pos in positions:
        ticket    = str(pos.get("ticket", ""))
        symbol    = pos.get("symbol", _SYMBOL)
        direction = pos.get("type", "BUY")
        entry     = float(pos.get("open_price", 0) or 0)
        sl        = float(pos.get("sl",         0) or 0)
        tp        = float(pos.get("tp",         0) or 0)
        profit    = float(pos.get("profit",     0) or 0)

        if not ticket or not entry:
            continue

        current[ticket] = {
            "symbol": symbol, "direction": direction,
            "entry": entry, "sl": sl, "tp": tp, "profit": profit,
        }

        # Precio en tiempo real
        tick  = _service_get(f"/tick/{symbol}")
        price = 0.0
        if tick:
            price = float(tick.get("ask" if direction == "BUY" else "bid", 0) or 0)
        if not price:
            continue

        # ── Break Even ──────────────────────────────────────────────────────
        if sl and entry:
            be_key = f"pos_be_{ticket}"
            if _db.get_setting(be_key) != "1":
                risk = abs(entry - sl)
                sl_already_at_be = (
                    (direction == "BUY"  and sl >= entry - pip) or
                    (direction == "SELL" and sl <= entry + pip)
                )
                be_trigger = (entry + risk) if direction == "BUY" else (entry - risk)
                triggered  = (
                    (direction == "BUY"  and price >= be_trigger) or
                    (direction == "SELL" and price <= be_trigger)
                )
                if triggered and not sl_already_at_be:
                    icon = "📈" if direction == "BUY" else "📉"
                    _send_tg(
                        f"⚡ *MUEVE SL A BREAK EVEN*\n"
                        f"━━━━━━━━━━━━━━━━\n"
                        f"💱 {symbol}: `{price:.5f}`\n"
                        f"{icon} {direction} | Entrada: `{entry:.5f}`\n"
                        f"✅ Pon SL en: `{entry:.5f}` (sin riesgo)\n"
                        f"SL actual: `{sl:.5f}` · Riesgo: {round(risk/pip,1)} pips\n"
                        f"💰 P&L: {profit:+.2f}\n"
                        f"🕐 UTC {now.strftime('%H:%M')}"
                    )
                    _db.set_setting(be_key, "1")

        # ── Cerca del TP (≤ 3 pips) ─────────────────────────────────────────
        if tp:
            near_key = f"pos_near_tp_{ticket}"
            if _db.get_setting(near_key) != "1":
                near = (
                    (direction == "BUY"  and price >= tp - 3 * pip) or
                    (direction == "SELL" and price <= tp + 3 * pip)
                )
                if near:
                    _send_tg(
                        f"🎯 *CERCA DEL TP — {symbol}*\n"
                        f"━━━━━━━━━━━━━━━━\n"
                        f"💱 Precio: `{price:.5f}` → TP: `{tp:.5f}`\n"
                        f"{'📈' if direction=='BUY' else '📉'} {direction} | "
                        f"Entrada: `{entry:.5f}`\n"
                        f"💰 P&L: {profit:+.2f}\n"
                        f"🕐 UTC {now.strftime('%H:%M')}"
                    )
                    _db.set_setting(near_key, "1")

    # ── Detectar posiciones cerradas (TP o SL ejecutado) ────────────────────
    prev: dict = {}
    try:
        raw = _db.get_setting("bg_tracked_positions") or "{}"
        prev = json.loads(raw)
    except Exception:
        pass

    for ticket, p in prev.items():
        if ticket in current:
            continue  # sigue abierta

        sym    = p.get("symbol", _SYMBOL)
        d      = p.get("direction", "BUY")
        entry  = float(p.get("entry", 0))
        sl_p   = float(p.get("sl",    0))
        tp_p   = float(p.get("tp",    0))
        profit = float(p.get("profit", 0))

        hit_tp = profit >= 0
        emoji  = "🎯" if hit_tp else "🛑"
        result = "TP ALCANZADO ✅" if hit_tp else "SL ALCANZADO ❌"
        _send_tg(
            f"{emoji} *{result}*\n"
            f"━━━━━━━━━━━━━━━━\n"
            f"💱 {sym} | Ticket #{ticket}\n"
            f"{'📈' if d=='BUY' else '📉'} {d} | Entrada: `{entry:.5f}`\n"
            f"SL: `{sl_p:.5f}` · TP: `{tp_p:.5f}`\n"
            f"💰 Resultado: {profit:+.2f}\n"
            f"🕐 UTC {now.strftime('%H:%M')}"
        )
        for suffix in ("be", "near_tp"):
            try:
                _db.set_setting(f"pos_{suffix}_{ticket}", "")
            except Exception:
                pass

    # Guardar estado actual
    try:
        _db.set_setting("bg_tracked_positions", json.dumps(current))
    except Exception:
        pass


# ─────────────────────────────────────────────────────────────────────────────
# Alertas por estrategia
# ─────────────────────────────────────────────────────────────────────────────

def _check_strategy_alerts(df, price: float) -> None:
    """Desactivado: las señales por estrategia individual generaban demasiado ruido.
    La única alerta de entrada es la señal PREMIUM (_check_premium_entry),
    que requiere score >= 82 y >= 5 confluencias simultáneas.
    """
    pass


# ─────────────────────────────────────────────────────────────────────────────
# Helpers para enriquecer el mensaje premium
# ─────────────────────────────────────────────────────────────────────────────

def _find_liquidity_target(df, direction: str) -> tuple:
    """
    Localiza el pool de liquidez más cercano en la dirección del trade.
    Para LONG: próximo swing high (stops de shorts acumulados ahí).
    Para SHORT: próximo swing low (stops de longs acumulados ahí).
    Retorna (nivel_precio, pips_de_distancia, descripción_str).
    """
    try:
        h  = df["High"].values
        lo = df["Low"].values
        c  = df["Close"].values
        px = float(c[-1])
        n  = len(h)
        pip = 0.0001

        # Pivots de 3 barras en las últimas 80 velas
        start = max(0, n - 80)
        pivots = []
        for i in range(start + 2, n - 1):
            if direction == "LONG":
                if h[i] > h[i - 1] and h[i] > h[i + 1] and h[i] > px:
                    pivots.append(h[i])
            else:
                if lo[i] < lo[i - 1] and lo[i] < lo[i + 1] and lo[i] < px:
                    pivots.append(lo[i])

        if not pivots:
            return None, None, None

        if direction == "LONG":
            target = min(p for p in pivots if p > px)
            pips   = round((target - px) / pip, 0)
            desc   = f"`{target:.5f}` (+{pips:.0f} pips)"
        else:
            target = max(p for p in pivots if p < px)
            pips   = round((px - target) / pip, 0)
            desc   = f"`{target:.5f}` (-{pips:.0f} pips)"

        return target, pips, desc
    except Exception:
        return None, None, None


def _generate_why_narrative(confluences: list, direction: str, regime: str,
                             rsi: float, atr_pips: float,
                             rsi_4h: float, has_4h: bool) -> str:
    """
    Genera 2-3 frases en lenguaje natural explicando por qué esta operación
    tiene sentido ahora mismo. Sintetiza las confluencias más relevantes.
    """
    bull = direction == "LONG"
    dir_word = "alcista" if bull else "bajista"

    # Estructura de tendencia
    if any("totalmente alineadas" in cf for cf in confluences):
        trend_line = f"La estructura técnica está perfectamente alineada {dir_word}"
    elif any("mayoritariamente" in cf for cf in confluences):
        trend_line = f"Las medias muestran un sesgo {dir_word} claro"
    else:
        trend_line = f"El precio se posiciona correctamente en terreno {dir_word}"

    # RSI
    if bull:
        if 50 <= rsi <= 65:
            rsi_line = f"con RSI {rsi:.0f} en zona de fuerza sin sobrecompra"
        elif rsi < 50:
            rsi_line = f"con RSI {rsi:.0f} en pullback limpio listo para rebotar"
        else:
            rsi_line = f"con RSI {rsi:.0f}"
    else:
        if 35 <= rsi <= 50:
            rsi_line = f"con RSI {rsi:.0f} en zona de debilidad sin sobreventa"
        elif rsi > 50:
            rsi_line = f"con RSI {rsi:.0f} en rebote que se agota"
        else:
            rsi_line = f"con RSI {rsi:.0f}"

    sentence1 = f"{trend_line}, {rsi_line}."

    # Momentum + 4H
    if any("acaba de cruzar" in cf for cf in confluences):
        mom = "El MACD acaba de cruzar marcando un cambio fresco de momentum"
    elif any("MACD positivo" in cf or "MACD negativo" in cf for cf in confluences):
        mom = "El MACD confirma el momentum en la misma dirección"
    else:
        mom = "El momentum técnico global apoya la señal"

    if has_4h and any("4H" in cf and "confirmada" in cf for cf in confluences):
        sentence2 = f"{mom}; el marco de 4H también confirma la tendencia con RSI {rsi_4h:.0f}."
    elif has_4h and any("4H" in cf for cf in confluences):
        sentence2 = f"{mom} y el 4H está del mismo lado."
    else:
        sentence2 = f"{mom}."

    # Institucionales
    inst = []
    if any("COT" in cf and "CONFIRMA" in cf for cf in confluences):
        inst.append("los institucionales (COT) operan en el mismo sentido")
    if any("institucional" in cf.lower() and "alta" in cf.lower() for cf in confluences):
        inst.append("el volumen institucional está por encima de la media")
    if any("London" in cf or "NY" in cf for cf in confluences):
        inst.append("estamos en sesión de máxima liquidez")

    sentence3 = (
        f"Además, {' y '.join(inst)}, lo que refuerza la validez de la señal."
        if inst else
        f"El ATR actual de {atr_pips:.0f} pips confirma que hay volatilidad suficiente para el movimiento esperado."
    )

    return f"{sentence1} {sentence2} {sentence3}"


# ─────────────────────────────────────────────────────────────────────────────
# SEÑAL PREMIUM — El filtro más exigente del sistema
# Solo se envía cuando TODO se alinea: técnico + fundamental + volumen + sesión
# ─────────────────────────────────────────────────────────────────────────────

_PREMIUM_COOLDOWN_SECS = 6 * 3600   # máximo 1 señal premium cada 6h

# Nivel de riesgo por calidad de señal
_RISK_TABLE = [
    (92, "1.0%",  "señal EXCEPCIONAL — máxima confianza del sistema"),
    (87, "0.75%", "señal de ALTA CALIDAD"),
    (82, "0.5%",  "buena señal — sé conservador en el tamaño"),
]

# Estrategias consideradas top-tier para el filtro de consenso
_TOP_STRATEGIES = {
    "ema_ribbon", "meta_composite", "precision_be",
    "ema_trend", "supertrend", "rsi_reversion",
}


def _check_premium_entry(df_1h, signal: dict, price: float) -> None:
    """
    Motor de señales premium: evalúa 10 capas de confluencia.
    Solo envía al Telegram cuando la puntuación >= 82 Y >= 5 confluencias.
    Cooldown: 6h entre señales para evitar spam.
    """
    if df_1h is None or df_1h.empty or len(df_1h) < 60 or not price:
        return

    direction = signal.get("direction")
    if direction not in ("LONG", "SHORT"):
        return

    try:
        import db as _db
        import pandas as _pd
        import numpy as _np

        # ── Cooldown global ───────────────────────────────────────────────
        _last_raw = _db.get_setting("premium_signal_last_ts") or "0"
        try:
            _elapsed = time.time() - float(_last_raw)
        except ValueError:
            _elapsed = _PREMIUM_COOLDOWN_SECS + 1
        if _elapsed < _PREMIUM_COOLDOWN_SECS:
            return

        # ── Calcular todos los indicadores ────────────────────────────────
        c  = df_1h["Close"]
        h  = df_1h["High"]
        lo = df_1h["Low"]
        o  = df_1h.get("Open", c)

        px = float(c.iloc[-1])

        # EMAs
        e5   = float(c.ewm(span=5,   adjust=False).mean().iloc[-1])
        e10  = float(c.ewm(span=10,  adjust=False).mean().iloc[-1])
        e20  = float(c.ewm(span=20,  adjust=False).mean().iloc[-1])
        e21  = float(c.ewm(span=21,  adjust=False).mean().iloc[-1])
        e50  = float(c.ewm(span=50,  adjust=False).mean().iloc[-1])
        e200 = float(c.ewm(span=200, adjust=False).mean().iloc[-1])

        # RSI(14)
        _d   = c.diff()
        _g   = _d.clip(lower=0).ewm(span=14, adjust=False).mean()
        _l   = (-_d.clip(upper=0)).ewm(span=14, adjust=False).mean()
        rsi  = float(100 - 100 / (1 + _g.iloc[-1] / (_l.iloc[-1] + 1e-9)))

        # ATR(14)
        _tr  = _pd.concat([h - lo, (h - c.shift()).abs(),
                            (lo - c.shift()).abs()], axis=1).max(axis=1)
        atr      = float(_tr.ewm(span=14, adjust=False).mean().iloc[-1])
        atr_pips = atr / 0.0001

        # MACD (12/26/9)
        _m12      = c.ewm(span=12, adjust=False).mean()
        _m26      = c.ewm(span=26, adjust=False).mean()
        _macd     = _m12 - _m26
        _macd_sig = _macd.ewm(span=9, adjust=False).mean()
        macd_hist = float((_macd - _macd_sig).iloc[-1])
        macd_prev = float((_macd - _macd_sig).iloc[-2]) if len(df_1h) > 2 else macd_hist

        # Stochastic(14,3)
        _lo14  = lo.rolling(14).min()
        _hi14  = h.rolling(14).max()
        _stk   = 100 * (c - _lo14) / (_hi14 - _lo14 + 1e-9)
        stk    = float(_stk.iloc[-1])

        # 4H (resample de 1H)
        try:
            df_4h   = df_1h.resample("4h").agg(
                {"Open": "first", "High": "max", "Low": "min", "Close": "last"}
            ).dropna()
            c4      = df_4h["Close"]
            e21_4h  = float(c4.ewm(span=21, adjust=False).mean().iloc[-1])
            e50_4h  = float(c4.ewm(span=50, adjust=False).mean().iloc[-1])
            px4h    = float(c4.iloc[-1])
            _d4     = c4.diff()
            _g4     = _d4.clip(lower=0).ewm(span=14, adjust=False).mean()
            _l4     = (-_d4.clip(upper=0)).ewm(span=14, adjust=False).mean()
            rsi_4h  = float(100 - 100 / (1 + _g4.iloc[-1] / (_l4.iloc[-1] + 1e-9)))
            has_4h  = True
        except Exception:
            px4h = e21_4h = e50_4h = rsi_4h = 0
            has_4h = False

        # Volumen (ya enriquecido por _enrich_volume_bg)
        try:
            from backend.indicators import _ensure_volume
            vol      = _ensure_volume(df_1h)
            vol_avg  = float(vol.rolling(20).mean().iloc[-1])
            vol_last = float(vol.iloc[-1])
            vol_ratio = vol_last / vol_avg if vol_avg > 0 else 1.0
        except Exception:
            vol_ratio = 1.0

        # COT (si está cacheado en DB)
        cot_bias = "neutral"
        try:
            _cot_raw = _db.get_setting("cot_cache")
            if _cot_raw:
                import json as _j
                _cot = _j.loads(_cot_raw)
                cot_bias = _cot.get("bias", "neutral")
        except Exception:
            pass

        # Noticias (si está cacheado)
        news_risk = "low"
        try:
            _cal_raw = _db.get_setting("calendar_cache")
            if _cal_raw:
                import json as _jc
                _cal = _jc.loads(_cal_raw)
                _now_utc = datetime.now(timezone.utc)
                for _ev in _cal:
                    if _ev.get("impact", "").upper() == "HIGH":
                        try:
                            _et = datetime.fromisoformat(_ev["date"].replace("Z", "+00:00"))
                            _mins = abs((_et - _now_utc).total_seconds() / 60)
                            if _mins < 60:
                                news_risk = "high"
                                break
                            elif _mins < 120:
                                news_risk = "medium"
                        except Exception:
                            pass
        except Exception:
            pass

        # ── EVALUACIÓN DE 10 CONFLUENCIAS ─────────────────────────────────
        bull = direction == "LONG"
        confluences = []
        score = 0

        # 1. EMA Ribbon 5/10/20/50 alineadas — estructura de tendencia limpia
        if bull:
            if e5 > e10 > e20 > e50:
                confluences.append("📊 EMA Ribbon 5/10/20/50 totalmente alineadas ALCISTAS")
                score += 20
            elif e5 > e20 and px > e50:
                confluences.append("📊 EMAs mayoritariamente alcistas (e5>e20, precio>e50)")
                score += 10
        else:
            if e5 < e10 < e20 < e50:
                confluences.append("📊 EMA Ribbon 5/10/20/50 totalmente alineadas BAJISTAS")
                score += 20
            elif e5 < e20 and px < e50:
                confluences.append("📊 EMAs mayoritariamente bajistas")
                score += 10

        # 2. MACD cruzando o confirmando — momentum institucional
        if bull:
            if macd_hist > 0 and macd_prev <= 0:
                confluences.append("⚡ MACD acaba de cruzar al alza — cambio de momentum")
                score += 15
            elif macd_hist > 0:
                confluences.append("⚡ MACD positivo — momentum alcista confirmado")
                score += 10
        else:
            if macd_hist < 0 and macd_prev >= 0:
                confluences.append("⚡ MACD acaba de cruzar a la baja — cambio de momentum")
                score += 15
            elif macd_hist < 0:
                confluences.append("⚡ MACD negativo — momentum bajista confirmado")
                score += 10

        # 3. RSI en zona limpia — ni sobrecomprado ni sobrevendido en la dirección
        if bull:
            if 50 <= rsi <= 65:
                confluences.append(f"📈 RSI {rsi:.0f} en zona ALCISTA limpia (50-65)")
                score += 14
            elif 42 <= rsi < 50:
                confluences.append(f"📈 RSI {rsi:.0f} — pullback limpio, listo para subir")
                score += 10
        else:
            if 35 <= rsi <= 50:
                confluences.append(f"📉 RSI {rsi:.0f} en zona BAJISTA limpia (35-50)")
                score += 14
            elif 50 < rsi <= 58:
                confluences.append(f"📉 RSI {rsi:.0f} — rebote limpio, listo para caer")
                score += 10

        # 4. Confirmación en 4H — la tendencia madre manda
        if has_4h:
            if bull and px4h > e21_4h > e50_4h and 40 <= rsi_4h <= 70:
                confluences.append("🕯 Tendencia 4H ALCISTA confirmada (EMA21+50+RSI)")
                score += 18
            elif bull and px4h > e50_4h:
                confluences.append("🕯 Precio sobre EMA50 en 4H — sesgo alcista")
                score += 10
            elif not bull and px4h < e21_4h < e50_4h and 30 <= rsi_4h <= 60:
                confluences.append("🕯 Tendencia 4H BAJISTA confirmada (EMA21+50+RSI)")
                score += 18
            elif not bull and px4h < e50_4h:
                confluences.append("🕯 Precio bajo EMA50 en 4H — sesgo bajista")
                score += 10

        # 5. Sesión de máxima liquidez — London o NY, no Asia
        h_utc = datetime.now(timezone.utc).hour
        if 7 <= h_utc < 12:
            confluences.append("🌍 Sesión London — máxima actividad institucional europea")
            score += 10
        elif 12 <= h_utc < 17:
            confluences.append("🗽 Sesión NY — máxima liquidez USD + overlap")
            score += 10
        elif 12 <= h_utc < 8:
            confluences.append("⚠️ Sesión NY tardía — liquidez moderada")
            score += 5

        # 6. ATR y volatilidad — sin rango, sin operación
        if atr_pips >= 10:
            confluences.append(f"💥 ATR {atr_pips:.1f} pips — volatilidad EXCELENTE para el movimiento")
            score += 10
        elif atr_pips >= 6:
            confluences.append(f"✅ ATR {atr_pips:.1f} pips — volatilidad SUFICIENTE")
            score += 6
        elif atr_pips < 4:
            score -= 5   # penalizar si ATR demasiado bajo

        # 7. EMA200 — filtro macro tendencia principal
        if (bull and px > e200) or (not bull and px < e200):
            confluences.append("🌐 Precio al lado correcto de EMA200 — macro a favor")
            score += 8

        # 8. Volumen institucional — actividad superior a la media
        if vol_ratio >= 1.8:
            confluences.append(f"📊 Volumen {vol_ratio:.1f}x la media — actividad institucional ALTA")
            score += 10
        elif vol_ratio >= 1.3:
            confluences.append(f"📊 Volumen {vol_ratio:.1f}x la media — actividad superior a lo normal")
            score += 6

        # 9. COT institucional confirma dirección
        if (bull and cot_bias == "bullish") or (not bull and cot_bias == "bearish"):
            confluences.append("🏦 COT institucional CONFIRMA la dirección — grandes en el mismo lado")
            score += 10
        elif cot_bias == "neutral":
            score += 2

        # 10. Noticias — riesgo macroeconómico
        if news_risk == "high":
            confluences.append("⚠️ NOTICIA HIGH IMPACT en <60min — esperar o reducir tamaño al 50%")
            score -= 15   # penalización severa: las noticias destruyen señales técnicas
        elif news_risk == "medium":
            confluences.append("⚠️ Evento macro próximo — SL más ajustado recomendado")
            score -= 5
        else:
            confluences.append("✅ Sin noticias de alto impacto próximas — entorno limpio")
            score += 5

        # ── UMBRAL: score ≥ 82 Y ≥ 5 confluencias positivas ─────────────
        _positive_conf = [c for c in confluences if not c.startswith("⚠️")]
        _log.info(f"Premium check: dir={direction} score={score} conf={len(_positive_conf)}")

        if score < 82 or len(_positive_conf) < 5:
            return   # No es suficientemente buena — silencio total

        if news_risk == "high" and score < 90:
            return   # Noticias + señal mediocre = no tocar

        # ── NIVELES DE LA OPERACIÓN ───────────────────────────────────────
        sl_dist  = atr * 1.5          # SL conservador 1.5×ATR
        tp1_dist = atr * 1.0          # TP1 rápido para asegurar parcial
        tp2_dist = atr * 2.0          # TP2 objetivo principal
        tp3_dist = atr * 3.5          # TP3 para dejar correr

        sl_pips  = round(sl_dist  / 0.0001, 0)
        tp1_pips = round(tp1_dist / 0.0001, 0)
        tp2_pips = round(tp2_dist / 0.0001, 0)
        tp3_pips = round(tp3_dist / 0.0001, 0)

        sign = 1 if bull else -1
        sl   = round(px - sign * sl_dist,  5)
        tp1  = round(px + sign * tp1_dist, 5)
        tp2  = round(px + sign * tp2_dist, 5)
        tp3  = round(px + sign * tp3_dist, 5)

        rr1 = round(tp1_pips / sl_pips, 1)
        rr2 = round(tp2_pips / sl_pips, 1)
        rr3 = round(tp3_pips / sl_pips, 1)

        # ── GESTIÓN DEL RIESGO según calidad ─────────────────────────────
        risk_pct = "0.5%"
        risk_note = "señal buena — empieza conservador"
        for _min_score, _r, _n in _RISK_TABLE:
            if score >= _min_score:
                risk_pct = _r
                risk_note = _n
                break

        # Ajustar si hay noticia cercana
        if news_risk == "high":
            risk_pct = "0.25%"
            risk_note = "REDUCIDO por noticia próxima"

        # ── LIQUIDEZ OBJETIVO ─────────────────────────────────────────────
        liq_lvl, liq_pips, liq_desc = _find_liquidity_target(df_1h, direction)

        # ── NARRATIVA DEL POR QUÉ ─────────────────────────────────────────
        why_text = _generate_why_narrative(
            confluences, direction, signal.get("regime", ""),
            rsi, atr_pips, rsi_4h if has_4h else 50.0, has_4h,
        )

        # ── EJEMPLOS DE RIESGO EN € ───────────────────────────────────────
        _risk_float = float(risk_pct.replace("%", "")) / 100
        _ex5k  = int(5000  * _risk_float)
        _ex10k = int(10000 * _risk_float)
        _ex20k = int(20000 * _risk_float)
        risk_examples = f"€5K→€{_ex5k} | €10K→€{_ex10k} | €20K→€{_ex20k}"

        # ── CONSTRUIR MENSAJE TELEGRAM ────────────────────────────────────
        dir_emoji  = "🟢 LONG" if bull else "🔴 SHORT"
        qual_emoji = ("🏆 EXCEPCIONAL" if score >= 92
                      else "⭐⭐ MUY BUENA" if score >= 87
                      else "⭐ BUENA")
        now_str    = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        conf_block = "\n".join(f"  {cf}" for cf in confluences)

        # Liquidez section
        if liq_lvl is not None:
            liq_section = (
                f"🎯 *¿HACIA QUÉ LIQUIDEZ VA?*\n"
                f"Próximo pool de liquidez en {liq_desc}\n"
                f"  ↳ Stops {'de posiciones short' if bull else 'de posiciones long'} acumulados ahí son el imán del movimiento.\n\n"
            )
        else:
            liq_section = ""

        msg = (
            f"🎯 *ENTRADA PREMIUM — SMC Pro*\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"*{dir_emoji} EUR/USD*  |  {qual_emoji}\n"
            f"Puntuación: *{score}/100*  ·  {len(_positive_conf)} confluencias activas\n\n"

            f"💡 *¿POR QUÉ ENTRAR AHORA?*\n"
            f"{why_text}\n\n"

            f"{liq_section}"

            f"💰 *NIVELES DE LA OPERACIÓN*\n"
            f"┌ Entrada:   `{px:.5f}`\n"
            f"├ Stop Loss: `{sl:.5f}`  (-{sl_pips:.0f} pips)\n"
            f"├ TP1:       `{tp1:.5f}`  (+{tp1_pips:.0f}p)  R:R 1:{rr1}\n"
            f"├ TP2:       `{tp2:.5f}`  (+{tp2_pips:.0f}p)  R:R 1:{rr2}\n"
            f"└ TP3:       `{tp3:.5f}`  (+{tp3_pips:.0f}p)  R:R 1:{rr3}\n\n"

            f"💼 *GESTIÓN DEL RIESGO*\n"
            f"• Riesgo recomendado: *{risk_pct}* de cuenta  ← {risk_note}\n"
            f"  ↳ {risk_examples}\n"
            f"• Plan: cerrar 40% en TP1 → SL a BE → 40% en TP2 → trail 20% a TP3\n"
            f"• SL basado en 1.5×ATR ({atr_pips:.1f} pips de volatilidad actual)\n\n"

            f"⚡ *CONFLUENCIAS DETECTADAS*\n"
            f"{conf_block}\n\n"

            f"📌 *CONTEXTO DE MERCADO*\n"
            f"• RSI 1H: {rsi:.0f}  |  ATR: {atr_pips:.1f} pips\n"
        )

        if has_4h:
            msg += f"• RSI 4H: {rsi_4h:.0f}  |  EMA50-4H: `{e50_4h:.5f}`\n"

        msg += (
            f"• COT institucional: {cot_bias.upper()}\n"
            f"• Noticias próximas: {'⚠️ PRECAUCIÓN' if news_risk != 'low' else '✅ sin riesgo'}\n"
            f"• Volumen relativo: {vol_ratio:.1f}x la media\n\n"
            f"⏰ {now_str}\n"
            f"_Confirma en el gráfico antes de entrar. Respeta siempre el SL._"
        )

        if _send_tg(msg):
            _db.set_setting("premium_signal_last_ts", str(time.time()))
            _log.info(
                "PREMIUM SIGNAL SENT: %s score=%d conf=%d",
                direction, score, len(confluences),
            )
        else:
            _log.warning("Premium signal: fallo al enviar Telegram")

    except Exception as _pe:
        _log.warning("_check_premium_entry error: %s", _pe)


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
