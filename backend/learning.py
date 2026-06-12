"""
backend/learning.py — Bucle de aprendizaje del bot (decisión → resultado → ajuste).

ESTO es lo que convierte el bot de "casino" en "entendimiento": cada señal operable
se guarda con su PORQUÉ completo (qué la generó), luego se verifica contra el mercado
real (¿tocó TP o SL?), y el sistema mide qué combinaciones de razones ganan de verdad.

Ciclo (cerrado, auditable):
  1. open_paper_trade(signal)   — registra la entrada con sus features (el porqué)
  2. evaluate_open_trades()     — comprueba con precio real si tocó TP/SL → cierra
  3. learning_report()          — agrega por feature: win rate y expectancy reales
  4. edge_adjustment(features)  — ajusta el score de señales nuevas según lo aprendido

PRINCIPIOS (CLAUDE.md):
  - Nada de fe: un feature solo "cuenta" con muestra suficiente (MIN_SAMPLES).
  - Ajustes SUAVES y acotados — el aprendizaje afina el filtro, no lo reinventa.
    Un edge de 30 ops no justifica cambiar el sistema; sí justifica un empujón.
  - Expectancy (no win rate) manda: 45% WR con 1:3 RR gana; 70% WR con 1:0.5 pierde.
  - Fail-closed: si no podemos evaluar un trade (sin datos), queda 'pending', no se
    inventa resultado.

POR QUÉ NO promete 22/30: porque mide la realidad en vez de desearla. El techo real
emerge de los datos; el aprendizaje nos acerca a ese techo, no a una fantasía.
"""
from __future__ import annotations

import json
import logging
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

_log = logging.getLogger(__name__)

_BASE_DIR = Path(__file__).parent.parent
PAPER_TRADES = _BASE_DIR / "paper_trades.jsonl"

MIN_SAMPLES = 20          # nº mínimo de trades cerrados para fiarse de un feature
MAX_ADJ     = 8           # ajuste máx (±) al score que el aprendizaje puede aplicar
RR_DEFAULT  = 3.0         # 1:3 — coherente con la estrategia


# ─────────────────────────────────────────────────────────────────────────────
# 1. REGISTRO DE ENTRADAS (el porqué)
# ─────────────────────────────────────────────────────────────────────────────

def _features_of(sig: dict) -> dict:
    """Extrae las 'razones' de una señal en etiquetas agregables (el porqué medible)."""
    cal = sig.get("calendar", {}) or {}
    ns  = sig.get("news_sentiment", {}) or {}
    hour = None
    try:
        hour = datetime.now(timezone.utc).hour
    except Exception:
        pass
    return {
        "pair":        sig.get("symbol"),
        "direction":   sig.get("direction"),
        "confluence":  bool(sig.get("confluence")),
        "setup_grade": sig.get("setup_grade", "normal"),
        "news_dir":    ns.get("direction", "NEUTRAL"),
        "calendar_clean": not bool(cal.get("block")),
        "calendar_bias_match": (cal.get("bias") == sig.get("direction") and bool(cal.get("bias"))),
        "session":     _session_of(hour),
        "score_band":  _score_band(int(sig.get("score") or 0)),
    }


def _session_of(hour) -> str:
    if hour is None:
        return "unknown"
    if 0 <= hour < 7:    return "asia"
    if 7 <= hour < 12:   return "london"
    if 12 <= hour < 16:  return "overlap"   # London+NY: máxima liquidez
    if 16 <= hour < 21:  return "newyork"
    return "late"


def _score_band(score: int) -> str:
    if score >= 85: return "85+"
    if score >= 75: return "75-84"
    if score >= 65: return "65-74"
    return "<65"


def open_paper_trade(sig: dict) -> bool:
    """
    Registra una señal operable como trade en papel (entrada virtual).
    Solo si tiene dirección, precio, TP y SL. Idempotente por (symbol, entry_ts redondeado).
    """
    direction = sig.get("direction")
    price = sig.get("price")
    tp    = sig.get("tp1") or sig.get("tp")
    sl    = sig.get("sl")
    if direction not in ("LONG", "SHORT") or not (price and tp and sl):
        return False

    now = datetime.now(timezone.utc)
    # Idempotencia: no abrir dos trades del mismo par en la misma vela 15m
    bucket = now.replace(minute=(now.minute // 15) * 15, second=0, microsecond=0)
    trade_id = f"{sig.get('symbol')}_{bucket.isoformat()}"

    for t in _read_trades():
        if t.get("trade_id") == trade_id and t.get("status") == "open":
            return False  # ya existe

    trade = {
        "trade_id":  trade_id,
        "symbol":    sig.get("symbol"),
        "direction": direction,
        "entry":     float(price),
        "tp":        float(tp),
        "sl":        float(sl),
        "opened_at": now.isoformat(),
        "status":    "open",
        "outcome":   None,        # 'WIN' | 'LOSS' | None
        "r_multiple": None,
        "features":  _features_of(sig),
        "why":       sig.get("vote_log", [])[:12],
    }
    _append_trade(trade)
    return True


# ─────────────────────────────────────────────────────────────────────────────
# 2. EVALUACIÓN CONTRA MERCADO REAL (¿tocó TP o SL?)
# ─────────────────────────────────────────────────────────────────────────────

def evaluate_open_trades(max_age_hours: int = 72) -> dict:
    """
    Para cada trade abierto, baja el precio desde la entrada y comprueba si tocó
    TP o SL (lo que ocurra primero, vela a vela). Cierra los resueltos.
    Devuelve resumen {evaluated, closed_win, closed_loss, still_open}.
    """
    from backend.multi_pair import get_pair_ohlc

    trades = _read_trades()
    now = datetime.now(timezone.utc)
    closed_win = closed_loss = still_open = evaluated = 0
    changed = False

    # Cache de OHLC por símbolo para no descargar 7 veces
    ohlc_cache: dict = {}

    for t in trades:
        if t.get("status") != "open":
            continue
        evaluated += 1
        try:
            opened = datetime.fromisoformat(t["opened_at"])
            if opened.tzinfo is None:
                opened = opened.replace(tzinfo=timezone.utc)
        except Exception:
            continue

        age_h = (now - opened).total_seconds() / 3600
        sym = t["symbol"]
        if sym not in ohlc_cache:
            # 15m da resolución suficiente para ver el orden TP/SL
            ohlc_cache[sym] = get_pair_ohlc(sym, "5d", "15m")
        df = ohlc_cache[sym]
        if df is None or df.empty:
            still_open += 1
            continue

        # Barras posteriores a la entrada
        try:
            df_after = df[df.index.tz_convert("UTC") >= opened] if df.index.tz is not None \
                       else df[df.index >= opened.replace(tzinfo=None)]
        except Exception:
            df_after = df[df.index >= opened.replace(tzinfo=None)]
        if df_after.empty:
            still_open += 1
            continue

        outcome = _scan_outcome(t, df_after)
        if outcome is None:
            # Sin resolver. Si es muy viejo, cerrar a mercado (timeout) para no acumular.
            if age_h > max_age_hours:
                last = float(df_after["Close"].iloc[-1])
                r = _r_from_price(t, last)
                t.update(status="closed", outcome=("WIN" if r > 0 else "LOSS"),
                         r_multiple=round(r, 2), closed_at=now.isoformat(),
                         close_reason="timeout")
                changed = True
                if r > 0: closed_win += 1
                else:     closed_loss += 1
            else:
                still_open += 1
            continue

        t.update(status="closed", outcome=outcome["outcome"],
                 r_multiple=outcome["r"], closed_at=now.isoformat(),
                 close_reason=outcome["reason"])
        changed = True
        if outcome["outcome"] == "WIN": closed_win += 1
        else:                           closed_loss += 1

    if changed:
        _write_trades(trades)

    return {"evaluated": evaluated, "closed_win": closed_win,
            "closed_loss": closed_loss, "still_open": still_open}


def _scan_outcome(t: dict, df_after) -> dict | None:
    """Recorre velas tras la entrada; devuelve el primer toque de TP o SL."""
    entry, tp, sl, direction = t["entry"], t["tp"], t["sl"], t["direction"]
    for _, row in df_after.iterrows():
        hi = float(row["High"]); lo = float(row["Low"])
        if direction == "LONG":
            hit_sl = lo <= sl
            hit_tp = hi >= tp
        else:
            hit_sl = hi >= sl
            hit_tp = lo <= tp
        # Si una vela toca ambos, asumimos lo PEOR (SL primero) — conservador,
        # evita sobreestimar el sistema (sesgo optimista mata cuentas).
        if hit_sl:
            return {"outcome": "LOSS", "r": -1.0, "reason": "SL"}
        if hit_tp:
            r = abs(tp - entry) / abs(entry - sl) if abs(entry - sl) > 0 else RR_DEFAULT
            return {"outcome": "WIN", "r": round(r, 2), "reason": "TP"}
    return None


def _r_from_price(t: dict, price: float) -> float:
    risk = abs(t["entry"] - t["sl"])
    if risk <= 0:
        return 0.0
    move = (price - t["entry"]) if t["direction"] == "LONG" else (t["entry"] - price)
    return move / risk


# ─────────────────────────────────────────────────────────────────────────────
# 3. INFORME DE APRENDIZAJE (qué funciona de verdad)
# ─────────────────────────────────────────────────────────────────────────────

def learning_report() -> dict:
    """
    Agrega los trades cerrados por feature. Para cada valor de cada feature:
    nº ops, win rate, expectancy (en R). Solo se reporta como 'fiable' si tiene
    ≥ MIN_SAMPLES.
    """
    closed = [t for t in _read_trades() if t.get("status") == "closed"]
    overall = _stats([t["r_multiple"] for t in closed if t.get("r_multiple") is not None],
                     [t["outcome"] for t in closed])

    feature_keys = ["pair", "direction", "confluence", "setup_grade", "news_dir",
                    "calendar_clean", "calendar_bias_match", "session", "score_band"]
    by_feature: dict = {}
    for fk in feature_keys:
        buckets = defaultdict(lambda: {"r": [], "out": []})
        for t in closed:
            val = (t.get("features") or {}).get(fk)
            if val is None:
                continue
            buckets[str(val)]["r"].append(t.get("r_multiple") or 0.0)
            buckets[str(val)]["out"].append(t.get("outcome"))
        by_feature[fk] = {
            val: {**_stats(d["r"], d["out"]),
                  "reliable": len(d["r"]) >= MIN_SAMPLES}
            for val, d in buckets.items()
        }

    return {"overall": overall, "by_feature": by_feature,
            "total_closed": len(closed)}


def _stats(rs: list, outs: list) -> dict:
    n = len(rs)
    if n == 0:
        return {"n": 0, "win_rate": 0.0, "expectancy_r": 0.0, "total_r": 0.0}
    wins = sum(1 for o in outs if o == "WIN")
    return {
        "n": n,
        "win_rate": round(wins / n * 100, 1),
        "expectancy_r": round(sum(rs) / n, 3),
        "total_r": round(sum(rs), 2),
    }


# ─────────────────────────────────────────────────────────────────────────────
# 4. AJUSTE ADAPTATIVO (el sistema afina el filtro con lo aprendido)
# ─────────────────────────────────────────────────────────────────────────────

def edge_adjustment(sig: dict) -> tuple[int, list[str]]:
    """
    Dada una señal nueva, devuelve un ajuste de score (acotado a ±MAX_ADJ) y las
    razones, basándose SOLO en features con muestra fiable y expectancy clara.

    Filosofía: empuja hacia lo que históricamente tuvo expectancy positiva y aleja
    de lo que la tuvo negativa. Suave y honesto — no decide solo, ajusta el peso.
    """
    rep = learning_report()
    if rep["total_closed"] < MIN_SAMPLES:
        return 0, [f"Aprendizaje: aún {rep['total_closed']}/{MIN_SAMPLES} trades — sin ajuste"]

    feats = _features_of(sig)
    adj = 0.0
    reasons: list[str] = []
    for fk, val in feats.items():
        stats = rep["by_feature"].get(fk, {}).get(str(val))
        if not stats or not stats.get("reliable"):
            continue
        exp = stats["expectancy_r"]
        # Expectancy en R → empujón proporcional, acotado
        if exp >= 0.25:
            bump = min(3, round(exp * 2))
            adj += bump
            reasons.append(f"+{bump} {fk}={val} (exp {exp:+.2f}R, {stats['n']} ops, WR {stats['win_rate']}%)")
        elif exp <= -0.25:
            bump = max(-3, round(exp * 2))
            adj += bump
            reasons.append(f"{bump} {fk}={val} (exp {exp:+.2f}R, {stats['n']} ops, WR {stats['win_rate']}%)")

    adj = int(max(-MAX_ADJ, min(MAX_ADJ, adj)))
    if not reasons:
        reasons.append("Aprendizaje: features sin edge claro — sin ajuste")
    return adj, reasons


# ─────────────────────────────────────────────────────────────────────────────
# 5. APRENDIZAJE WALK-FORWARD SOBRE BACKTEST (sin look-ahead)
# ─────────────────────────────────────────────────────────────────────────────

def walkforward_learning(trades: list[dict], rr: float = RR_DEFAULT,
                         warmup: int = 30, drop_threshold: float = -0.15) -> dict:
    """
    Simula el aprendizaje sobre trades históricos de forma HONESTA (walk-forward):
    para decidir si filtrar el trade i, usa SOLO la evidencia de los trades 0..i-1.
    Nunca mira el futuro. Así medimos el efecto real del aprendizaje, no una fantasía.

    POR QUÉ así (CLAUDE.md, anti-overfitting #2):
        Aprender y testear sobre los MISMOS datos infla el resultado: el sistema
        "memoriza" el pasado. Walk-forward replica lo que pasaría en vivo — solo
        sabes lo que ya ocurrió. Si el aprendizaje no ayuda walk-forward, no ayuda.

    LIMITACIÓN HONESTA: en histórico solo hay features 'regime', 'session', 'dir'.
        Noticias y calendario NO existen en datos pasados (el feed es de esta semana),
        así que el aprendizaje EN VIVO será más rico que esta simulación.

    Devuelve: baseline (todos los trades) vs learned (filtrando lo que el bot habría
    aprendido a evitar), con n, win_rate, expectancy_r y total_r de cada uno.
    """
    # Normalizar: cada trade → (R, feats). TP=+rr, SL=-1. Ignorar OPEN.
    seq = []
    for t in trades:
        out = t.get("outcome")
        if out == "TP":
            r = rr
        elif out == "SL":
            r = -1.0
        else:
            continue
        feats = t.get("feats") or {}
        seq.append((r, {k: feats.get(k) for k in ("regime", "session", "dir")}))

    if len(seq) < warmup + 10:
        return {"enough": False, "n_total": len(seq), "warmup": warmup}

    buckets: dict = defaultdict(lambda: {"sum": 0.0, "n": 0})
    base_r, learn_r = [], []
    dropped = 0

    for idx, (r, feats) in enumerate(seq):
        base_r.append(r)
        # Decisión con SOLO el pasado (walk-forward)
        keep = True
        if idx >= warmup:
            exps = []
            for fk, val in feats.items():
                if val is None:
                    continue
                b = buckets[(fk, str(val))]
                if b["n"] >= MIN_SAMPLES:
                    exps.append(b["sum"] / b["n"])
            # Si la expectancy media aprendida de sus features es claramente mala → evitar
            if exps and (sum(exps) / len(exps)) < drop_threshold:
                keep = False
        if keep:
            learn_r.append(r)
        else:
            dropped += 1
        # Aprender DESPUÉS de decidir (el trade i alimenta a los i+1)
        for fk, val in feats.items():
            if val is None:
                continue
            b = buckets[(fk, str(val))]
            b["sum"] += r; b["n"] += 1

    def _m(rs):
        n = len(rs)
        if n == 0:
            return {"n": 0, "win_rate": 0.0, "expectancy_r": 0.0, "total_r": 0.0}
        wins = sum(1 for x in rs if x > 0)
        return {"n": n, "win_rate": round(wins / n * 100, 1),
                "expectancy_r": round(sum(rs) / n, 3), "total_r": round(sum(rs), 1)}

    return {"enough": True, "baseline": _m(base_r), "learned": _m(learn_r),
            "dropped": dropped, "warmup": warmup, "drop_threshold": drop_threshold}


# ─────────────────────────────────────────────────────────────────────────────
# Persistencia JSONL
# ─────────────────────────────────────────────────────────────────────────────

def _read_trades() -> list[dict]:
    if not PAPER_TRADES.exists():
        return []
    out = []
    try:
        for ln in PAPER_TRADES.read_text(encoding="utf-8").strip().splitlines():
            if ln.strip():
                try:
                    out.append(json.loads(ln))
                except Exception:
                    pass
    except Exception as e:
        _log.warning("_read_trades: %s", e)
    return out


def _append_trade(trade: dict) -> None:
    try:
        with open(PAPER_TRADES, "a", encoding="utf-8") as f:
            f.write(json.dumps(trade, default=str) + "\n")
    except Exception as e:
        _log.warning("_append_trade: %s", e)


def _write_trades(trades: list[dict]) -> None:
    try:
        with open(PAPER_TRADES, "w", encoding="utf-8") as f:
            for t in trades:
                f.write(json.dumps(t, default=str) + "\n")
    except Exception as e:
        _log.warning("_write_trades: %s", e)
