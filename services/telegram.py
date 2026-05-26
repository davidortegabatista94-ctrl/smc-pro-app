"""
services/telegram.py — Telegram alert builders and sender.
No Streamlit dependencies.
"""
import logging
from datetime import datetime, timedelta

from backend.config import (
    TELEGRAM_TOKEN, TELEGRAM_CHAT_ID, PIP, UTC_OFFSET_SPAIN, MIN_DEFINITIVE_SCORE,
)
from backend.indicators import score_label

# DB is optional — used only for reading recent snapshots in 2h summary
try:
    import db as _db
    _DB_OK = True
except ImportError:
    _db = None
    _DB_OK = False


def send_telegram_raw(msg: str, token: str = None, chat_id: str = None) -> bool:
    """Send a plain Markdown message to the configured Telegram chat."""
    try:
        import requests
        _token   = token   or TELEGRAM_TOKEN
        _chat_id = chat_id or TELEGRAM_CHAT_ID
        if not _token or _token == "TU_TELEGRAM_BOT_TOKEN" or not _chat_id:
            return False
        requests.post(
            f"https://api.telegram.org/bot{_token}/sendMessage",
            json={"chat_id": _chat_id, "text": msg, "parse_mode": "Markdown"},
            timeout=10,
        )
        return True
    except Exception as e:
        logging.warning("send_telegram_raw error: %s", e)
        return False


def send_telegram_alert(
    signal: dict, score: int, tick=None,
    definitive: bool = False, reason: str = None,
    token: str = None, chat_id: str = None,
) -> bool:
    """Build and send a trade alert message."""
    _token   = token   or TELEGRAM_TOKEN
    _chat_id = chat_id or TELEGRAM_CHAT_ID
    if _token == "TU_TELEGRAM_BOT_TOKEN":
        return False
    try:
        direction = signal.get("direction", "")
        price     = signal.get("price", 0) or 0
        tp        = signal.get("tp", 0) or 0
        sl        = signal.get("sl", 0) or 0
        rr        = signal.get("rr", 0) or 0

        if reason and reason.startswith("CLOSED_"):
            outcome = reason.split("_")[1]
            emoji   = "✅" if outcome == "TP" else "❌" if outcome == "SL" else "🔄"
            msg = (
                f"🔒 *POSICIÓN CERRADA*\n"
                f"━━━━━━━━━━━━━━━━━━\n"
                f"*Resultado:* {emoji} {outcome}\n"
                f"*Dirección:* {'📈 LONG' if direction=='LONG' else '📉 SHORT'}\n"
                f"*Entrada:* `{price:.5f}`\n"
                f"*TP:* `{tp:.5f}` | *SL:* `{sl:.5f}`\n"
                f"━━━━━━━━━━━━━━━━━━\n"
                f"🚀 _Listo para nueva señal definitiva_"
            )
        elif reason == "BE":
            msg = (
                f"⚖️ *BREAK EVEN ALCANZADO*\n"
                f"━━━━━━━━━━━━━━━━━━\n"
                f"*Dirección:* {'📈 LONG' if direction=='LONG' else '📉 SHORT'}\n"
                f"*Beneficio:* +{abs(price-sl)/PIP:.1f}p (1:1)\n"
                f"*Entrada:* `{price:.5f}`\n"
                f"*TP:* `{tp:.5f}` | *SL:* `{sl:.5f}`\n"
                f"━━━━━━━━━━━━━━━━━━\n"
                f"🎯 _Posición en ganancias — Continúa seguimiento_"
            )
        elif definitive:
            msg = (
                f"🚨 *SEÑAL DEFINITIVA — SMC PRO v2*\n"
                f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                f"*Dir:* {'📈 LONG' if direction=='LONG' else '📉 SHORT'}\n"
                f"*Score:* {score}/100 — ⭐ DEFINITIVA ⭐\n"
                f"*Confluencia:* >{MIN_DEFINITIVE_SCORE}%\n"
                f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                f"*Entrada:* `{price:.5f}`\n"
                f"*TP:* `{tp:.5f}` (+{abs(tp-price)/PIP:.1f}p)\n"
                f"*SL:* `{sl:.5f}` (-{abs(price-sl)/PIP:.1f}p)\n"
                f"*R:R:* 1:{rr:.2f}\n"
                f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                f"🎯 _Posición abierta — Seguimiento activo_"
            )
        else:
            label, _ = score_label(score)
            spread_txt = f"\nSpread: {tick['spread_pips']} pips" if tick else ""
            msg = (
                f"⚡ *SMC PRO v2 — EURUSD*\n"
                f"━━━━━━━━━━━━━━━━━━\n"
                f"*Dir:* {'📈 LONG' if direction=='LONG' else '📉 SHORT'}\n"
                f"*Score:* {score}/100 — {label}\n"
                f"━━━━━━━━━━━━━━━━━━\n"
                f"*Entrada:* `{price:.5f}`\n"
                f"*TP:* `{tp:.5f}` (+{abs(tp-price)/PIP:.1f}p)\n"
                f"*SL:* `{sl:.5f}` (-{abs(price-sl)/PIP:.1f}p)\n"
                f"*R:R:* 1:{rr:.2f}{spread_txt}\n"
                f"━━━━━━━━━━━━━━━━━━\n"
                f"⚠️ _Solo informativo. Usa siempre SL._"
            )
        return send_telegram_raw(msg, token=_token, chat_id=_chat_id)
    except Exception as e:
        logging.warning(f"send_telegram_alert: {e}")
        return False


def _build_hourly_telegram_message(
    signal: dict, score: int, session: str,
    dxy_dir: str, dxy_chg: float, dxy_trend: str,
    vol_spikes: list, delta: dict | None,
    consensus: dict, price: float | None,
    label: str, context_reasons: list | None,
    in_window: bool, win_label: str,
) -> str:
    """Build the 2h Telegram summary message."""
    _now_utc  = datetime.utcnow()
    _now_str  = _now_utc.strftime("%H:%M UTC")
    _price_str = f"`{price:.5f}`" if price else "N/A"

    if in_window:
        _dir    = signal.get("final_signal", "SIN SEÑAL")
        _entry  = signal.get("entry") or price
        _sl     = signal.get("stop_loss")
        _tp     = signal.get("take_profit")
        _buy    = signal.get("buy_signals", 0)
        _sell   = signal.get("sell_signals", 0)
        _regime = signal.get("regime") or signal.get("kb_regime_label", "N/A")
        _strat  = signal.get("strategy") or signal.get("kb_best_strategy", "")

        _delta_txt = ""
        if delta:
            _d_pct = delta.get("delta_pct", 0)
            _delta_txt = f"\n• Delta volumen: {'compradores' if _d_pct > 0 else 'vendedores'} dominan ({_d_pct:+.1f}%)"
        _spike_txt = (
            f"\n• ⚡ Spike de volumen detectado ({vol_spikes[0]['ratio']:.1f}x)"
            if vol_spikes else ""
        )
        _reasons_txt = ""
        if context_reasons:
            _reasons_txt = "\n".join(f"  ✦ {r}" for r in context_reasons[:3])

        _entry_block = ""
        if _entry and _sl and _tp and score >= 60:
            _tp_pips = abs(_tp - _entry) / PIP
            _sl_pips = abs(_entry - _sl) / PIP
            _rr      = _tp_pips / _sl_pips if _sl_pips else 0
            _entry_block = (
                f"\n━━━━━━━━━━━━━━━━━\n"
                f"🔑 *Posible Entrada:*\n"
                f"  Precio: `{_entry:.5f}`\n"
                f"  SL: `{_sl:.5f}` (-{_sl_pips:.1f}p)\n"
                f"  TP: `{_tp:.5f}` (+{_tp_pips:.1f}p)\n"
                f"  R:R 1:{_rr:.1f}"
            )
        elif score < 60:
            _entry_block = "\n⏸️ *Score bajo — sin entrada recomendada ahora*"

        msg = (
            f"⚡ *SMC Pro — Resumen 2h*\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"🕐 {_now_str} | {win_label}\n"
            f"💱 EUR/USD: {_price_str}\n"
            f"📊 Score: *{score}/100* — {label}\n"
            f"🎯 Señal: *{_dir}*  (▲{_buy} ▼{_sell})\n"
            f"📈 Régimen: {_regime}{(' | ' + _strat) if _strat else ''}\n"
            f"💵 DXY: {dxy_trend or 'N/A'} ({dxy_chg:+.2f}%){_delta_txt}{_spike_txt}"
        )
        if _reasons_txt:
            msg += f"\n\n*Confluencias clave:*\n{_reasons_txt}"
        msg += _entry_block
        msg += "\n━━━━━━━━━━━━━━━━━━\n⚠️ _Solo informativo. Usa siempre SL._"

    else:
        _recent_snaps = []
        if _DB_OK and _db:
            try:
                _recent_snaps = _db.get_recent_snapshots(hours=4, limit=25)
            except Exception:
                pass

        if _recent_snaps:
            _scores  = [s.get("score", 0) for s in _recent_snaps if s.get("score")]
            _avg_sc  = round(sum(_scores) / len(_scores), 1) if _scores else 0
            _max_sc  = max(_scores) if _scores else 0
            _signals = [s.get("signal", "") for s in _recent_snaps]
            _buys    = sum(1 for s in _signals if "COMPRA" in str(s) or "BUY" in str(s))
            _sells   = sum(1 for s in _signals if "VENTA" in str(s) or "SELL" in str(s))
            _bias    = "alcista 📈" if _buys > _sells else ("bajista 📉" if _sells > _buys else "neutral ⚪")

            _regimes = [s.get("regime", "") for s in _recent_snaps if s.get("regime")]
            _dominant_regime = max(set(_regimes), key=_regimes.count) if _regimes else "N/A"
            _tech = f"Régimen dominante: {_dominant_regime} | Señales: {_buys}↑ {_sells}↓ | Score medio: {_avg_sc}/100"

            _dxy_trends = [s.get("dxy_trend", "") for s in _recent_snaps if s.get("dxy_trend")]
            _dxy_dom = max(set(_dxy_trends), key=_dxy_trends.count) if _dxy_trends else "N/A"
            _news_sent = consensus.get("weighted_sentiment", 0) if isinstance(consensus, dict) else 0
            _fund = f"DXY: {_dxy_dom} ({dxy_chg:+.2f}%) | Sentimiento noticias: {'+' if _news_sent > 0 else ''}{_news_sent:.3f}"

            _snap_data  = [s.get("snapshot_data", {}) for s in _recent_snaps if s.get("snapshot_data")]
            _spikes     = sum(1 for d in _snap_data if isinstance(d, dict) and d.get("vol_spike"))
            _delta_vals = [d.get("delta_pct", 0) for d in _snap_data if isinstance(d, dict) and "delta_pct" in d]
            _avg_delta  = round(sum(_delta_vals) / len(_delta_vals), 1) if _delta_vals else 0
            _sent = f"Spikes volumen: {_spikes} | Delta medio: {_avg_delta:+.1f}% | Sesgo: {_bias}"

            _conclusion = (
                "✅ Sesión con confluencias sólidas"
                if _max_sc >= 75 and _buys > _sells else
                ("✅ Presión vendedora sostenida"
                 if _max_sc >= 75 and _sells > _buys else
                 "⚠️ Sesión sin señales claras — espera próxima ventana")
            )

            msg = (
                f"⏸️ *SMC Pro — Fuera de Horario*\n"
                f"━━━━━━━━━━━━━━━━━━\n"
                f"🕐 {_now_str} | {win_label}\n"
                f"📊 Resumen últimas 4h ({len(_recent_snaps)} análisis):\n"
                f"  Score: {_avg_sc}/100 medio | Máx: {_max_sc}/100\n"
                f"\n📈 *TÉCNICO:*\n  {_tech}\n"
                f"\n📰 *FUNDAMENTAL:*\n  {_fund}\n"
                f"\n💹 *VOLUMEN/SENTIMIENTO:*\n  {_sent}\n"
                f"\n━━━━━━━━━━━━━━━━━━\n"
                f"💡 *Conclusión:* {_conclusion}\n"
                f"🎯 Próxima ventana: {win_label}"
            )
        else:
            msg = (
                f"⏸️ *SMC Pro — Fuera de Horario*\n"
                f"🕐 {_now_str} | {win_label}\n"
                f"💱 EUR/USD: {_price_str}\n"
                f"💵 DXY: {dxy_trend or 'N/A'} ({dxy_chg:+.2f}%)\n"
                f"📊 Sin suficientes datos de la sesión anterior.\n"
                f"🎯 Próxima ventana: {win_label}"
            )

    return msg


def _build_urgent_telegram_message(signal: dict, score: int, reason: str) -> str:
    """Mensaje urgente cuando se detecta oportunidad o evento importante."""
    _now_utc = datetime.utcnow()
    _now_es  = _now_utc + timedelta(hours=UTC_OFFSET_SPAIN)
    _now_str = _now_es.strftime("%H:%M (España)")
    _price   = signal.get("price")
    _dir     = signal.get("final_signal", "N/A")
    _entry   = signal.get("entry") or _price
    _sl      = signal.get("stop_loss") or signal.get("sl")
    _tp      = signal.get("take_profit") or signal.get("tp")
    _regime  = signal.get("regime") or signal.get("kb_regime_label", "N/A")
    _strat   = signal.get("strategy") or signal.get("kb_best_strategy", "")
    _price_s = f"`{_price:.5f}`" if _price else "N/A"

    _entry_block = ""
    if _entry and _sl and _tp:
        _tp_p = abs(_tp - _entry) / PIP
        _sl_p = abs(_entry - _sl) / PIP
        _rr   = _tp_p / _sl_p if _sl_p else 0
        _entry_block = (
            f"\n━━━━━━━━━━━━━━━━━━\n"
            f"🔑 *Entrada sugerida:*\n"
            f"  Precio: `{_entry:.5f}`\n"
            f"  SL: `{_sl:.5f}` (-{_sl_p:.1f}p)\n"
            f"  TP: `{_tp:.5f}` (+{_tp_p:.1f}p)\n"
            f"  R:R 1:{_rr:.1f}"
        )

    return (
        f"🚨 *SMC Pro — ALERTA IMPORTANTE*\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"🕐 {_now_str}\n"
        f"💱 EUR/USD: {_price_s}\n"
        f"📊 Score: *{score}/100*\n"
        f"🎯 Señal: *{_dir}*\n"
        f"📈 Régimen: {_regime}{(' | ' + _strat) if _strat else ''}\n"
        f"⚠️ *Motivo:* {reason}"
        f"{_entry_block}\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"⚠️ _Solo informativo. Usa siempre SL._"
    )
