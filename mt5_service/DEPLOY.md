# Guía de despliegue — MT5 Service en Railway

## Arquitectura resultante

```
Railway Project
├── smc-app          (tu Streamlit actual)   ← ya existe
└── mt5-service      (nuevo servicio Docker) ← esto que acabas de crear
        │
        └── expone REST API en $PORT
                ↕ HTTP interno de Railway
smc-app llama a mt5-service cuando MT5_SERVICE_URL está configurada
```

---

## Pasos para añadir el servicio en Railway

### 1. Haz commit y push de los cambios

```bash
git add mt5_service/ smc_pro_app.py
git commit -m "feat: add MT5 Docker service for Railway autotrading"
git push
```

### 2. En Railway Dashboard → tu proyecto → "+ New Service"

- Elige **"GitHub Repo"** (el mismo repo)
- En **"Root Directory"** pon: `mt5_service`
- Railway detectará el `Dockerfile` automáticamente
- Ponle nombre: `mt5-service`

### 3. Variables de entorno del servicio `mt5-service`

En Railway → mt5-service → Variables:

| Variable          | Valor                          | Descripción                        |
|-------------------|--------------------------------|------------------------------------|
| `MT5_LOGIN`       | `12345678`                     | Número de cuenta MT5               |
| `MT5_PASSWORD`    | `tu_contraseña`                | Contraseña de la cuenta            |
| `MT5_SERVER`      | `ICMarkets-Demo`               | Servidor del broker                |
| `MT5_API_TOKEN`   | `un_token_secreto_largo`       | Token de seguridad interno         |
| `MT5_MAX_RISK_PCT`| `1.0`                          | Riesgo máximo por operación (%)    |
| `MT5_DAILY_LOSS_PCT` | `3.0`                       | Circuit breaker pérdida diaria (%) |

### 4. Variable de entorno del servicio `smc-app`

Una vez el servicio `mt5-service` esté desplegado, Railway le asigna una URL interna.
Ve a Railway → smc-app → Variables:

| Variable          | Valor                                           |
|-------------------|-------------------------------------------------|
| `MT5_SERVICE_URL` | `https://mt5-service.railway.internal` *(o la URL pública que Railway asigne)* |
| `MT5_API_TOKEN`   | el mismo token que pusiste en mt5-service       |

> **Truco Railway**: Para comunicación interna entre servicios usa la URL privada
> `http://mt5-service.railway.internal:PORT`. Ve a mt5-service → Settings → Networking
> para ver el hostname interno.

---

## Verificar que funciona

Desde la terminal de Railway (o con curl):

```bash
# Health check
curl https://tu-mt5-service.railway.app/health

# Respuesta esperada:
# {"status": "ok", "mt5": "connected", "service": "mt5-service"}

# Info de cuenta
curl -H "Authorization: Bearer tu_token" \
     https://tu-mt5-service.railway.app/account

# Ejecutar una orden de prueba (DEMO)
curl -X POST \
  -H "Authorization: Bearer tu_token" \
  -H "Content-Type: application/json" \
  -d '{"symbol":"EURUSD","direction":"BUY","volume":0.01,"price":1.085,"sl":1.082,"tp":1.090}' \
  https://tu-mt5-service.railway.app/trade
```

---

## Cómo fluye el autotrading

```
Usuario activa "Trading Automático" en Streamlit
            ↓
auto_trade_signal() genera señal SMC
            ↓
place_mt5_order() detecta MT5_SERVICE_URL
            ↓
HTTP POST /trade → mt5-service Docker
            ↓
mt5_bridge.py → Wine → MT5 terminal → Broker
```

---

## Notas importantes

- El primer arranque tarda ~2 minutos (instala MT5 en Wine dentro del contenedor)
- Los reinicios posteriores son rápidos (~15 s) porque MT5 ya está instalado
- Railway mantiene el volumen efímero; si el contenedor se reinicia, MT5 necesita
  reiniciarse también (el start.sh lo maneja)
- Para producción real, considera usar un volumen persistente en Railway para que
  MT5 no tenga que reinstalarse en cada deploy
