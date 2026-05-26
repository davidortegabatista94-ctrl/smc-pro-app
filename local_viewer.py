"""
local_viewer.py — Visor MT5 local para SMC Pro
================================================
Este proceso corre SOLO en Windows (necesita MetaTrader5 instalado).
No hace análisis propio. Railway es el servidor real:
  - Lee señales de la BD compartida (escritas por Railway cada 5 min)
  - Conecta con MT5 local y ejecuta órdenes
  - Escribe resultados de trades en la BD (Railway los ve en tiempo real)
  - Auto-refresca cada 2 minutos

Flujo:
  Railway genera señal → BD → este visor la lee → MT5 ejecuta → BD → Railway reporta
"""

import streamlit as st
import time
import logging
import os
from datetime import datetime, timezone

logging.basicConfig(level=logging.WARNING)

RAILWAY_URL  = os.environ.get("RAILWAY_URL", "https://smc-pro-app-production.up.railway.app")
SYMBOL       = "EURUSD"
PIP          = 0.0001
REFRESH_SECS = 120   # 2 minutos
MIN_SCORE    = 70    # score mínimo para autotrading

# ── Página ────────────────────────────────────────────────────────────────────
st.set_page_config(
    page_title="SMC Pro — MT5 Bridge",
    page_icon="⚡",
    layout="wide",
)

# ── BD compartida (mismo PostgreSQL que Railway) ──────────────────────────────
try:
    import db as _db
    _DB_OK = True
except Exception as _e:
    _DB_OK = False
    st.error(f"❌ No se puede conectar a la BD: {_e}")

# ── MT5 ───────────────────────────────────────────────────────────────────────
_mt5_mod   = None
_mt5_conn  = False
_mt5_error = ""

def _get_mt5():
    global _mt5_mod
    if _mt5_mod is None:
        try:
            import MetaTrader5 as _m
            _mt5_mod = _m
        except Exception as e:
            _mt5_mod = False
            globals()["_mt5_error"] = str(e)
    return _mt5_mod

def mt5_connect(login=None, password=None, server=None) -> tuple[bool, str]:
    global _mt5_conn, _mt5_error
    mt5 = _get_mt5()
    if mt5 is False:
        return False, f"MetaTrader5 no instalado: {_mt5_error}"
    if _mt5_conn:
        return True, "Ya conectado"
    try:
        if not mt5.initialize():
            err = str(mt5.last_error())
            _mt5_error = err
            return False, f"initialize() falló: {err}"
        if login and password:
            kw = {"login": int(login), "password": str(password)}
            if server:
                kw["server"] = server
            if not mt5.login(**kw):
                err = str(mt5.last_error())
                mt5.shutdown()
                _mt5_error = err
                return False, f"login() falló: {err}"
        _mt5_conn = True
        return True, "✅ Conectado"
    except Exception as e:
        _mt5_error = str(e)
        return False, str(e)

def mt5_account() -> dict | None:
    mt5 = _get_mt5()
    if not mt5 or not _mt5_conn:
        return None
    try:
        i = mt5.account_info()
        if not i:
            return None
        return {
            "balance":   i.balance,
            "equity":    i.equity,
            "profit":    i.profit,
            "free":      i.margin_free,
            "leverage":  i.leverage,
            "currency":  i.currency,
            "server":    i.server,
            "name":      i.name,
        }
    except Exception:
        return None

def mt5_tick() -> dict | None:
    mt5 = _get_mt5()
    if not mt5 or not _mt5_conn:
        return None
    try:
        t = mt5.symbol_info_tick(SYMBOL)
        if not t:
            return None
        return {
            "bid":    t.bid,
            "ask":    t.ask,
            "spread": round((t.ask - t.bid) / PIP, 1),
        }
    except Exception:
        return None

def mt5_positions() -> list:
    mt5 = _get_mt5()
    if not mt5 or not _mt5_conn:
        return []
    try:
        pos = mt5.positions_get(symbol=SYMBOL)
        if not pos:
            return []
        result = []
        for p in pos:
            result.append({
                "ticket":  p.ticket,
                "type":    "LONG" if p.type == 0 else "SHORT",
                "volume":  p.volume,
                "open":    p.price_open,
                "current": p.price_current,
                "sl":      p.sl,
                "tp":      p.tp,
                "profit":  p.profit,
            })
        return result
    except Exception:
        return []

def mt5_can_trade() -> tuple[bool, str]:
    mt5 = _get_mt5()
    if not mt5:
        return False, "MT5 no disponible"
    try:
        info = mt5.terminal_info()
        if info is None:
            return False, "No se obtuvo terminal_info()"
        allowed = getattr(info, "trade_allowed", None)
        if allowed is False:
            return False, "AutoTrading deshabilitado en MT5"
        return True, "Trading permitido"
    except Exception as e:
        return False, str(e)

def mt5_place_order(direction: str, volume: float, price: float,
                    sl: float, tp: float) -> dict:
    mt5 = _get_mt5()
    if not mt5 or not _mt5_conn:
        return {"success": False, "error": "MT5 no conectado"}
    ok, msg = mt5_can_trade()
    if not ok:
        return {"success": False, "error": msg}
    try:
        otype = mt5.ORDER_TYPE_BUY if direction == "LONG" else mt5.ORDER_TYPE_SELL
        req = {
            "action":       mt5.TRADE_ACTION_DEAL,
            "symbol":       SYMBOL,
            "volume":       float(volume),
            "type":         otype,
            "price":        float(price),
            "sl":           float(sl),
            "tp":           float(tp),
            "deviation":    10,
            "magic":        234567,
            "comment":      "SMC Bot",
            "type_time":    mt5.ORDER_TIME_GTC,
            "type_filling": mt5.ORDER_FILLING_IOC,
        }
        res = mt5.order_send(req)
        if res is None or res.retcode != 10009:
            code = res.retcode if res else "None"
            return {"success": False, "error": f"retcode={code}"}
        return {"success": True, "ticket": res.order, "price": res.price}
    except Exception as e:
        return {"success": False, "error": str(e)}

def mt5_close(ticket: int) -> dict:
    mt5 = _get_mt5()
    if not mt5 or not _mt5_conn:
        return {"success": False, "error": "MT5 no conectado"}
    try:
        pos = mt5.positions_get(ticket=ticket)
        if not pos:
            return {"success": False, "error": "Posición no encontrada"}
        p = pos[0]
        otype = mt5.ORDER_TYPE_SELL if p.type == 0 else mt5.ORDER_TYPE_BUY
        tick  = mt5.symbol_info_tick(p.symbol)
        price = tick.bid if p.type == 0 else tick.ask
        req = {
            "action":       mt5.TRADE_ACTION_DEAL,
            "position":     ticket,
            "symbol":       p.symbol,
            "volume":       p.volume,
            "type":         otype,
            "price":        price,
            "deviation":    10,
            "magic":        234567,
            "comment":      "SMC Bot Close",
            "type_time":    mt5.ORDER_TIME_GTC,
            "type_filling": mt5.ORDER_FILLING_IOC,
        }
        res = mt5.order_send(req)
        if res is None or res.retcode != 10009:
            return {"success": False, "error": f"retcode={res.retcode if res else 'None'}"}
        return {"success": True, "price": res.price}
    except Exception as e:
        return {"success": False, "error": str(e)}

# ── CSS ───────────────────────────────────────────────────────────────────────
st.markdown("""<style>
[data-testid="stAppViewContainer"]{background:#060a10}
[data-testid="stHeader"]{display:none!important}
[data-testid="stSidebar"]{background:#080d16;border-right:1px solid #151d2e}
.block-container{padding:1rem 1.5rem 2rem}
h1,h2,h3,p,label,span{color:#e0e8f0}
[data-testid="stMetricValue"]{color:#e0e8f0!important;font-size:1.4rem!important}
[data-testid="stMetricLabel"]{color:#6b8aaa!important;font-size:0.75rem!important}
.smc-hdr{background:#0b0f18;border:1px solid #151d2e;border-radius:10px;
  padding:12px 20px;display:flex;justify-content:space-between;align-items:center;margin-bottom:14px}
.smc-logo{color:#3d7eff;font-weight:800;font-size:17px;letter-spacing:-.5px}
.bdg{border-radius:6px;padding:3px 10px;font-size:11px;font-weight:600;display:inline-block}
.bdg-b{background:rgba(61,126,255,.12);color:#3d7eff;border:1px solid rgba(61,126,255,.25)}
.bdg-g{background:rgba(16,185,129,.12);color:#10b981;border:1px solid rgba(16,185,129,.25)}
.bdg-r{background:rgba(248,113,113,.12);color:#f87171;border:1px solid rgba(248,113,113,.25)}
.bdg-y{background:rgba(245,158,11,.12);color:#f59e0b;border:1px solid rgba(245,158,11,.25)}
.bdg-x{background:rgba(107,114,128,.10);color:#9ca3af;border:1px solid rgba(107,114,128,.2)}
.smc-rail{background:linear-gradient(90deg,#0d1829,#060e1c);border:1px solid #3d7eff33;
  border-radius:10px;padding:10px 16px;margin-bottom:14px;
  display:flex;align-items:center;justify-content:space-between;gap:10px}
.smc-rail-btn{background:#3d7eff;color:#fff!important;padding:5px 14px;border-radius:6px;
  font-size:12px;font-weight:700;text-decoration:none!important;white-space:nowrap}
.smc-card{background:#0b0f18;border:1px solid #151d2e;border-radius:10px;padding:16px 20px;margin:6px 0}
.sig-b{border-left:3px solid #10b981;background:rgba(16,185,129,.04)!important}
.sig-s{border-left:3px solid #f87171;background:rgba(248,113,113,.04)!important}
.sig-n{border-left:3px solid #6b7280;background:rgba(107,114,128,.03)!important}
.sig-dir{font-size:26px;font-weight:800;letter-spacing:-1px;margin-bottom:4px}
.sig-price{font-family:monospace;font-size:20px;font-weight:700;color:#e0e8f0}
.sig-meta{color:#6b8aaa;font-size:12px;margin-top:6px}
.sc-track{height:5px;background:#151d2e;border-radius:3px;margin-top:8px;overflow:hidden}
.sc-fill{height:100%;border-radius:3px;transition:width .4s}
.pos-card{background:#0b0f18;border:1px solid #151d2e;border-radius:10px;
  padding:14px 18px;margin:4px 0;display:flex;justify-content:space-between;align-items:center}
.pos-open{border-left:3px solid #3d7eff!important}
.pos-long{border-left:3px solid #10b981!important}
.pos-short{border-left:3px solid #f87171!important}
.pos-profit-pos{color:#10b981;font-weight:700;font-family:monospace}
.pos-profit-neg{color:#f87171;font-weight:700;font-family:monospace}
div[data-testid="stButton"]>button{
  background:#0b0f18;border:1px solid #151d2e;color:#e0e8f0;border-radius:8px;
  font-size:13px;padding:8px 18px;transition:all .2s}
div[data-testid="stButton"]>button:hover{border-color:#3d7eff;color:#3d7eff}
div[data-testid="stButton"]>button[kind="primary"]{
  background:#10b981;border-color:#10b981;color:#fff;font-weight:700}
div[data-testid="stButton"]>button[kind="primary"]:hover{background:#059669}
[data-testid="stTextInput"]>div>div>input,
[data-testid="stSelectbox"]>div>div>div{
  background:#0b0f18!important;border:1px solid #1e2a3a!important;color:#e0e8f0!important;border-radius:8px!important}
[data-testid="stForm"]{border:none!important;padding:0!important}
</style>""", unsafe_allow_html=True)

# ── Login ─────────────────────────────────────────────────────────────────────
_USERS = {"david": "david", "javi": "javi"}
_NAMES = {"david": "David", "javi": "Javi"}

if "user" not in st.session_state:
    st.session_state.user      = None
    st.session_state.mt5_login = ""
    st.session_state.mt5_pass  = ""
    st.session_state.mt5_srv   = ""
    st.session_state.autotr    = False
    st.session_state.last_rf   = 0.0
    st.session_state.last_exec = ""  # ticket del último trade ejecutado

if st.session_state.user is None:
    st.markdown("""<style>.block-container{max-width:420px!important;padding-top:90px!important}</style>""",
                unsafe_allow_html=True)
    st.markdown("### ⚡ SMC Pro — MT5 Bridge")
    with st.form("_lf"):
        _lu = st.selectbox("Usuario", list(_USERS.keys()))
        _lp = st.text_input("Contraseña", type="password")
        _ls = st.form_submit_button("🔐 Entrar", use_container_width=True)
    if _ls:
        ok = False
        if _DB_OK:
            try:
                ok = _db.authenticate_user(_lu, _lp)
            except Exception:
                pass
        if not ok:
            ok = (_USERS.get(_lu) == _lp)
        if ok:
            st.session_state.user = _lu
            if _DB_OK:
                try:
                    _db.update_last_login(_lu)
                except Exception:
                    pass
            st.rerun()
        else:
            st.error("❌ Contraseña incorrecta")
    st.stop()

user = st.session_state.user
name = _NAMES.get(user, user.capitalize())

# ── Cargar credenciales MT5 guardadas en BD ───────────────────────────────────
if "mt5_creds_loaded" not in st.session_state and _DB_OK:
    try:
        creds = _db.load_user_mt5(user)
        if creds:
            st.session_state.mt5_login = str(creds.get("login", ""))
            st.session_state.mt5_pass  = str(creds.get("password", ""))
            st.session_state.mt5_srv   = str(creds.get("server", ""))
    except Exception:
        pass
    st.session_state.mt5_creds_loaded = True

# ── Auto-refresh ──────────────────────────────────────────────────────────────
_now = time.time()
_elapsed = _now - st.session_state.last_rf

@st.fragment(run_every=REFRESH_SECS)
def _refresh_tick():
    st.session_state.last_rf = time.time()

_refresh_tick()

# ── Header ────────────────────────────────────────────────────────────────────
_htime = datetime.now(timezone.utc).strftime("%H:%M UTC")
_mt5_avail = _get_mt5() is not False
_conn_cls  = "bdg-g" if _mt5_conn else ("bdg-y" if _mt5_avail else "bdg-r")
_conn_txt  = "MT5 conectado" if _mt5_conn else ("MT5 disponible" if _mt5_avail else "MT5 no encontrado")

st.markdown(f"""<div class="smc-hdr">
  <div style="display:flex;align-items:center;gap:10px">
    <span class="smc-logo">⚡ SMC Pro</span>
    <span style="color:#6b8aaa;font-size:13px">MT5 Bridge</span>
    <span class="bdg {_conn_cls}">{_conn_txt}</span>
  </div>
  <div style="display:flex;align-items:center;gap:8px">
    <span class="bdg bdg-b">👤 {name}</span>
    <span style="color:#6b8aaa;font-size:12px">{_htime}</span>
  </div>
</div>""", unsafe_allow_html=True)

# ── Banner Railway ────────────────────────────────────────────────────────────
st.markdown(f"""<div class="smc-rail">
  <span style="color:#8899aa;font-size:13px">
    📡 <strong style="color:#3d7eff">Railway</strong> genera las señales 24/7 ·
    Este visor las ejecuta en MT5 · Datos sincronizados en BD compartida
  </span>
  <a href="{RAILWAY_URL}" target="_blank" class="smc-rail-btn">🌐 Panel completo →</a>
</div>""", unsafe_allow_html=True)

# ── Layout ────────────────────────────────────────────────────────────────────
col_left, col_right = st.columns([3, 2], gap="large")

# ═══════════════════════════════════════════════════════
# COLUMNA IZQUIERDA — Señal + Posiciones MT5
# ═══════════════════════════════════════════════════════
with col_left:

    # ── Última señal de Railway (desde BD) ───────────────
    st.markdown("#### 📊 Última señal del servidor")
    snap = None
    if _DB_OK:
        try:
            snap = _db.get_last_snapshot()
        except Exception:
            pass

    if snap and snap.get("price"):
        price  = float(snap.get("price", 0))
        sig    = snap.get("signal", "NEUTRAL")
        score  = int(snap.get("score", 0))
        regime = snap.get("regime", "—")
        _ts    = str(snap.get("created_at", ""))[:16]
        extra  = snap.get("snapshot_data") or {}
        if isinstance(extra, str):
            import json as _json
            try:
                extra = _json.loads(extra)
            except Exception:
                extra = {}

        _isbuy  = "COMPRA" in sig.upper() or "BUY"  in sig.upper()
        _issell = "VENTA"  in sig.upper() or "SELL" in sig.upper()
        _scls   = "sig-b" if _isbuy else ("sig-s" if _issell else "sig-n")
        _dico   = "🟢 COMPRA" if _isbuy else ("🔴 VENTA" if _issell else "⚪ NEUTRAL")
        _dirbr  = "LONG"  if _isbuy else ("SHORT" if _issell else None)
        _sc_col = "#10b981" if score >= 70 else ("#f59e0b" if score >= 50 else "#f87171")
        rsi     = extra.get("rsi", 0)
        sess    = extra.get("session", "—")
        atr     = extra.get("atr_1h_pips", 0)

        st.markdown(f"""<div class="smc-card {_scls}">
  <div class="sig-dir">{_dico}</div>
  <div class="sig-price">{price:.5f}</div>
  <div class="sig-meta">
    <span class="bdg bdg-x">Score {score}/100</span> &nbsp;
    <span class="bdg bdg-x">RSI {rsi:.1f}</span> &nbsp;
    <span class="bdg bdg-x">ATR {atr:.1f}p</span> &nbsp;
    <span class="bdg bdg-x">📍 {sess}</span> &nbsp;
    <span class="bdg bdg-x">⚙ {regime}</span>
  </div>
  <div class="sc-track"><div class="sc-fill" style="width:{score}%;background:{_sc_col}"></div></div>
  <div style="color:#6b8aaa;font-size:11px;margin-top:8px">Generada por Railway · {_ts} UTC</div>
</div>""", unsafe_allow_html=True)

        # ── Ejecución manual ──────────────────────────────
        if _mt5_conn and _dirbr:
            tick = mt5_tick()
            _ep  = tick["ask"] if _dirbr == "LONG" else tick["bid"] if tick else price
            _atr_v = float(atr) * PIP if atr else 0.001
            _sl  = round(_ep - _atr_v * 1.5, 5) if _dirbr == "LONG" else round(_ep + _atr_v * 1.5, 5)
            _tp  = round(_ep + _atr_v * 2.0, 5) if _dirbr == "LONG" else round(_ep - _atr_v * 2.0, 5)

            with st.expander("⚡ Ejecutar señal en MT5", expanded=(score >= MIN_SCORE)):
                c1, c2, c3 = st.columns(3)
                _vol = c1.number_input("Volumen (lotes)", 0.01, 10.0, 0.01, 0.01, key="_vol")
                _sl2 = c2.number_input("Stop Loss", value=_sl, format="%.5f", key="_sl2")
                _tp2 = c3.number_input("Take Profit", value=_tp, format="%.5f", key="_tp2")
                if st.button(f"{'🟢' if _dirbr=='LONG' else '🔴'} Ejecutar {_dirbr} {_vol} lotes",
                             type="primary", key="_exec_btn"):
                    with st.spinner("Enviando orden a MT5..."):
                        res = mt5_place_order(_dirbr, _vol, _ep, _sl2, _tp2)
                    if res.get("success"):
                        st.success(f"✅ Orden ejecutada — Ticket #{res['ticket']} @ {res['price']:.5f}")
                        if _DB_OK:
                            try:
                                _db.save_trade(
                                    user_id=user, direction=_dirbr,
                                    entry_price=res["price"], volume=_vol,
                                    sl=_sl2, tp=_tp2, score=score,
                                    strategy="mt5_local",
                                    market_snapshot=extra,
                                )
                            except Exception:
                                pass
                    else:
                        st.error(f"❌ Error: {res.get('error')}")
    else:
        st.markdown("""<div class="smc-card sig-n" style="text-align:center;padding:30px">
  <div style="color:#6b8aaa;font-size:14px">Esperando señal de Railway...</div>
  <div style="color:#3d4a5a;font-size:12px;margin-top:6px">
    El servidor analiza cada 5 minutos. Asegúrate de que Railway esté activo.
  </div>
</div>""", unsafe_allow_html=True)

    # ── Posiciones abiertas en MT5 ────────────────────────
    st.markdown("#### 📋 Posiciones abiertas en MT5")
    if _mt5_conn:
        positions = mt5_positions()
        if positions:
            for pos in positions:
                _pcls   = "pos-long" if pos["type"] == "LONG" else "pos-short"
                _pprof  = pos["profit"]
                _profcls = "pos-profit-pos" if _pprof >= 0 else "pos-profit-neg"
                _psign  = "+" if _pprof >= 0 else ""
                st.markdown(f"""<div class="pos-card pos-open {_pcls}">
  <div>
    <span class="bdg {'bdg-g' if pos['type']=='LONG' else 'bdg-r'}">{pos['type']}</span>
    <span style="color:#6b8aaa;font-size:12px;margin-left:8px">#{pos['ticket']}</span><br>
    <span style="font-size:13px;color:#8899aa">
      Entrada: <b style="color:#e0e8f0">{pos['open']:.5f}</b> &nbsp;
      Actual: <b style="color:#e0e8f0">{pos['current']:.5f}</b> &nbsp;
      Vol: {pos['volume']}
    </span>
  </div>
  <div style="text-align:right">
    <div class="{_profcls}" style="font-size:18px">{_psign}{_pprof:.2f}</div>
    <div style="color:#6b8aaa;font-size:11px">
      SL: {pos['sl']:.5f} &nbsp; TP: {pos['tp']:.5f}
    </div>
    <div style="margin-top:6px"></div>
  </div>
</div>""", unsafe_allow_html=True)
                if st.button(f"❌ Cerrar #{pos['ticket']}", key=f"_close_{pos['ticket']}"):
                    res = mt5_close(pos["ticket"])
                    if res.get("success"):
                        st.success(f"✅ Posición #{pos['ticket']} cerrada @ {res['price']:.5f}")
                        st.rerun()
                    else:
                        st.error(f"Error al cerrar: {res.get('error')}")
        else:
            st.markdown("""<div class="smc-card" style="color:#6b8aaa;font-size:13px;text-align:center">
  ○ Sin posiciones abiertas en EURUSD
</div>""", unsafe_allow_html=True)

        # Tick en vivo
        tick = mt5_tick()
        if tick:
            c1, c2, c3 = st.columns(3)
            c1.metric("BID", f"{tick['bid']:.5f}")
            c2.metric("ASK", f"{tick['ask']:.5f}")
            c3.metric("Spread", f"{tick['spread']} pips")
    else:
        st.markdown("""<div class="smc-card sig-n" style="color:#6b8aaa;font-size:13px;text-align:center;padding:20px">
  Conecta MT5 en el panel derecho para ver posiciones y precios en vivo
</div>""", unsafe_allow_html=True)

    # ── Countdown refresh ─────────────────────────────────
    _secs_left = max(0, REFRESH_SECS - int(time.time() - st.session_state.last_rf))
    st.caption(f"🔄 Próxima sincronización con Railway en {_secs_left // 60}:{_secs_left % 60:02d}")

# ═══════════════════════════════════════════════════════
# COLUMNA DERECHA — MT5 + Cuenta + Auto-trade
# ═══════════════════════════════════════════════════════
with col_right:

    # ── Conexión MT5 ──────────────────────────────────────
    st.markdown("#### 🖥️ MetaTrader 5")

    if not _mt5_avail:
        st.error("MetaTrader5 no está instalado. Instálalo desde metatrader5.com e instala el paquete Python: `pip install MetaTrader5`")
    else:
        if not _mt5_conn:
            with st.form("_mt5f"):
                st.markdown("**Credenciales MT5:**")
                _l = st.text_input("Login (número de cuenta)",
                                   value=st.session_state.mt5_login, placeholder="12345678")
                _p = st.text_input("Password", type="password",
                                   value=st.session_state.mt5_pass)
                _s = st.text_input("Server (opcional)",
                                   value=st.session_state.mt5_srv,
                                   placeholder="ICMarkets-Demo")
                _btn = st.form_submit_button("🔗 Conectar MT5", use_container_width=True)
            if _btn:
                with st.spinner("Conectando..."):
                    ok, msg = mt5_connect(login=_l or None, password=_p or None, server=_s or None)
                if ok:
                    st.session_state.mt5_login = _l
                    st.session_state.mt5_pass  = _p
                    st.session_state.mt5_srv   = _s
                    if _DB_OK:
                        try:
                            _db.save_user_mt5(user, _l, _p, _s)
                        except Exception:
                            pass
                    st.success(msg)
                    st.rerun()
                else:
                    st.error(f"❌ {msg}")
        else:
            acct = mt5_account()
            if acct:
                st.markdown(f"""<div class="smc-card">
  <div style="color:#6b8aaa;font-size:12px;margin-bottom:8px">
    🏦 {acct['name']} · {acct['server']}
  </div>""", unsafe_allow_html=True)
                c1, c2 = st.columns(2)
                c1.metric("Balance", f"{acct['balance']:.2f} {acct['currency']}")
                c2.metric("Equity", f"{acct['equity']:.2f} {acct['currency']}")
                c1.metric("P&L abierto", f"{acct['profit']:+.2f}")
                c2.metric("Margen libre", f"{acct['free']:.2f}")
                st.markdown("</div>", unsafe_allow_html=True)
            if st.button("🔌 Desconectar MT5"):
                mt5mod = _get_mt5()
                if mt5mod:
                    try:
                        mt5mod.shutdown()
                    except Exception:
                        pass
                globals()["_mt5_conn"] = False
                st.rerun()

    st.markdown("---")

    # ── Auto-trading ──────────────────────────────────────
    st.markdown("#### 🤖 Auto-trading")

    if not _mt5_conn:
        st.markdown("""<div class="smc-card" style="color:#6b8aaa;font-size:13px">
  Conecta MT5 para activar auto-trading
</div>""", unsafe_allow_html=True)
    else:
        _can, _can_msg = mt5_can_trade()
        if not _can:
            st.warning(f"⚠️ {_can_msg}\nActiva **AutoTrading** en MT5 (botón verde en la barra superior del terminal).")

        st.session_state.autotr = st.toggle(
            "Ejecutar señales automáticamente",
            value=st.session_state.autotr,
            disabled=not _can,
        )

        if st.session_state.autotr and _can and snap and snap.get("price"):
            score = int(snap.get("score", 0))
            _isbuy  = "COMPRA" in str(snap.get("signal","")).upper()
            _issell = "VENTA"  in str(snap.get("signal","")).upper()
            _dirbr  = "LONG" if _isbuy else ("SHORT" if _issell else None)
            _snap_id = str(snap.get("created_at",""))

            if _dirbr and score >= MIN_SCORE and _snap_id != st.session_state.last_exec:
                tick  = mt5_tick()
                _ep   = tick["ask"] if _dirbr == "LONG" else tick["bid"] if tick else float(snap["price"])
                extra = snap.get("snapshot_data") or {}
                if isinstance(extra, str):
                    import json as _json
                    try: extra = _json.loads(extra)
                    except Exception: extra = {}
                _atr_v = float(extra.get("atr_1h_pips", 10)) * PIP
                _sl = round(_ep - _atr_v * 1.5, 5) if _dirbr == "LONG" else round(_ep + _atr_v * 1.5, 5)
                _tp = round(_ep + _atr_v * 2.0, 5) if _dirbr == "LONG" else round(_ep - _atr_v * 2.0, 5)

                st.markdown(f"""<div class="smc-card sig-{'b' if _dirbr=='LONG' else 's'}">
  <b>🤖 Auto-trade detectado</b><br>
  <span style="color:#6b8aaa;font-size:12px">
    {_dirbr} · Score {score} · EP {_ep:.5f} · SL {_sl:.5f} · TP {_tp:.5f}
  </span>
</div>""", unsafe_allow_html=True)

                if st.button("✅ Ejecutar ahora (auto)", type="primary", key="_auto_exec"):
                    res = mt5_place_order(_dirbr, 0.01, _ep, _sl, _tp)
                    if res.get("success"):
                        st.session_state.last_exec = _snap_id
                        st.success(f"✅ Ejecutado #{res['ticket']}")
                        if _DB_OK:
                            try:
                                _db.save_trade(
                                    user_id=user, direction=_dirbr,
                                    entry_price=res["price"], volume=0.01,
                                    sl=_sl, tp=_tp, score=score,
                                    strategy="auto_mt5",
                                    market_snapshot=extra,
                                )
                            except Exception:
                                pass
                        st.rerun()
                    else:
                        st.error(f"❌ {res.get('error')}")
            elif score < MIN_SCORE and _dirbr:
                st.info(f"Score {score}/100 — mínimo {MIN_SCORE} para auto-ejecutar")
            elif not _dirbr:
                st.info("Señal NEUTRAL — sin acción")

        st.caption(f"Mínimo score para auto-trade: {MIN_SCORE}/100")

    st.markdown("---")

    # ── Logout ────────────────────────────────────────────
    if st.button("🚪 Cerrar sesión", key="_logout"):
        st.session_state.user = None
        st.session_state.mt5_creds_loaded = False
        if _DB_OK and "session_token" in st.session_state:
            try:
                _db.invalidate_session(st.session_state.session_token)
            except Exception:
                pass
        st.rerun()
