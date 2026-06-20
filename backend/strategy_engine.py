"""
backend/strategy_engine.py — Motor de selección de tácticas (la "estrategia propia").

IDEA CENTRAL (lo que el usuario llama "crear su propia estrategia"):
    El bot NO se casa con una táctica fija. Tiene un abanico pequeño de tácticas,
    cada una con HIPÓTESIS ECONÓMICA, y mide con datos —walk-forward, neto de
    costes— cuál tiene ventaja. Enciende las que la demuestran, apaga las que no.
    La estrategia EMERGE de la evidencia validada fuera de muestra.

POR QUÉ NO es data-mining (la trampa que mata cuentas):
    - El repertorio es PEQUEÑO y cada táctica tiene un porqué económico ANTES de
      ver los datos (no se inventan reglas buscando lo que más rinde en el pasado).
    - La selección es WALK-FORWARD: para decidir si una táctica está "encendida"
      en el momento i, solo se usa su rendimiento en 0..i-1. Nunca el futuro.
    - Se exige muestra mínima (anti-overfitting) y se restan costes reales.
    - La línea divisoria con el autoengaño: validación fuera de muestra, no
      optimización in-sample. Solo construimos la primera.

ESTADO: el motor está VACÍO hasta que haya trades cerrados suficientes. Eso es
correcto y honesto — sin evidencia no hay veredicto. El worker 24/7 lo va llenando.
"""
from __future__ import annotations

import logging
from collections import defaultdict

_log = logging.getLogger(__name__)

# ── Repertorio de tácticas con su hipótesis económica (el PORQUÉ) ─────────────
TACTICS: dict[str, dict] = {
    "pullback_trend": {
        "nombre": "Pullback en tendencia",
        "hipotesis": "En tendencia, el precio se extiende y vuelve al EMA21 antes de "
                     "continuar. Entrar en ese retroceso da mejor precio y la tendencia "
                     "viva empuja a favor. Continuación de momentum, WR histórico 48-56%.",
        "regimenes": ["trending_bull", "trending_bear", "volatile_trend"],
        "fuente": "Pullbacks al EMA21 baten breakouts (48-56% vs 32-38% WR).",
    },
    "meanrev_range": {
        "nombre": "Reversión a la media en rango",
        "hipotesis": "En mercado lateral, los extremos de Bollinger con RSI saturado "
                     "tienden a volver a la media. Sin tendencia que rompa, el precio "
                     "oscila entre soportes/resistencias.",
        "regimenes": ["ranging"],
        "fuente": "Mean-reversion solo en ausencia de tendencia (filtro de régimen).",
    },
    "news_reaction": {
        "nombre": "Reacción a sorpresa macro",
        "hipotesis": "Una sorpresa macro (dato vs previsión) reprecia la divisa de forma "
                     "persistente durante horas. Entrar en la dirección de la sorpresa "
                     "SOLO cuando el técnico confirma evita el latigazo inicial.",
        "regimenes": ["cualquiera"],
        "fuente": "Repricing post-dato. Requiere calendario en vivo (no existe en histórico).",
        "live_only": True,
    },
    "london_sweep_fvg": {
        "nombre": "London Sweep + FVG (ICT)",
        "hipotesis": "En la apertura de NY el precio barre la liquidez de los extremos del "
                     "rango de Londres (stops minoristas) y revierte, dejando un Fair Value "
                     "Gap que tiende a rellenarse. Se entra en el relleno del FVG, alineado "
                     "con la tendencia macro D1 EMA50.",
        "regimenes": ["cualquiera"],
        "fuente": "Portada de /api. Microestructura real (caza de liquidez + repricing).",
        # VEREDICTO de validación honesta sobre M15 real 18 años (con costes + OOS):
        # La versión que afirmaba ~76%/año era ilusión (look-ahead + fills perfectos).
        # Corregida y con slippage: -3.3%/año, 0/19 años positivos, consistente IS/OOS.
        # → SIN edge demostrado. Disponible y visible, pero el motor la mantiene OFF.
        "validated": {
            "edge": False,
            "avg_annual_net": -3.3,
            "note": "76%/año era look-ahead; corregida y con costes pierde. Gateada OFF.",
        },
        "default_off": True,
    },
}

MIN_TACTIC_SAMPLES = 25      # trades cerrados por táctica antes de un veredicto
COST_FLOOR_R       = 0.0     # expectancy neto de costes que separa ON/OFF


def derive_live_tactic(sig: dict) -> str:
    """
    Etiqueta qué táctica representa una señal EN VIVO (analyze_pair).
    Prioridad: reacción a noticia macro > pullback/reversión por régimen.
    """
    cal = sig.get("calendar", {}) or {}
    # Si el sesgo del calendario (sorpresa macro) coincide con la dirección y hay
    # confluencia técnica → es una reacción a noticia.
    if cal.get("bias") and cal.get("bias") == sig.get("direction") and sig.get("confluence"):
        return "news_reaction"
    rg = (sig.get("regime") or "")
    if rg in ("trending_bull", "trending_bear", "volatile_trend"):
        return "pullback_trend"
    if rg == "ranging":
        return "meanrev_range"
    # Si no hay régimen explícito en la señal, inferir por setup
    if sig.get("confluence"):
        return "pullback_trend"
    return "other"


def _r_of(trade: dict, rr_default: float = 3.0) -> float | None:
    """R real neto de costes de un trade (pips_netos / sl_pips)."""
    out = trade.get("outcome")
    if out not in ("TP", "SL", "WIN", "LOSS"):
        return None
    sl_pips = trade.get("sl_pips")
    pips    = trade.get("pips")
    if sl_pips and pips is not None and sl_pips > 0:
        return pips / sl_pips
    # Trades en papel (learning) traen r_multiple directo
    if trade.get("r_multiple") is not None:
        return float(trade["r_multiple"])
    return rr_default if out in ("TP", "WIN") else -1.0


def _tactic_of(trade: dict) -> str | None:
    feats = trade.get("feats") or {}
    if feats.get("tactic"):
        return feats["tactic"]
    # Trades en papel guardan features en 'features'
    pf = trade.get("features") or {}
    return pf.get("tactic")


def tactic_ledger(trades: list[dict], warmup: int = 20,
                  drop_threshold: float = -0.10) -> dict:
    """
    Ledger walk-forward por táctica. Procesa trades en orden y, para cada uno,
    decide si su táctica estaría ENCENDIDA usando SOLO el pasado de esa táctica.

    Devuelve, por táctica:
      n, win_rate, expectancy_r (neto costes), estado ON/OFF, motivo.
    Y un resumen de la 'estrategia emergente'.
    """
    # Orden temporal
    def _t(tr):
        return tr.get("time") or tr.get("opened_at") or tr.get("closed_at") or ""
    seq = sorted([t for t in trades if _tactic_of(t) and _r_of(t) is not None], key=_t)

    running: dict = defaultdict(lambda: {"sum": 0.0, "n": 0})
    final:   dict = defaultdict(lambda: {"r": [], "wins": 0, "kept": 0, "dropped": 0})

    for tr in seq:
        tac = _tactic_of(tr)
        r   = _r_of(tr)
        # ¿Encendida según el pasado de la táctica? (walk-forward)
        b = running[tac]
        on = True
        if b["n"] >= MIN_TACTIC_SAMPLES and (b["sum"] / b["n"]) < drop_threshold:
            on = False
        f = final[tac]
        f["r"].append(r)
        if r > 0:
            f["wins"] += 1
        if on:
            f["kept"] += 1
        else:
            f["dropped"] += 1
        # Aprender después de decidir
        b["sum"] += r; b["n"] += 1

    tactics_out = {}
    for tac, f in final.items():
        n = len(f["r"])
        exp = sum(f["r"]) / n if n else 0.0
        reliable = n >= MIN_TACTIC_SAMPLES
        meta = TACTICS.get(tac, {})
        validated = meta.get("validated", {})
        if validated.get("edge") is False:
            # Validada honestamente como SIN edge (backtest M15 con costes) → OFF
            estado = "OFF"
        elif not reliable:
            estado = "aprendiendo"
        elif exp > COST_FLOOR_R:
            estado = "ON"
        else:
            estado = "OFF"
        tactics_out[tac] = {
            "nombre":       meta.get("nombre", tac),
            "n":            n,
            "win_rate":     round(f["wins"] / n * 100, 1) if n else 0.0,
            "expectancy_r": round(exp, 3),
            "total_r":      round(sum(f["r"]), 1),
            "estado":       estado,
            "reliable":     reliable,
            "hipotesis":    meta.get("hipotesis", ""),
        }

    # Incluir tácticas validadas SIN edge aunque aún no tengan trades en vivo,
    # para que su veredicto quede VISIBLE (OFF documentado).
    for tac, meta in TACTICS.items():
        if tac in tactics_out:
            continue
        validated = meta.get("validated", {})
        if validated.get("edge") is False:
            tactics_out[tac] = {
                "nombre": meta.get("nombre", tac), "n": 0, "win_rate": 0.0,
                "expectancy_r": validated.get("avg_annual_net", 0.0), "total_r": 0.0,
                "estado": "OFF", "reliable": True, "hipotesis": meta.get("hipotesis", ""),
            }

    # Estrategia emergente = tácticas ON, ordenadas por edge
    on_tactics = sorted(
        [(t, d) for t, d in tactics_out.items() if d["estado"] == "ON"],
        key=lambda kv: kv[1]["expectancy_r"], reverse=True,
    )
    off_tactics = [t for t, d in tactics_out.items() if d["estado"] == "OFF"]

    return {
        "tactics":     tactics_out,
        "on":          [t for t, _ in on_tactics],
        "off":         off_tactics,
        "total":       len(seq),
        "veredicto":   _veredicto(on_tactics, off_tactics, len(seq)),
    }


def _veredicto(on_tactics, off_tactics, total) -> str:
    if total < MIN_TACTIC_SAMPLES:
        return (f"Sin veredicto aún: {total} operaciones cerradas. El bot necesita "
                f"~{MIN_TACTIC_SAMPLES}+ por táctica para decidir con honestidad.")
    if not on_tactics and not off_tactics:
        return "Reuniendo evidencia — ninguna táctica alcanza muestra fiable todavía."
    partes = []
    if on_tactics:
        nombres = ", ".join(d["nombre"] + f" ({d['expectancy_r']:+.2f}R)" for _, d in on_tactics)
        partes.append(f"OPERA: {nombres}")
    if off_tactics:
        partes.append(f"EVITA: {', '.join(off_tactics)} (sin edge neto de costes)")
    return " · ".join(partes)


def tactic_status() -> dict:
    """
    Conveniencia: lee los trades en papel cerrados + (si existe) un cache de trades
    de backtest, y devuelve el ledger. Pensado para el dashboard.
    """
    try:
        from backend.learning import _read_trades
        closed = [t for t in _read_trades() if t.get("status") == "closed"]
    except Exception:
        closed = []
    return tactic_ledger(closed)
