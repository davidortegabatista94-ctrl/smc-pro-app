"""
investment_module.py — Módulo de Inversión a Largo Plazo (1-5 años)

Análisis fundamental + técnico + macroeconómico para construir
una cartera diversificada con alta probabilidad de rentabilidad positiva.

Sin señales de trading. Sin ruido. Solo inversión de calidad.
"""

import streamlit as st
import pandas as pd
import numpy as np
import logging
import time
from datetime import datetime, timedelta

_log = logging.getLogger("smc.invest")

# ─────────────────────────────────────────────────────────────────────────────
# Universo de activos curados
# ─────────────────────────────────────────────────────────────────────────────

UNIVERSE = {
    "🇺🇸 Índices USA": {
        "SPY":  {"name": "S&P 500 (SPDR)",        "type": "etf",   "er": 0.09, "cat": "equity_usa"},
        "QQQ":  {"name": "Nasdaq 100",             "type": "etf",   "er": 0.20, "cat": "equity_growth"},
        "VTI":  {"name": "Total Market USA",       "type": "etf",   "er": 0.03, "cat": "equity_usa"},
        "SCHD": {"name": "Dividendo Quality USA",  "type": "etf",   "er": 0.06, "cat": "dividend"},
        "IWM":  {"name": "Russell 2000 (Small Cap)","type": "etf",  "er": 0.19, "cat": "equity_small"},
    },
    "🌍 Mercados Globales": {
        "VT":   {"name": "Global Total Market",    "type": "etf",   "er": 0.07, "cat": "equity_global"},
        "VEA":  {"name": "Europa + Asia Desarrollados","type": "etf","er": 0.05,"cat": "equity_intl"},
        "VWO":  {"name": "Mercados Emergentes",    "type": "etf",   "er": 0.08, "cat": "equity_em"},
        "EWJ":  {"name": "Japón ETF (iShares)",    "type": "etf",   "er": 0.50, "cat": "equity_intl"},
    },
    "⚙️ Sectores USA": {
        "XLK":  {"name": "Tecnología",             "type": "etf",   "er": 0.10, "cat": "sector_tech"},
        "XLV":  {"name": "Salud",                  "type": "etf",   "er": 0.10, "cat": "sector_health"},
        "XLF":  {"name": "Financiero",             "type": "etf",   "er": 0.10, "cat": "sector_finance"},
        "XLE":  {"name": "Energía",                "type": "etf",   "er": 0.10, "cat": "sector_energy"},
        "XLI":  {"name": "Industrial",             "type": "etf",   "er": 0.10, "cat": "sector_industry"},
        "XLRE": {"name": "Real Estate (REIT)",     "type": "etf",   "er": 0.10, "cat": "real_estate"},
    },
    "🏦 Renta Fija": {
        "BND":  {"name": "Bonos Total USA (Vanguard)","type": "etf","er": 0.03, "cat": "bond_total"},
        "TLT":  {"name": "Bonos 20+ años (iShares)","type": "etf",  "er": 0.15, "cat": "bond_long"},
        "VTIP": {"name": "TIPS (anti-inflación)",  "type": "etf",   "er": 0.04, "cat": "bond_tips"},
        "HYG":  {"name": "High Yield Corporativo", "type": "etf",   "er": 0.48, "cat": "bond_hy"},
        "VCSH": {"name": "Bonos Corto Plazo Corp.", "type": "etf",  "er": 0.04, "cat": "bond_short"},
    },
    "🥇 Activos Reales": {
        "IAU":  {"name": "Oro (iShares, bajo coste)","type": "etf", "er": 0.25, "cat": "gold"},
        "GLD":  {"name": "Oro (SPDR)",             "type": "etf",   "er": 0.40, "cat": "gold"},
        "PDBC": {"name": "Commodities Diversif.",  "type": "etf",   "er": 0.59, "cat": "commodity"},
        "VNQ":  {"name": "REIT USA (Vanguard)",    "type": "etf",   "er": 0.12, "cat": "real_estate"},
    },
    "💎 Acciones de Calidad": {
        "AAPL":  {"name": "Apple",              "type": "stock", "er": 0, "cat": "tech"},
        "MSFT":  {"name": "Microsoft",          "type": "stock", "er": 0, "cat": "tech"},
        "GOOGL": {"name": "Alphabet (Google)",  "type": "stock", "er": 0, "cat": "tech"},
        "AMZN":  {"name": "Amazon",             "type": "stock", "er": 0, "cat": "tech"},
        "NVDA":  {"name": "NVIDIA",             "type": "stock", "er": 0, "cat": "tech"},
        "BRK-B": {"name": "Berkshire Hathaway", "type": "stock", "er": 0, "cat": "diversified"},
        "JPM":   {"name": "JPMorgan Chase",     "type": "stock", "er": 0, "cat": "finance"},
        "JNJ":   {"name": "Johnson & Johnson",  "type": "stock", "er": 0, "cat": "health"},
        "V":     {"name": "Visa",               "type": "stock", "er": 0, "cat": "finance"},
        "UNH":   {"name": "UnitedHealth",       "type": "stock", "er": 0, "cat": "health"},
    },
}

# Perfiles de riesgo con allocaciones target
RISK_PROFILES = {
    "conservador": {
        "label":      "🛡️ Conservador (1-2 años)",
        "bond_pct":   45,
        "equity_pct": 35,
        "gold_pct":   10,
        "cash_pct":   10,
        "stocks_pct": 0,
        "desc":    "Preservar capital. Crecimiento moderado. Baja volatilidad.",
        "dd_max":  "−10%",
        "return_annual": "4–7%",
        "horizon": "1-2 años",
    },
    "moderado": {
        "label":      "⚖️ Moderado (2-4 años)",
        "bond_pct":   20,
        "equity_pct": 55,
        "gold_pct":   10,
        "cash_pct":   5,
        "stocks_pct": 10,
        "desc":    "Balance crecimiento/estabilidad. Volatilidad aceptable.",
        "dd_max":  "−20%",
        "return_annual": "7–11%",
        "horizon": "2-4 años",
    },
    "agresivo": {
        "label":      "🚀 Agresivo (4-5 años)",
        "bond_pct":   5,
        "equity_pct": 60,
        "gold_pct":   5,
        "cash_pct":   0,
        "stocks_pct": 30,
        "desc":    "Maximizar crecimiento. Alta volatilidad temporal aceptada.",
        "dd_max":  "−35%",
        "return_annual": "10–15%",
        "horizon": "4-5 años",
    },
}

# ─────────────────────────────────────────────────────────────────────────────
# Datos y scoring
# ─────────────────────────────────────────────────────────────────────────────

@st.cache_data(ttl=86400, show_spinner=False)
def _fetch_asset(ticker: str) -> dict:
    """Descarga precio histórico (2 años) + fundamentales de yfinance."""
    try:
        import yfinance as yf
        t = yf.Ticker(ticker)

        hist = t.history(period="2y", auto_adjust=True)
        if hist is None or hist.empty or len(hist) < 50:
            return {}

        info = {}
        try:
            info = t.info or {}
        except Exception:
            pass

        c = hist["Close"].dropna()
        now_p = float(c.iloc[-1])

        # Retornos en distintos horizontes
        def _ret(days):
            idx = max(0, len(c) - days)
            past = float(c.iloc[idx])
            return (now_p - past) / past * 100 if past > 0 else 0.0

        ret_1m  = _ret(21)
        ret_3m  = _ret(63)
        ret_6m  = _ret(126)
        ret_1y  = _ret(252)

        # EMA 200 (semanal = ~200 días)
        ema200 = float(c.ewm(span=200, adjust=False).mean().iloc[-1])

        # RSI semanal (aprox con 14 semanas = 70 días)
        wc   = c.resample("W").last().dropna()
        _d   = wc.diff()
        _g   = _d.clip(lower=0).rolling(14).mean()
        _l   = (-_d.clip(upper=0)).rolling(14).mean()
        rsi_w = float(100 - 100 / (1 + _g.iloc[-1] / (_l.iloc[-1] + 1e-9))) if len(wc) >= 15 else 50.0

        # Máx drawdown 2 años
        roll_max = c.cummax()
        dd = ((c - roll_max) / roll_max * 100).min()

        return {
            "price":    round(now_p, 2),
            "ret_1m":   round(ret_1m, 1),
            "ret_3m":   round(ret_3m, 1),
            "ret_6m":   round(ret_6m, 1),
            "ret_1y":   round(ret_1y, 1),
            "ema200":   round(ema200, 2),
            "above_ema200": now_p > ema200,
            "rsi_w":    round(rsi_w, 1),
            "max_dd_2y": round(float(dd), 1),
            "hist":     c,
            # Fundamentales (solo stocks; ETFs devuelven None)
            "pe_fwd":        info.get("forwardPE"),
            "pe_trail":      info.get("trailingPE"),
            "pb":            info.get("priceToBook"),
            "rev_growth":    info.get("revenueGrowth"),
            "earn_growth":   info.get("earningsGrowth"),
            "profit_margin": info.get("profitMargins"),
            "roe":           info.get("returnOnEquity"),
            "debt_eq":       info.get("debtToEquity"),
            "div_yield":     info.get("dividendYield"),
            "market_cap":    info.get("marketCap"),
            "beta":          info.get("beta"),
            "name_long":     info.get("longName", ticker),
        }
    except Exception as e:
        _log.debug(f"fetch_asset {ticker}: {e}")
        return {}


def _score_asset(ticker: str, meta: dict, data: dict) -> dict:
    """
    Puntúa un activo de 0 a 100 en tres dimensiones:
      - Técnico (0-40):   momentum multi-horizonte + EMA200 + RSI semanal
      - Fundamental (0-40): calidad del negocio / índice
      - Macro (0-20):     entorno macroeconómico actual
    """
    if not data:
        return {"total": 0, "tech": 0, "fund": 0, "macro": 0, "flags": [], "verdict": "Sin datos"}

    flags    = []
    tech     = 0
    fund     = 0
    macro    = 0
    warnings = []

    # ── TÉCNICO ──────────────────────────────────────────────────────────────

    # Momentum multi-horizonte (el más importante para LP)
    r1m = data.get("ret_1m", 0)
    r3m = data.get("ret_3m", 0)
    r6m = data.get("ret_6m", 0)
    r1y = data.get("ret_1y", 0)

    if r1y > 20:
        tech += 12; flags.append("📈 Retorno 1 año >20%")
    elif r1y > 10:
        tech += 8;  flags.append("📈 Retorno 1 año >10%")
    elif r1y > 0:
        tech += 4
    elif r1y < -20:
        tech -= 5;  warnings.append("⚠️ Caída >20% en 1 año")

    if r6m > 10:
        tech += 8; flags.append("📈 Momentum 6M fuerte")
    elif r6m > 0:
        tech += 4
    elif r6m < -10:
        warnings.append("⚠️ Momentum 6M negativo")

    if r3m > 5:
        tech += 6; flags.append("✅ Momentum 3M positivo")
    elif r3m > 0:
        tech += 3
    elif r3m < -8:
        tech -= 3

    if r1m > 2:
        tech += 4
    elif r1m < -5:
        tech -= 2

    # EMA200 — tendencia de largo plazo
    if data.get("above_ema200"):
        tech += 8; flags.append("✅ Sobre EMA200 — tendencia alcista LP")
    else:
        tech -= 4; warnings.append("⚠️ Bajo EMA200 — tendencia LP bajista")

    # RSI semanal
    rsi_w = data.get("rsi_w", 50)
    if 40 <= rsi_w <= 70:
        tech += 4; flags.append(f"✅ RSI semanal {rsi_w:.0f} — zona limpia")
    elif rsi_w > 80:
        tech -= 5; warnings.append(f"⚠️ RSI semanal {rsi_w:.0f} — sobrecomprado")
    elif rsi_w < 30:
        tech += 2; flags.append(f"📊 RSI semanal {rsi_w:.0f} — zona de posible rebote")

    # Drawdown máximo
    max_dd = data.get("max_dd_2y", 0)
    if max_dd > -15:
        tech += 2; flags.append("✅ Drawdown 2Y controlado")
    elif max_dd < -40:
        warnings.append(f"⚠️ Drawdown máximo 2Y: {max_dd:.0f}%")

    tech = max(0, min(40, tech))

    # ── FUNDAMENTAL ──────────────────────────────────────────────────────────

    if meta["type"] == "stock":
        # P/E ratio
        pe = data.get("pe_fwd") or data.get("pe_trail")
        if pe is not None and pe > 0:
            if pe < 18:
                fund += 12; flags.append(f"💰 P/E {pe:.1f} — valoración atractiva")
            elif pe < 28:
                fund += 8;  flags.append(f"💰 P/E {pe:.1f} — valoración razonable")
            elif pe < 40:
                fund += 4
            else:
                fund -= 4;  warnings.append(f"⚠️ P/E {pe:.1f} — algo caro")

        # Crecimiento de ingresos
        rev_g = data.get("rev_growth")
        if rev_g is not None:
            rv_pct = rev_g * 100
            if rv_pct > 15:
                fund += 10; flags.append(f"🚀 Crecimiento ingresos {rv_pct:.0f}%")
            elif rv_pct > 5:
                fund += 6;  flags.append(f"✅ Crecimiento ingresos {rv_pct:.0f}%")
            elif rv_pct < 0:
                warnings.append(f"⚠️ Ingresos cayendo {rv_pct:.0f}%")

        # Margen de beneficio
        pm = data.get("profit_margin")
        if pm is not None:
            pm_pct = pm * 100
            if pm_pct > 20:
                fund += 8; flags.append(f"💎 Margen neto {pm_pct:.0f}% — empresa muy rentable")
            elif pm_pct > 10:
                fund += 5; flags.append(f"✅ Margen neto {pm_pct:.0f}%")
            elif pm_pct < 0:
                fund -= 8; warnings.append("❌ Empresa con pérdidas")

        # ROE
        roe = data.get("roe")
        if roe is not None and roe > 0.15:
            fund += 5; flags.append(f"✅ ROE {roe*100:.0f}% — alta rentabilidad del capital")

        # Deuda
        de = data.get("debt_eq")
        if de is not None:
            if de < 50:
                fund += 5; flags.append("✅ Deuda/Capital baja — balance sólido")
            elif de > 200:
                fund -= 5; warnings.append(f"⚠️ Deuda/Capital alta ({de:.0f}%)")

    else:  # ETF
        # Expense ratio (cuanto menos mejor)
        er = meta.get("er", 0.5)
        if er < 0.10:
            fund += 15; flags.append(f"💰 Expense ratio {er:.2f}% — muy bajo coste")
        elif er < 0.25:
            fund += 10; flags.append(f"✅ Expense ratio {er:.2f}% — coste razonable")
        elif er < 0.50:
            fund += 5
        else:
            warnings.append(f"⚠️ Expense ratio {er:.2f}% — coste elevado")

        # Diversificación bonus
        cat = meta.get("cat", "")
        if cat in ("equity_usa", "equity_global"):
            fund += 15; flags.append("🌐 Altamente diversificado — riesgo distribuido")
        elif cat in ("equity_intl", "equity_em"):
            fund += 10
        elif cat in ("bond_total", "bond_tips"):
            fund += 12; flags.append("🛡️ Renta fija de alta calidad")
        elif cat in ("dividend",):
            fund += 10; flags.append("💵 ETF de dividendo — flujo de caja regular")
        elif cat in ("sector_tech", "sector_health"):
            fund += 8

        # Dividendo bonus si lo tiene
        dy = data.get("div_yield")
        if dy and dy > 0.02:
            fund += 5; flags.append(f"💵 Dividendo {dy*100:.1f}% anual")

    fund = max(0, min(40, fund))

    # ── MACRO (0-20) ─────────────────────────────────────────────────────────
    # Se calculará globalmente y se aplicará por categoría
    # Por ahora lo dejamos en 10 (neutro); _apply_macro_scores lo ajustará
    macro = 10

    total = tech + fund + macro
    total = max(0, min(100, total))

    # Veredicto
    if total >= 75:
        verdict = "🟢 COMPRAR"
    elif total >= 58:
        verdict = "🟡 MANTENER / ACUMULAR"
    elif total >= 40:
        verdict = "🟠 ESPERAR"
    else:
        verdict = "🔴 EVITAR"

    return {
        "total": total, "tech": tech, "fund": fund, "macro": macro,
        "flags": flags, "warnings": warnings, "verdict": verdict,
    }


# ─────────────────────────────────────────────────────────────────────────────
# PYMEs con potencial explosivo — universo curado de 20 small/mid caps
# ─────────────────────────────────────────────────────────────────────────────

PYME_UNIVERSE = {
    "IONQ": {
        "name":   "IonQ — Computación Cuántica",
        "sector": "Tecnología / IA",
        "risk":   5,
        "thesis": (
            "Líder en computación cuántica atrapada en iones. "
            "Contratos con AWS, Azure, Google Cloud. La computación cuántica "
            "podría romper el cifrado actual y acelerar la IA 100x. "
            "Cuando llegue la ventaja cuántica ('quantum advantage'), IonQ "
            "estará ahí."
        ),
        "catalysts": ["Contratos gobierno USA", "Quantum advantage milestone", "Acuerdos big cloud"],
        "risks":     ["Tecnología inmadura", "Pérdidas operativas", "Competencia IBM/Google"],
    },
    "RKLB": {
        "name":   "Rocket Lab — Nueva Era Espacial",
        "sector": "Espacio / Defensa",
        "risk":   4,
        "thesis": (
            "El único rival serio de SpaceX en pequeños satélites. "
            "Lanzamientos Electron probados (50+), fabricación de satélites Photon, "
            "y en desarrollo el cohete Neutron para cargas medianas. "
            "Contratos NASA, DARPA y Defensa USA. La economía espacial llegará a "
            "$1 billón para 2040."
        ),
        "catalysts": ["Primer vuelo Neutron", "Contratos DOD", "Acuerdos satélites comerciales"],
        "risks":     ["Fallos de lanzamiento", "SpaceX dominancia", "Financiación Neutron"],
    },
    "ASTS": {
        "name":   "AST SpaceMobile — 5G desde el Espacio",
        "sector": "Telecom / Espacio",
        "risk":   5,
        "thesis": (
            "Construye una red de banda ancha directamente desde satélites "
            "a móviles normales, sin hardware especial. Acuerdos con AT&T, "
            "Verizon, Vodafone, Rakuten. Si funciona, conecta al 90% del mundo "
            "sin cobertura. Un monopolio espacial de telecomunicaciones."
        ),
        "catalysts": ["Comercialización servicio", "Nuevos operadores", "Despliegue constelación BlueBird"],
        "risks":     ["Ejecución técnica compleja", "Interferencias satelitales", "Capital intensivo"],
    },
    "BBAI": {
        "name":   "BigBear.ai — IA para Defensa",
        "sector": "IA / Defensa",
        "risk":   4,
        "thesis": (
            "Plataforma de IA para inteligencia militar, supply chain y "
            "seguridad nacional. Contratos US Army, Air Force y agencias civiles. "
            "El gasto global en IA de defensa crece al 14% anual. "
            "Empresa pequeña en un mercado de cientos de miles de millones."
        ),
        "catalysts": ["Contratos militares grandes", "Expansión internacional", "M&A objetivo"],
        "risks":     ["Dependencia presupuesto gobierno", "Competencia Palantir", "Márgenes bajos"],
    },
    "SOUN": {
        "name":   "SoundHound AI — Voz IA",
        "sector": "IA / Automoción",
        "risk":   4,
        "thesis": (
            "IA de voz independiente del teléfono, integrada en vehículos "
            "(Stellantis, Honda), restaurantes y bancos. NVIDIA tiene participación. "
            "El mercado de asistentes de voz IA crece al 28% anual. "
            "Primer mover en voz sin internet en coches."
        ),
        "catalysts": ["Expansión autos nuevos modelos", "Plataforma restaurantes escala", "Partnership NVIDIA"],
        "risks":     ["Amazon/Apple/Google competencia", "Pérdidas operativas", "Concentración clientes"],
    },
    "RXRX": {
        "name":   "Recursion Pharma — IA + Descubrimiento Fármacos",
        "sector": "Biotech / IA",
        "risk":   4,
        "thesis": (
            "Usa IA y biología celular a escala para descubrir fármacos 10x más "
            "rápido y barato. Partnership con NVIDIA ($50M) para el 'sistema operativo "
            "de biología'. Pipeline de 40+ candidatos. Si la IA transforma la "
            "farmacéutica, Recursion está en el centro."
        ),
        "catalysts": ["Datos clínicos positivos", "Partnership big pharma", "Plataforma licencias"],
        "risks":     ["Alta tasa de fracaso clínico", "Quema de caja", "Competencia IA bio"],
    },
    "BEAM": {
        "name":   "Beam Therapeutics — Gene Editing",
        "sector": "Biotech",
        "risk":   5,
        "thesis": (
            "Edición genética de base ('base editing'): corrige letras del ADN "
            "sin cortar la doble hélice. Más precisa que CRISPR tradicional. "
            "Pipeline contra anemia falciforme, leucemia aguda, enfermedades cardiacas. "
            "Potencial de cura permanente de enfermedades genéticas."
        ),
        "catalysts": ["Datos clínicos Fase 1/2", "Aprobación FDA primera indicación", "Partnerships pharma"],
        "risks":     ["Fracaso clínico", "Años hasta revenue", "Caja limitada"],
    },
    "LUNR": {
        "name":   "Intuitive Machines — Economía Lunar",
        "sector": "Espacio / NASA",
        "risk":   5,
        "thesis": (
            "Únicos que han aterrizado con éxito en la Luna en 50 años (2024). "
            "Contratos exclusivos NASA Artemis para infraestructura lunar: "
            "comunicaciones, navegación, transporte de carga. La economía lunar "
            "empieza ahora y ellos llevan ventaja de 10 años."
        ),
        "catalysts": ["Misiones Luna 2025-26", "Contratos NASA Lunar Comms Network", "Misiones privadas"],
        "risks":     ["Alta probabilidad de fallo misión", "Dependencia NASA", "Capital intensivo"],
    },
    "KTOS": {
        "name":   "Kratos Defense — Drones & Hipersónica",
        "sector": "Defensa",
        "risk":   3,
        "thesis": (
            "Fabricante de drones militares de bajo coste, misiles y sistemas "
            "hipersónicos. El conflicto en Ucrania demostró que los drones "
            "cambian la guerra. Backlog récord. Contratos con USAF, Marina USA. "
            "Crecimiento estable con upside de contratos grandes."
        ),
        "catalysts": ["Contratos Valkyrie drone", "Programa hipersónico", "Aumento presupuesto Defensa USA"],
        "risks":     ["Ciclo presupuestario congreso", "Competencia Northrop/Lockheed", "Márgenes ajustados"],
    },
    "HIMS": {
        "name":   "Hims & Hers — Telesalud + GLP-1",
        "sector": "Salud / Telehealth",
        "risk":   3,
        "thesis": (
            "Plataforma de salud online (telehealth + farmacia). Gran apuesta: "
            "vende compuestos de semaglutida (Ozempic genérico) directamente. "
            "El mercado de GLP-1 (obesidad/diabetes) puede superar $100B en 5 años. "
            "Ya rentable. Crecimiento de revenue >60% anual."
        ),
        "catalysts": ["Escala GLP-1 compuestos", "Expansión Europa", "Nuevas categorías salud"],
        "risks":     ["FDA regulación compuestos", "Ozempic disponibilidad mejora", "Competencia"],
    },
    "FLNC": {
        "name":   "Fluence Energy — Almacenamiento Grid",
        "sector": "Energía Limpia",
        "risk":   3,
        "thesis": (
            "Líder en sistemas de almacenamiento de energía a escala industrial "
            "(baterías para redes eléctricas). JV entre Siemens y AES. "
            "La transición renovable es imposible sin almacenamiento. "
            "Backlog $4B+. Contrato tras contrato de utilities y gobiernos."
        ),
        "catalysts": ["Contratos utilities escala", "Política IRA USA", "Expansión internacional"],
        "risks":     ["Precios baterías", "Cadena suministro", "Márgenes presionados"],
    },
    "ARRY": {
        "name":   "Array Technologies — Solar Tracker",
        "sector": "Energía Solar",
        "risk":   3,
        "thesis": (
            "Fabricante de sistemas de seguimiento solar (trackers): maximizan "
            "la energía generada un 25% vs paneles fijos. Líder mundial en "
            "este nicho crítico. El boom solar a escala utility requiere trackers. "
            "Beneficia directamente del IRA americano y políticas solares globales."
        ),
        "catalysts": ["Expansión internacional", "Nuevos contratos utility-scale", "IRA créditos fiscales"],
        "risks":     ["Aranceles paneles solares", "Dependencia USA", "Competencia precios"],
    },
    "JOBY": {
        "name":   "Joby Aviation — Taxi Aéreo Eléctrico",
        "sector": "Movilidad / eVTOL",
        "risk":   5,
        "thesis": (
            "Desarrolla un taxi aéreo eléctrico silencioso (eVTOL). "
            "Avanzado en certificación FAA (más cerca que ninguno). "
            "Toyota ha invertido $894M. Acuerdo con Delta Air Lines. "
            "Si el taxi aéreo se hace realidad, Joby lo lidera."
        ),
        "catalysts": ["Certificación FAA", "Primer servicio comercial", "Expansión ciudades"],
        "risks":     ["Proceso certificación muy largo", "Caja pesada", "Aceptación pública"],
    },
    "TMDX": {
        "name":   "TransMedics — Transporte Órganos",
        "sector": "MedTech",
        "risk":   3,
        "thesis": (
            "Sistema revolucionario que mantiene órganos vivos durante el "
            "transporte (OCS), triplicando la distancia de trasplante viable. "
            "Crecimiento de trasplantes +80% donde se usa. "
            "Monopolio de facto en un nicho que literalmente salva vidas. "
            "Ingresos creciendo >100% anual."
        ),
        "catalysts": ["Expansión corazón/pulmón", "Nuevos centros trasplante", "Internacional"],
        "risks":     ["Reembolso aseguradoras", "Competencia", "Crecimiento dependiente adopción"],
    },
    "RXST": {
        "name":   "RxSight — Lentes Inteligentes",
        "sector": "MedTech / Oftalmología",
        "risk":   3,
        "thesis": (
            "Única lente intraocular ajustable después de la cirugía de cataratas "
            "(LDD: Light Delivery Device). Pacientes ven mejor que con cualquier "
            "lente premium actual. Crecimiento de implantes >80% anual. "
            "25M cirugías de cataratas al año en el mundo — mercado enorme y sin explotar."
        ),
        "catalysts": ["Expansión internacional", "Nuevos centros adopción", "Datos comparativos clínicos"],
        "risks":     ["Precio premium barrera", "Reembolso seguro limitado", "Competencia Alcon/J&J"],
    },
    "ACMR": {
        "name":   "ACM Research — Equipos Semiconductores",
        "sector": "Semiconductores / Equipo",
        "risk":   3,
        "thesis": (
            "Equipos de limpieza de obleas para fabricación de chips. "
            "China invierte masivamente en semiconductor soberano — ACMR "
            "es uno de sus proveedores clave. Backlog disparado. "
            "El mundo necesita más fábricas de chips independientemente del ciclo."
        ),
        "catalysts": ["Inversión fab China", "CHIPS Act USA nuevas fábricas", "Nuevos clientes"],
        "risks":     ["Restricciones exportación USA-China", "Ciclo semis", "Concentración geográfica"],
    },
    "SPIR": {
        "name":   "Spire Global — Datos Satelitales",
        "sector": "Espacio / Data",
        "risk":   4,
        "thesis": (
            "Constelación de 100+ satélites recopilando datos meteorológicos, "
            "marítimos y de aviación. Contratos con NOAA, ESA, navales y "
            "aseguradoras. La data economy espacial apenas empieza. "
            "El seguro marítimo solo ya es un mercado de $50B que necesita estos datos."
        ),
        "catalysts": ["Contratos gubernamentales", "Expansión datos marítimos", "Clima extremo demanda"],
        "risks":     ["Quema de caja", "Competencia Planet/Maxar", "Precio dato presionado"],
    },
    "DAVE": {
        "name":   "Dave Inc — Neobank para Todos",
        "sector": "Fintech",
        "risk":   3,
        "thesis": (
            "Banco digital para las personas rechazadas o ignoradas por la "
            "banca tradicional (>100M en USA). Sin comisiones de overdraft. "
            "Adelantos de nómina instantáneos. Ya RENTABLE. "
            "Creciendo en un mercado que los grandes bancos no quieren tocar."
        ),
        "catalysts": ["Nuevos productos financieros", "Expansión B2B banking", "Adquisiciones"],
        "risks":     ["Morosidad en recesión", "Competencia Chime/Cash App", "Regulación fintech"],
    },
    "ACHR": {
        "name":   "Archer Aviation — Air Taxi",
        "sector": "Movilidad / eVTOL",
        "risk":   5,
        "thesis": (
            "eVTOL competidor de Joby. Acuerdo con United Airlines (200 aeronaves). "
            "Stellantis fabrica los componentes (reducción coste). "
            "Si el mercado de air taxi llega a $15B en 2030, "
            "Archer tiene una posición fuerte con el backing industrial."
        ),
        "catalysts": ["Certificación FAA", "Primer vuelo comercial", "Pedidos United Airlines"],
        "risks":     ["Certif. FAA incierta", "Caja pesada", "Joby lleva ventaja"],
    },
    "CWAN": {
        "name":   "Clearwater Analytics — SaaS Financiero",
        "sector": "Fintech / SaaS",
        "risk":   2,
        "thesis": (
            "Plataforma SaaS de gestión y reporting de inversiones para "
            "aseguradoras, fondos y bancos. Crecimiento ARR >20% anual. "
            "Retención de clientes >98%. En un mercado regulado donde "
            "cambiar de proveedor es casi imposible — moat defensivo fuerte."
        ),
        "catalysts": ["Expansión Europa/Asia", "Nuevos módulos producto", "Regulación IFRS aumenta demanda"],
        "risks":     ["Valoración premium", "Bloomberg/SS&C competencia", "Ciclo presupuestario clientes"],
    },
}

_RISK_STARS = {1: "⭐ Bajo", 2: "⭐⭐ Moderado", 3: "⭐⭐⭐ Medio-alto",
               4: "⭐⭐⭐⭐ Alto", 5: "⭐⭐⭐⭐⭐ Muy alto"}
_SECTOR_COLORS = {
    "Tecnología / IA": "#805ad5", "IA / Defensa": "#3182ce",
    "IA / Automoción": "#2b6cb0", "Espacio / Defensa": "#2d3748",
    "Espacio / NASA": "#1a365d", "Espacio / Data": "#2c5282",
    "Telecom / Espacio": "#2b6cb0", "Biotech / IA": "#276749",
    "Biotech": "#276749", "Defensa": "#744210", "Salud / Telehealth": "#285e61",
    "Energía Limpia": "#276749", "Energía Solar": "#975a16",
    "Movilidad / eVTOL": "#44337a", "MedTech": "#285e61",
    "MedTech / Oftalmología": "#285e61", "Semiconductores / Equipo": "#744210",
    "Fintech": "#2c5282", "Fintech / SaaS": "#2c5282",
}


@st.cache_data(ttl=86400, show_spinner=False)
def _fetch_pyme(ticker: str) -> dict:
    """Descarga 2 años de precio + fundamentales para una PYME."""
    try:
        import yfinance as yf
        t    = yf.Ticker(ticker)
        hist = t.history(period="2y", auto_adjust=True)
        if hist is None or hist.empty or len(hist) < 30:
            return {}
        info = {}
        try:
            info = t.info or {}
        except Exception:
            pass

        c      = hist["Close"].dropna()
        px     = float(c.iloc[-1])

        def _ret(days):
            idx  = max(0, len(c) - days)
            past = float(c.iloc[idx])
            return (px - past) / past * 100 if past > 0 else 0.0

        ema200 = float(c.ewm(span=200, adjust=False).mean().iloc[-1])
        ema50  = float(c.ewm(span=50,  adjust=False).mean().iloc[-1])

        # RSI semanal
        wc = c.resample("W").last().dropna()
        _d = wc.diff()
        _g = _d.clip(lower=0).rolling(14).mean()
        _l = (-_d.clip(upper=0)).rolling(14).mean()
        rsi_w = float(100 - 100 / (1 + _g.iloc[-1] / (_l.iloc[-1] + 1e-9))) if len(wc) >= 15 else 50.0

        # Drawdown
        roll_max = c.cummax()
        dd_2y    = float(((c - roll_max) / roll_max * 100).min())

        # Distancia desde mínimo/máximo 52 semanas
        hi52 = float(c.tail(252).max())
        lo52 = float(c.tail(252).min())
        pct_from_hi  = (px - hi52) / hi52 * 100
        pct_from_low = (px - lo52) / lo52 * 100

        return {
            "price":        round(px, 2),
            "ret_1m":       round(_ret(21), 1),
            "ret_3m":       round(_ret(63), 1),
            "ret_6m":       round(_ret(126), 1),
            "ret_1y":       round(_ret(252), 1),
            "ema200":       round(ema200, 2),
            "ema50":        round(ema50, 2),
            "above_ema200": px > ema200,
            "above_ema50":  px > ema50,
            "rsi_w":        round(rsi_w, 1),
            "max_dd_2y":    round(dd_2y, 1),
            "hi52":         round(hi52, 2),
            "lo52":         round(lo52, 2),
            "pct_from_hi":  round(pct_from_hi, 1),
            "pct_from_low": round(pct_from_low, 1),
            "hist":         c,
            # Fundamentales
            "market_cap":   info.get("marketCap"),
            "rev_growth":   info.get("revenueGrowth"),
            "gross_margin": info.get("grossMargins"),
            "pe_fwd":       info.get("forwardPE"),
            "beta":         info.get("beta"),
            "short_float":  info.get("shortPercentOfFloat"),
            "analyst_target": info.get("targetMeanPrice"),
            "analyst_count":  info.get("numberOfAnalystOpinions"),
        }
    except Exception as e:
        _log.debug(f"fetch_pyme {ticker}: {e}")
        return {}


def _score_pyme(ticker: str, meta: dict, data: dict) -> dict:
    """
    Scoring adaptado a small caps / growth:
    - Momentum (0-45): lo más importante, el mercado habla
    - Calidad negocio (0-35): crecimiento revenue, margen bruto, cash
    - Analyst conviction (0-20): consenso y upside analistas
    """
    if not data:
        return {"total": 0, "momentum": 0, "quality": 0, "analyst": 0,
                "flags": [], "warnings": [], "verdict": "Sin datos"}

    flags = []; warnings = []; momentum = 0; quality = 0; analyst_sc = 0

    r1m = data.get("ret_1m", 0); r3m = data.get("ret_3m", 0)
    r6m = data.get("ret_6m", 0); r1y = data.get("ret_1y", 0)

    # ── Momentum (0-45) ───────────────────────────────────────────────────────
    if r1y > 50:
        momentum += 15; flags.append(f"🚀 +{r1y:.0f}% en 1 año — momentum EXPLOSIVO")
    elif r1y > 20:
        momentum += 10; flags.append(f"📈 +{r1y:.0f}% en 1 año — tendencia fuerte")
    elif r1y > 0:
        momentum += 5
    elif r1y < -40:
        momentum -= 5; warnings.append(f"⚠️ -{abs(r1y):.0f}% en 1 año — debilidad severa")

    if r6m > 25:
        momentum += 12; flags.append(f"🔥 +{r6m:.0f}% en 6M — aceleración")
    elif r6m > 10:
        momentum += 8
    elif r6m > 0:
        momentum += 4
    elif r6m < -20:
        momentum -= 4

    if r3m > 15:
        momentum += 10; flags.append(f"✅ +{r3m:.0f}% en 3M — momentum corto fuerte")
    elif r3m > 5:
        momentum += 6
    elif r3m > 0:
        momentum += 3
    elif r3m < -15:
        momentum -= 3

    if r1m > 5:
        momentum += 6; flags.append(f"✅ +{r1m:.0f}% este mes")
    elif r1m > 0:
        momentum += 3
    elif r1m < -10:
        momentum -= 2

    if data.get("above_ema200"):
        momentum += 2; flags.append("✅ Sobre EMA200 — tendencia LP intacta")
    else:
        warnings.append("⚠️ Bajo EMA200 — tendencia LP rota")

    pct_hi = data.get("pct_from_hi", 0)
    if pct_hi > -15:
        momentum += 3; flags.append(f"💪 A solo {abs(pct_hi):.0f}% del máximo 52 semanas")
    elif pct_hi < -50:
        warnings.append(f"⚠️ {abs(pct_hi):.0f}% lejos del máximo — muy castigada")

    momentum = max(0, min(45, momentum))

    # ── Calidad negocio (0-35) ────────────────────────────────────────────────
    rv = data.get("rev_growth")
    if rv is not None:
        rv_pct = rv * 100
        if rv_pct > 50:
            quality += 15; flags.append(f"🚀 Crecimiento ingresos +{rv_pct:.0f}% — hipercrecimiento")
        elif rv_pct > 25:
            quality += 10; flags.append(f"📈 Crecimiento ingresos +{rv_pct:.0f}%")
        elif rv_pct > 10:
            quality += 6
        elif rv_pct < 0:
            quality -= 3; warnings.append(f"⚠️ Ingresos cayendo {rv_pct:.0f}%")

    gm = data.get("gross_margin")
    if gm is not None:
        gm_pct = gm * 100
        if gm_pct > 60:
            quality += 10; flags.append(f"💎 Margen bruto {gm_pct:.0f}% — negocio de alta calidad")
        elif gm_pct > 40:
            quality += 6; flags.append(f"✅ Margen bruto {gm_pct:.0f}%")
        elif gm_pct > 20:
            quality += 3
        elif gm_pct < 10:
            warnings.append(f"⚠️ Margen bruto bajo {gm_pct:.0f}%")

    beta = data.get("beta")
    if beta is not None and beta > 0:
        if beta > 2.5:
            warnings.append(f"⚠️ Beta {beta:.1f} — muy alta volatilidad esperada")
        elif 1.5 <= beta <= 2.5:
            quality += 5; flags.append(f"📊 Beta {beta:.1f} — volatilidad alta pero controlable")

    dd = data.get("max_dd_2y", 0)
    if dd > -30:
        quality += 5; flags.append(f"✅ Drawdown max 2Y: {dd:.0f}% — aguanta bien")
    elif dd < -70:
        warnings.append(f"⚠️ Drawdown 2Y: {dd:.0f}% — muy volátil")

    quality = max(0, min(35, quality))

    # ── Analistas (0-20) ──────────────────────────────────────────────────────
    target  = data.get("analyst_target")
    n_anal  = data.get("analyst_count", 0) or 0
    px      = data.get("price", 1)
    if target and px > 0 and n_anal >= 3:
        upside = (target - px) / px * 100
        if upside > 50:
            analyst_sc += 20; flags.append(f"🎯 Upside analistas: +{upside:.0f}% (objetivo ${target:.2f})")
        elif upside > 25:
            analyst_sc += 14; flags.append(f"🎯 Upside analistas: +{upside:.0f}%")
        elif upside > 10:
            analyst_sc += 8
        elif upside < -10:
            warnings.append(f"⚠️ Analistas por debajo del precio actual")

    analyst_sc = max(0, min(20, analyst_sc))

    total   = momentum + quality + analyst_sc
    total   = max(0, min(100, total))
    risk    = meta.get("risk", 3)

    if total >= 70 and risk <= 3:
        verdict = "🟢 OPORTUNIDAD FUERTE"
    elif total >= 70:
        verdict = "🟡 OPORTUNIDAD (alto riesgo)"
    elif total >= 50:
        verdict = "🔵 SEGUIR DE CERCA"
    elif total >= 35:
        verdict = "🟠 ESPERAR MOMENTO"
    else:
        verdict = "🔴 NO AHORA"

    return {
        "total": total, "momentum": momentum, "quality": quality,
        "analyst": analyst_sc, "flags": flags, "warnings": warnings,
        "verdict": verdict,
    }


@st.cache_data(ttl=14400, show_spinner=False)
def _get_macro_context() -> dict:
    """
    Entorno macroeconómico actual.
    Usa yfinance para tasas + VIX + SP500 como proxies del ciclo económico.
    """
    ctx = {
        "fed_rate_proxy": None,   # ^TNX = 10Y yield
        "vix": None,
        "sp500_1y": None,
        "dxy_1y": None,
        "gold_1y": None,
        "signal": "neutral",
        "description": "Entorno macroeconómico neutral.",
        "favors": [],
    }
    try:
        import yfinance as yf
        _tickers = {"TNX": "^TNX", "VIX": "^VIX", "SPY": "SPY", "DXY": "DX-Y.NYB", "GLD": "IAU"}
        _data = yf.download(
            list(_tickers.values()), period="1y", auto_adjust=True, progress=False
        )
        close = _data["Close"] if "Close" in _data.columns else _data.xs("Close", axis=1, level=0)

        def _ret(col):
            s = close[col].dropna()
            if s.empty: return 0
            return (float(s.iloc[-1]) - float(s.iloc[0])) / float(s.iloc[0]) * 100

        ctx["fed_rate_proxy"] = round(float(close["^TNX"].dropna().iloc[-1]), 2) if "^TNX" in close else None
        ctx["vix"]            = round(float(close["^VIX"].dropna().iloc[-1]), 1)  if "^VIX" in close else None
        ctx["sp500_1y"]       = round(_ret("SPY"), 1)
        ctx["gold_1y"]        = round(_ret("IAU"), 1)
        ctx["dxy_1y"]         = round(_ret("DX-Y.NYB"), 1) if "DX-Y.NYB" in close.columns else None

        # Interpretar entorno
        rate = ctx["fed_rate_proxy"] or 4.0
        vix  = ctx["vix"] or 18.0
        sp1y = ctx["sp500_1y"]

        favors = []
        if rate > 5.0:
            ctx["signal"] = "defensive"
            ctx["description"] = f"Tipos altos ({rate:.1f}%) — favorece bonos corto plazo, calidad y dividendo."
            favors += ["bond_short", "bond_tips", "dividend", "bond_total"]
        elif rate < 3.0:
            ctx["signal"] = "growth"
            ctx["description"] = f"Tipos bajos ({rate:.1f}%) — favorece acciones growth y ETFs globales."
            favors += ["equity_growth", "equity_usa", "tech", "real_estate"]
        else:
            ctx["signal"] = "balanced"
            ctx["description"] = f"Tipos moderados ({rate:.1f}%) — entorno balanceado para acciones y bonos."
            favors += ["equity_usa", "dividend", "bond_total"]

        if vix > 28:
            ctx["description"] += " VIX alto — mercado en estrés, reduce posiciones y aumenta calidad."
            favors = ["bond_total", "gold", "dividend"]
        elif vix < 16:
            ctx["description"] += " VIX bajo — baja volatilidad, entorno favorable para acciones."
            favors += ["equity_usa", "equity_growth"]

        if (ctx["gold_1y"] or 0) > 10:
            favors += ["gold", "commodity"]

        ctx["favors"] = list(set(favors))

    except Exception as e:
        _log.debug(f"macro_context: {e}")

    return ctx


def _apply_macro_score(cat: str, macro_ctx: dict) -> int:
    """Ajusta el score macro de un activo según su categoría y el entorno actual."""
    favors = macro_ctx.get("favors", [])
    signal = macro_ctx.get("signal", "neutral")

    if cat in favors:
        return 18   # entorno muy favorable
    if signal == "defensive":
        if cat in ("equity_growth", "sector_tech", "equity_em"):
            return 4   # entorno difícil para growth en tipos altos
        if cat in ("bond_total", "bond_short", "bond_tips", "dividend"):
            return 18
    if signal == "growth":
        if cat in ("equity_growth", "equity_usa", "tech"):
            return 18
        if cat in ("bond_long",):
            return 4
    return 10   # neutro


# ─────────────────────────────────────────────────────────────────────────────
# Portfolio construction
# ─────────────────────────────────────────────────────────────────────────────

def _build_portfolio(scored_assets: list, profile_key: str, capital: float) -> list:
    """
    Selecciona los mejores activos por categoría según el perfil de riesgo
    y calcula la alocación de capital.
    """
    profile = RISK_PROFILES[profile_key]

    # Separar por categoría
    equities   = [a for a in scored_assets if a["cat"] in
                  ("equity_usa","equity_global","equity_growth","equity_intl","equity_small","equity_em","sector_tech","sector_health","sector_industry","sector_finance","sector_energy","real_estate")]
    bonds      = [a for a in scored_assets if "bond" in a["cat"]]
    gold_comm  = [a for a in scored_assets if a["cat"] in ("gold","commodity")]
    stocks     = [a for a in scored_assets if a["type"] == "stock"]
    dividends  = [a for a in scored_assets if a["cat"] == "dividend"]

    # Ordenar por score total
    for lst in [equities, bonds, gold_comm, stocks, dividends]:
        lst.sort(key=lambda x: x["score"], reverse=True)

    portfolio = []

    # Renta variable (ETFs)
    eq_budget  = profile["equity_pct"]
    eq_picks   = (dividends[:1] + equities[:3])[:3]   # 1 dividendo + 2 índices
    if eq_picks:
        per_eq = eq_budget / len(eq_picks)
        for a in eq_picks:
            portfolio.append({**a, "alloc_pct": round(per_eq, 1),
                               "alloc_eur": round(capital * per_eq / 100, 0)})

    # Renta fija
    bond_budget = profile["bond_pct"]
    bond_picks  = bonds[:2]
    if bond_picks:
        per_b = bond_budget / len(bond_picks)
        for a in bond_picks:
            portfolio.append({**a, "alloc_pct": round(per_b, 1),
                               "alloc_eur": round(capital * per_b / 100, 0)})

    # Oro / Commodities
    gold_budget = profile["gold_pct"]
    if gold_comm:
        portfolio.append({**gold_comm[0], "alloc_pct": round(gold_budget, 1),
                          "alloc_eur": round(capital * gold_budget / 100, 0)})

    # Acciones individuales (solo perfil moderado/agresivo)
    stk_budget = profile.get("stocks_pct", 0)
    if stk_budget > 0 and stocks:
        stk_picks = stocks[:3]
        per_s = stk_budget / len(stk_picks)
        for a in stk_picks:
            portfolio.append({**a, "alloc_pct": round(per_s, 1),
                               "alloc_eur": round(capital * per_s / 100, 0)})

    return portfolio


# ─────────────────────────────────────────────────────────────────────────────
# Render principal
# ─────────────────────────────────────────────────────────────────────────────

def render_investment_module():
    """Punto de entrada: renderiza el módulo completo de inversión a largo plazo."""

    st.markdown("""
    <style>
    .inv-card{background:#1a1f2e;border:1px solid #2d3748;border-radius:12px;padding:16px;margin-bottom:12px}
    .inv-green{color:#48bb78;font-weight:700}
    .inv-yellow{color:#ecc94b;font-weight:700}
    .inv-red{color:#fc8181;font-weight:700}
    .inv-title{font-size:1.3rem;font-weight:700;margin-bottom:4px}
    .alloc-bar{height:8px;border-radius:4px;background:#48bb78;display:inline-block}
    </style>
    """, unsafe_allow_html=True)

    # ── Header ────────────────────────────────────────────────────────────────
    c_title, c_back = st.columns([5, 1])
    with c_title:
        st.markdown("# 📈 Inversión a Largo Plazo")
        st.caption("Análisis fundamental · técnico · macroeconómico · cartera diversificada 1-5 años")
    with c_back:
        if st.button("← Trading", use_container_width=True):
            st.session_state.app_mode = None
            st.rerun()

    st.markdown("---")

    # ── Configuración del usuario ─────────────────────────────────────────────
    cfg_col1, cfg_col2, cfg_col3 = st.columns(3)
    with cfg_col1:
        profile_key = st.selectbox(
            "Perfil de riesgo",
            list(RISK_PROFILES.keys()),
            format_func=lambda k: RISK_PROFILES[k]["label"],
        )
    with cfg_col2:
        capital = st.number_input(
            "Capital a invertir (€/$)",
            min_value=500, max_value=500_000, value=10_000, step=500,
        )
    with cfg_col3:
        horizon = st.selectbox("Horizonte temporal", ["1 año", "2 años", "3 años", "5 años"])

    profile = RISK_PROFILES[profile_key]

    # ── Tabs ──────────────────────────────────────────────────────────────────
    tab_port, tab_screen, tab_pymes, tab_macro, tab_guide = st.tabs([
        "🎯 Mi Cartera",
        "📊 Screener de Activos",
        "💣 PYMEs Explosivas",
        "🌍 Entorno Macro",
        "📖 Guía de Inversión",
    ])

    # ── Cargar datos (con spinner) ─────────────────────────────────────────────
    with st.spinner("Analizando universo de activos... (primera carga ~30 segundos)"):
        macro_ctx = _get_macro_context()

        # Recopilar todos los tickers
        all_meta = {}
        for group, assets in UNIVERSE.items():
            for ticker, meta in assets.items():
                all_meta[ticker] = {**meta, "group": group}

        # Fetch + score todos los activos
        scored = []
        _failed = []
        for ticker, meta in all_meta.items():
            data = _fetch_asset(ticker)
            if not data:
                _failed.append(ticker)
                continue
            sc = _score_asset(ticker, meta, data)
            # Aplicar score macro real
            sc["macro"] = _apply_macro_score(meta["cat"], macro_ctx)
            sc["total"] = min(100, sc["tech"] + sc["fund"] + sc["macro"])
            # Recalcular veredicto con score real
            if sc["total"] >= 75:
                sc["verdict"] = "🟢 COMPRAR"
            elif sc["total"] >= 58:
                sc["verdict"] = "🟡 ACUMULAR"
            elif sc["total"] >= 40:
                sc["verdict"] = "🟠 ESPERAR"
            else:
                sc["verdict"] = "🔴 EVITAR"

            scored.append({
                "ticker": ticker,
                "name":   meta["name"],
                "type":   meta["type"],
                "cat":    meta["cat"],
                "er":     meta.get("er", 0),
                "group":  meta["group"],
                "score":  sc["total"],
                "tech":   sc["tech"],
                "fund":   sc["fund"],
                "macro":  sc["macro"],
                "verdict": sc["verdict"],
                "flags":   sc["flags"],
                "warnings": sc.get("warnings", []),
                "data":   data,
            })

        scored.sort(key=lambda x: x["score"], reverse=True)

    # ══════════════════════════════════════════════════════════════════════════
    # TAB 1: MI CARTERA
    # ══════════════════════════════════════════════════════════════════════════
    with tab_port:
        st.markdown(f"### {profile['label']}")

        # Métricas del perfil
        pm1, pm2, pm3, pm4 = st.columns(4)
        pm1.metric("Capital", f"{capital:,.0f} €")
        pm2.metric("Rentabilidad esperada", profile["return_annual"] + " anual")
        pm3.metric("Drawdown máximo tolerable", profile["dd_max"])
        pm4.metric("Horizonte", profile["horizon"])

        st.info(profile["desc"])

        if macro_ctx.get("signal") == "defensive":
            st.warning(f"🏦 **Entorno defensivo**: {macro_ctx['description']}")
        elif macro_ctx.get("signal") == "growth":
            st.success(f"🚀 **Entorno growth**: {macro_ctx['description']}")
        else:
            st.info(f"⚖️ **Entorno balanceado**: {macro_ctx['description']}")

        # Construir cartera
        portfolio = _build_portfolio(scored, profile_key, capital)

        if not portfolio:
            st.error("No se pudo construir la cartera — sin datos de activos.")
        else:
            st.markdown("#### 💼 Cartera recomendada")

            # Tabla resumen
            _pdf = pd.DataFrame([{
                "Activo":     f"{p['ticker']} — {p['name']}",
                "Tipo":       "ETF" if p["type"] == "etf" else "Acción",
                "% Cartera":  f"{p['alloc_pct']:.1f}%",
                f"Capital (€/{capital:,.0f})": f"{p['alloc_eur']:,.0f} €",
                "Score":      f"{p['score']}/100",
                "Veredicto":  p["verdict"],
                "Retorno 1Y": f"{p['data'].get('ret_1y', 0):+.1f}%",
            } for p in portfolio])
            st.dataframe(_pdf, use_container_width=True, hide_index=True)

            # Pie chart de alocación
            try:
                import plotly.express as _px
                _pie_data = pd.DataFrame({
                    "Activo": [f"{p['ticker']}\n{p['alloc_pct']:.0f}%" for p in portfolio],
                    "Peso":   [p["alloc_pct"] for p in portfolio],
                })
                _fig_pie = _px.pie(
                    _pie_data, values="Peso", names="Activo",
                    title="Distribución de la cartera",
                    color_discrete_sequence=_px.colors.qualitative.Set3,
                    hole=0.35,
                )
                _fig_pie.update_layout(template="plotly_dark", height=400)
                st.plotly_chart(_fig_pie, use_container_width=True)
            except Exception:
                pass

            # Detalle por activo
            st.markdown("#### 🔍 Análisis detallado por posición")
            for p in portfolio:
                with st.expander(
                    f"{p['ticker']} — {p['name']}  |  {p['alloc_pct']:.1f}%  |  {p['verdict']}",
                    expanded=False,
                ):
                    dc1, dc2, dc3, dc4 = st.columns(4)
                    dc1.metric("Score total", f"{p['score']}/100")
                    dc2.metric("Técnico", f"{p['tech']}/40")
                    dc3.metric("Fundamental", f"{p['fund']}/40")
                    dc4.metric("Macro", f"{p['macro']}/20")

                    _d = p["data"]
                    ec1, ec2, ec3, ec4 = st.columns(4)
                    ec1.metric("Precio actual", f"${_d.get('price', 0):.2f}")
                    ec2.metric("Retorno 1 año", f"{_d.get('ret_1y', 0):+.1f}%",
                               delta="positivo" if _d.get("ret_1y", 0) > 0 else "negativo")
                    ec3.metric("RSI semanal", f"{_d.get('rsi_w', 0):.0f}")
                    ec4.metric("Max DD 2Y", f"{_d.get('max_dd_2y', 0):.0f}%")

                    if p["flags"]:
                        st.markdown("**Puntos positivos:**")
                        for f in p["flags"][:5]:
                            st.markdown(f"  {f}")
                    if p["warnings"]:
                        st.markdown("**Puntos de atención:**")
                        for w in p["warnings"][:3]:
                            st.markdown(f"  {w}")

                    # Mini chart de precio
                    try:
                        import plotly.graph_objects as _go
                        _hist = _d.get("hist")
                        if _hist is not None and len(_hist) > 20:
                            _fig_h = _go.Figure()
                            _fig_h.add_trace(_go.Scatter(
                                x=_hist.index, y=_hist.values,
                                mode="lines", name=p["ticker"],
                                line=dict(color="limegreen" if _d.get("ret_1y", 0) > 0 else "tomato", width=1.5),
                                fill="tozeroy",
                                fillcolor="rgba(72,187,120,0.06)" if _d.get("ret_1y", 0) > 0 else "rgba(252,129,129,0.06)",
                            ))
                            _fig_h.update_layout(
                                template="plotly_dark", height=200,
                                margin=dict(l=20, r=10, t=10, b=20),
                                showlegend=False,
                            )
                            st.plotly_chart(_fig_h, use_container_width=True)
                    except Exception:
                        pass

            # Rentabilidad esperada acumulada
            st.markdown("#### 📈 Proyección de crecimiento (escenarios)")
            try:
                import plotly.graph_objects as _go2
                _horizon_map = {"1 año": 1, "2 años": 2, "3 años": 3, "5 años": 5}
                _yrs = _horizon_map.get(horizon, 3)
                _years = list(range(_yrs + 1))

                # Extraer retorno anual esperado del perfil
                _ret_str = profile["return_annual"]  # e.g. "7-11%"
                _parts = _ret_str.replace("%", "").split("–")
                _r_low  = float(_parts[0]) / 100
                _r_high = float(_parts[1]) / 100
                _r_mid  = (_r_low + _r_high) / 2

                _fig_proj = _go2.Figure()
                for _r, _lbl, _clr in [
                    (_r_low,  "Escenario conservador", "orange"),
                    (_r_mid,  "Escenario base",        "limegreen"),
                    (_r_high, "Escenario optimista",   "cyan"),
                ]:
                    _vals = [capital * (1 + _r) ** y for y in _years]
                    _fig_proj.add_trace(_go2.Scatter(
                        x=_years, y=_vals, mode="lines+markers",
                        name=_lbl, line=dict(color=_clr, width=2),
                    ))

                _fig_proj.update_layout(
                    title=f"Proyección de {capital:,.0f}€ a {_yrs} años",
                    xaxis_title="Años", yaxis_title="Capital (€)",
                    template="plotly_dark", height=320,
                    margin=dict(l=40, r=20, t=50, b=40),
                )
                st.plotly_chart(_fig_proj, use_container_width=True)
                st.caption("Proyección basada en retornos históricos. No garantiza resultados futuros.")
            except Exception:
                pass

    # ══════════════════════════════════════════════════════════════════════════
    # TAB 2: SCREENER
    # ══════════════════════════════════════════════════════════════════════════
    with tab_screen:
        st.markdown("### 📊 Todos los activos analizados")

        if _failed:
            st.caption(f"No se pudieron cargar: {', '.join(_failed)}")

        # Filtros
        sf1, sf2, sf3 = st.columns(3)
        with sf1:
            _min_score = st.slider("Score mínimo", 0, 100, 50)
        with sf2:
            _type_filter = st.multiselect(
                "Tipo", ["ETF", "Acción"], default=["ETF", "Acción"]
            )
        with sf3:
            _group_filter = st.multiselect(
                "Grupo", list(UNIVERSE.keys()), default=list(UNIVERSE.keys())
            )

        _filtered = [
            a for a in scored
            if a["score"] >= _min_score
            and ("ETF" if a["type"] == "etf" else "Acción") in _type_filter
            and a["group"] in _group_filter
        ]

        # Tabla completa
        _sdf = pd.DataFrame([{
            "Ticker":     a["ticker"],
            "Nombre":     a["name"],
            "Grupo":      a["group"].split(" ", 1)[-1],
            "Score":      a["score"],
            "Técnico":    a["tech"],
            "Fundamental":a["fund"],
            "Macro":      a["macro"],
            "Veredicto":  a["verdict"],
            "Ret 1M":     f"{a['data'].get('ret_1m', 0):+.1f}%",
            "Ret 3M":     f"{a['data'].get('ret_3m', 0):+.1f}%",
            "Ret 1Y":     f"{a['data'].get('ret_1y', 0):+.1f}%",
            "RSI Semanal":f"{a['data'].get('rsi_w', 0):.0f}",
            "EMA200":     "✅" if a["data"].get("above_ema200") else "❌",
        } for a in _filtered])

        st.dataframe(
            _sdf.sort_values("Score", ascending=False),
            use_container_width=True, hide_index=True,
        )

        # Top 5 por categoría
        st.markdown("#### 🏆 Top 5 activos por score")
        _top5 = _filtered[:5]
        _cols = st.columns(5)
        for i, a in enumerate(_top5[:5]):
            with _cols[i]:
                _clr = "🟢" if a["score"] >= 70 else "🟡" if a["score"] >= 55 else "🟠"
                st.metric(
                    f"{_clr} {a['ticker']}",
                    f"{a['score']}/100",
                    delta=f"{a['data'].get('ret_1y', 0):+.1f}% 1Y",
                )
                st.caption(a["verdict"])

    # ══════════════════════════════════════════════════════════════════════════
    # ══════════════════════════════════════════════════════════════════════════
    # TAB 3: PYMEs EXPLOSIVAS
    # ══════════════════════════════════════════════════════════════════════════
    with tab_pymes:
        st.markdown("### 💣 Las 20 PYMEs con Mayor Potencial Explosivo")
        st.markdown(
            "Small y mid-caps en sectores con catalizadores fuertes: IA, espacio, biotech, "
            "defensa, movilidad y energía. **Alto riesgo — alto potencial.** "
            "Nunca más del 5-10% de tu cartera en cada una."
        )

        _pyme_risk_filter = st.select_slider(
            "Máximo nivel de riesgo a mostrar",
            options=[1, 2, 3, 4, 5],
            value=5,
            format_func=lambda x: _RISK_STARS[x],
        )

        # Cargar datos PYMEs
        with st.spinner("Analizando 20 PYMEs... (~20 segundos la primera vez)"):
            _pyme_scored = []
            for _tk, _pm in PYME_UNIVERSE.items():
                _pd_data = _fetch_pyme(_tk)
                _ps = _score_pyme(_tk, _pm, _pd_data)
                _pyme_scored.append({
                    "ticker":   _tk,
                    "meta":     _pm,
                    "data":     _pd_data,
                    "score":    _ps["total"],
                    "momentum": _ps["momentum"],
                    "quality":  _ps["quality"],
                    "analyst":  _ps["analyst"],
                    "verdict":  _ps["verdict"],
                    "flags":    _ps["flags"],
                    "warnings": _ps["warnings"],
                })
            _pyme_scored.sort(key=lambda x: x["score"], reverse=True)

        # Filtrar por riesgo
        _pyme_filtered = [p for p in _pyme_scored if p["meta"].get("risk", 5) <= _pyme_risk_filter]

        # ── Ranking visual ────────────────────────────────────────────────────
        st.markdown(f"#### 🏆 Ranking ({len(_pyme_filtered)} empresas)")

        _pcols = st.columns([2, 1, 1, 1, 1, 2])
        for _h in ["Empresa", "Score", "Momento", "Calidad", "Riesgo", "Veredicto"]:
            _pcols[["Empresa", "Score", "Momento", "Calidad", "Riesgo", "Veredicto"].index(_h)]\
                .markdown(f"**{_h}**")

        for _p in _pyme_filtered:
            _c1, _c2, _c3, _c4, _c5, _c6 = st.columns([2, 1, 1, 1, 1, 2])
            _d  = _p["data"]
            _m  = _p["meta"]
            _c1.markdown(f"**{_p['ticker']}** — {_m['name'].split('—')[0].strip()}")
            _c2.markdown(f"**{_p['score']}/100**")
            _c3.markdown(f"{_p['momentum']}/45")
            _c4.markdown(f"{_p['quality']}/35")
            _c5.markdown(_RISK_STARS.get(_m.get("risk", 3), "⭐⭐⭐"))
            _c6.markdown(_p["verdict"])

        st.markdown("---")

        # ── Tarjetas detalladas por empresa ───────────────────────────────────
        st.markdown("#### 🔍 Análisis detallado por empresa")

        for _p in _pyme_filtered:
            _d = _p["data"]
            _m = _p["meta"]
            _tk = _p["ticker"]
            _sector_color = _SECTOR_COLORS.get(_m["sector"], "#2d3748")

            with st.expander(
                f"{_tk}  ·  {_m['name']}  ·  Score {_p['score']}/100  ·  {_p['verdict']}",
                expanded=False,
            ):
                # Cabecera con sector y riesgo
                st.markdown(
                    f"<div style='background:{_sector_color};border-radius:8px;"
                    f"padding:10px 16px;margin-bottom:12px'>"
                    f"<b>{_m['sector']}</b> &nbsp;|&nbsp; "
                    f"Riesgo: {_RISK_STARS.get(_m['risk'], '⭐⭐⭐')}"
                    f"</div>",
                    unsafe_allow_html=True,
                )

                # Métricas de precio
                _pm1, _pm2, _pm3, _pm4, _pm5 = st.columns(5)
                _pm1.metric("Precio", f"${_d.get('price', 0):.2f}")
                _pm2.metric("1 Mes",  f"{_d.get('ret_1m', 0):+.1f}%",
                            delta="▲" if _d.get("ret_1m", 0) > 0 else "▼")
                _pm3.metric("3 Meses", f"{_d.get('ret_3m', 0):+.1f}%")
                _pm4.metric("6 Meses", f"{_d.get('ret_6m', 0):+.1f}%")
                _pm5.metric("1 Año",   f"{_d.get('ret_1y', 0):+.1f}%",
                            delta="▲" if _d.get("ret_1y", 0) > 0 else "▼")

                # Distancia máximo 52W
                _hi52 = _d.get("hi52", 0)
                _lo52 = _d.get("lo52", 0)
                _px   = _d.get("price", 0)
                if _hi52 and _lo52:
                    _range = _hi52 - _lo52
                    _pos   = (_px - _lo52) / _range if _range > 0 else 0.5
                    st.markdown(
                        f"**Rango 52 semanas:** ${_lo52:.2f} ← "
                        f"actual ${_px:.2f} ({_pos*100:.0f}%) "
                        f"→ máx ${_hi52:.2f}   "
                        f"| {_d.get('pct_from_hi', 0):.1f}% desde el máximo"
                    )

                st.markdown("---")

                # Tesis de inversión
                st.markdown(f"**💡 Tesis de inversión:**")
                st.info(_m["thesis"])

                # Catalizadores y riesgos en 2 columnas
                _cat_col, _risk_col = st.columns(2)
                with _cat_col:
                    st.markdown("**🚀 Catalizadores:**")
                    for _cat in _m.get("catalysts", []):
                        st.markdown(f"  ✅ {_cat}")
                with _risk_col:
                    st.markdown("**⚠️ Riesgos:**")
                    for _rsk in _m.get("risks", []):
                        st.markdown(f"  🔴 {_rsk}")

                # Señales del scoring
                if _p["flags"]:
                    st.markdown("**📊 Señales positivas detectadas:**")
                    for _f in _p["flags"][:6]:
                        st.markdown(f"  {_f}")
                if _p["warnings"]:
                    st.markdown("**⚠️ Atención:**")
                    for _w in _p["warnings"][:3]:
                        st.markdown(f"  {_w}")

                # Fundamentales si disponibles
                _rev_g  = _d.get("rev_growth")
                _gm     = _d.get("gross_margin")
                _target = _d.get("analyst_target")
                _n_a    = _d.get("analyst_count", 0) or 0
                _mcap   = _d.get("market_cap")

                _fund_items = []
                if _mcap:
                    _mc_b = _mcap / 1e9
                    _fund_items.append(f"Market cap: ${_mc_b:.1f}B")
                if _rev_g is not None:
                    _fund_items.append(f"Crecimiento ingresos: {_rev_g*100:+.0f}%")
                if _gm is not None:
                    _fund_items.append(f"Margen bruto: {_gm*100:.0f}%")
                if _target and _n_a >= 3 and _d.get("price", 0) > 0:
                    _ups = (_target - _d["price"]) / _d["price"] * 100
                    _fund_items.append(f"Objetivo analistas: ${_target:.2f} ({_ups:+.0f}%, {_n_a} analistas)")

                if _fund_items:
                    st.markdown("**📋 Datos fundamentales:**  " + "  ·  ".join(_fund_items))

                # Mini gráfico precio
                try:
                    import plotly.graph_objects as _go_p
                    _hist_p = _d.get("hist")
                    if _hist_p is not None and len(_hist_p) > 30:
                        _ema50_s = _hist_p.ewm(span=50, adjust=False).mean()
                        _fig_p = _go_p.Figure()
                        _fig_p.add_trace(_go_p.Scatter(
                            x=_hist_p.index, y=_hist_p.values,
                            mode="lines", name="Precio",
                            line=dict(
                                color="limegreen" if _d.get("ret_1y", 0) > 0 else "tomato",
                                width=1.5,
                            ),
                        ))
                        _fig_p.add_trace(_go_p.Scatter(
                            x=_ema50_s.index, y=_ema50_s.values,
                            mode="lines", name="EMA50",
                            line=dict(color="orange", width=1, dash="dot"),
                        ))
                        _fig_p.update_layout(
                            template="plotly_dark", height=220,
                            margin=dict(l=20, r=10, t=10, b=20),
                            showlegend=True, legend=dict(x=0, y=1),
                        )
                        st.plotly_chart(_fig_p, use_container_width=True)
                except Exception:
                    pass

                # Nota de riesgo
                if _m.get("risk", 3) >= 4:
                    st.warning(
                        "⚠️ **Inversión de ALTO RIESGO** — Solo para capital que puedes perder totalmente. "
                        "Máximo 2-3% de tu cartera. No es renta fija, es apuesta con upside asimétrico."
                    )

        # Disclaimer final
        st.markdown("---")
        st.caption(
            "💣 Las PYMEs explosivas son inversiones especulativas de alto riesgo. "
            "Pueden multiplicar por 5-10x O perder el 90% de su valor. "
            "Nunca inviertas más del 10% de tu cartera en este tipo de activos. "
            "Este análisis es informativo, no asesoramiento financiero."
        )

    # ══════════════════════════════════════════════════════════════════════════
    # TAB 4: ENTORNO MACRO
    # ══════════════════════════════════════════════════════════════════════════
    with tab_macro:
        st.markdown("### 🌍 Entorno Macroeconómico")

        if macro_ctx.get("signal") == "defensive":
            st.error(f"🏦 **ENTORNO DEFENSIVO** — {macro_ctx['description']}")
        elif macro_ctx.get("signal") == "growth":
            st.success(f"🚀 **ENTORNO GROWTH** — {macro_ctx['description']}")
        else:
            st.info(f"⚖️ **ENTORNO BALANCEADO** — {macro_ctx['description']}")

        mc1, mc2, mc3 = st.columns(3)
        mc1.metric(
            "Bono 10Y USA (proxy Fed)",
            f"{macro_ctx.get('fed_rate_proxy', 'N/A')}%",
            help=">5% = tipos altos, entorno restrictivo",
        )
        mc2.metric(
            "VIX (miedo del mercado)",
            f"{macro_ctx.get('vix', 'N/A')}",
            help="<16=calma, 16-28=normal, >28=estrés",
        )
        mc3.metric(
            "S&P 500 retorno 1 año",
            f"{macro_ctx.get('sp500_1y', 'N/A'):+.1f}%" if macro_ctx.get("sp500_1y") else "N/A",
        )

        st.markdown("#### ¿Qué activos favorece este entorno?")
        _favors = macro_ctx.get("favors", [])
        _fav_map = {
            "equity_usa": "📈 Índices USA (SPY, VTI)",
            "equity_growth": "🚀 ETFs Growth (QQQ)",
            "bond_total": "🏦 Bonos diversificados (BND)",
            "bond_short": "🛡️ Bonos corto plazo (VCSH)",
            "bond_tips": "🛡️ TIPS anti-inflación (VTIP)",
            "dividend": "💵 ETF Dividendo (SCHD)",
            "gold": "🥇 Oro (IAU, GLD)",
            "commodity": "🛢️ Materias primas",
            "real_estate": "🏠 REIT (VNQ)",
            "tech": "💻 Tecnología (XLK, NVDA)",
        }
        for f in _favors:
            if f in _fav_map:
                st.success(f"  ✅ {_fav_map[f]}")

        st.markdown("""
        #### Claves para interpretar el entorno:

        | Indicador | Favorable para acciones | Favorable para bonos |
        |-----------|------------------------|---------------------|
        | Tipos bajos (<3%) | ✅ Sí | ❌ No (precio bono sube pero yield baja) |
        | Tipos altos (>5%) | ⚠️ Presión | ✅ Yield alta sin riesgo |
        | VIX < 16 | ✅ Mercado tranquilo | — |
        | VIX > 28 | ⚠️ Estrés — reducir riesgo | ✅ Refugio |
        | Inflación alta | ✅ TIPS, Oro, Energía | ❌ Bonos nominales |
        | Recesión | ❌ Bolsa cae | ✅ Bonos gobierno suben |
        """)

    # ══════════════════════════════════════════════════════════════════════════
    # TAB 4: GUÍA
    # ══════════════════════════════════════════════════════════════════════════
    with tab_guide:
        st.markdown("### 📖 Guía de Inversión a Largo Plazo")

        st.markdown("""
        #### Por qué la cartera diversificada funciona

        Históricamente (datos desde 1970):
        - **S&P 500** (SPY): +10.5% anual medio
        - **Cartera 60/40** (60% bolsa + 40% bonos): +8.5% anual medio
        - **Inflación media**: +3% anual

        Una cartera diversificada con ETFs de bajo coste ha batido
        a más del **90% de los gestores activos** en horizontes de 10+ años.

        #### Las 5 reglas de oro

        **1. Empieza ya — el tiempo es el activo más valioso**
        ```
        10.000€ al 8% anual:
        5 años  → 14.693€
        10 años → 21.589€
        20 años → 46.610€
        ```

        **2. Diversifica por geografía, sector y tipo de activo**
        No pongas todo en tecnología USA. El mundo cambia.

        **3. Usa ETFs de bajo coste (expense ratio < 0.20%)**
        Un 1% de comisión extra te costará ~25% del capital en 30 años.

        **4. Aportaciones periódicas (Dollar Cost Averaging)**
        Invierte el mismo importe cada mes, sin importar el precio.
        Promedias el coste de entrada automáticamente.

        **5. No vendas en los crashes**
        Las caídas son temporales. Los inversores que vendieron
        en COVID (marzo 2020) perdieron el +100% de rebote.

        #### Cómo usar este sistema

        1. **Elige tu perfil** según cuándo necesitas el dinero
        2. **Revisa la cartera** cada 3-6 meses (no cada día)
        3. **Rebalancea** si algún activo se desvía >10% del target
        4. **Añade capital** mensualmente si puedes
        5. **Ignora el ruido** del mercado a corto plazo

        #### Disclaimer
        Los retornos históricos no garantizan resultados futuros.
        Este sistema es una herramienta de análisis, no asesoramiento financiero.
        Consulta a un asesor financiero regulado para tu situación específica.
        """)

    st.caption("📈 SMC Pro — Módulo de Inversión · Datos: Yahoo Finance · Actualización: cada 24h")
