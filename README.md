# SMC Pro App - Instrucciones de Instalación

## 🚀 Formas de Ejecutar la App

### Opción 1: Archivo .bat (Más Fácil)
1. **Haz doble clic** en `Start_SMC_App.bat`
2. Se abrirá automáticamente en tu navegador

### Opción 2: Script PowerShell
1. **Haz clic derecho** en `Start_SMC_App.ps1`
2. Selecciona "Ejecutar con PowerShell"
3. Si pide permisos, permite la ejecución

### Opción 3: Terminal (Avanzado)
```bash
cd c:\Users\xavi2\Desktop\smc_tool
.\.venv\Scripts\streamlit run smc_pro_app.py
```

## 📋 Requisitos Previos

### 1. MetaTrader 5 (Opcional pero recomendado)
- **Descarga e instala** MT5 desde: https://www.metatrader5.com/es/download
- **Abre MT5** antes de usar la app para datos en tiempo real

### 2. Dependencias Automáticas
Todas las dependencias ya están instaladas en el entorno virtual (`.venv`)

## 🎯 Cómo Usar

1. **Ejecuta la app** usando cualquiera de las opciones arriba
2. **Abre tu navegador** en: `http://localhost:8501`
3. **Configura** tus claves API si es necesario:
   - News API Key (ya configurada)
   - Telegram Bot Token (opcional)

## 📁 Archivos Importantes

- `smc_pro_app.py` - Código principal de la app
- `Start_SMC_App.bat` - **Ejecuta este archivo** (más fácil)
- `Start_SMC_App.ps1` - Script de PowerShell alternativo
- `.venv/` - Entorno virtual con todas las dependencias
- `news_cache.json` - Cache de noticias

## ⚠️ Notas Importantes

- **Primera ejecución**: Puede tardar unos segundos en cargar
- **MT5**: Si no tienes MT5 instalado, usará datos de yfinance (delay ~15min)
- **Firewall**: Puede pedir permisos de red para acceder a APIs
- **Actualizaciones**: La app se actualiza automáticamente cada 15 minutos

## 🆘 Solución de Problemas

### Error: "MetaTrader5 no disponible"
- Instala MT5 desde el sitio oficial
- Abre MT5 antes de ejecutar la app

### Error: "Puerto ocupado"
- Cierra otras instancias de Streamlit
- O cambia el puerto: `streamlit run smc_pro_app.py --server.port 8502`

### Error: "No se puede conectar a internet"
- Verifica tu conexión
- Las noticias requieren acceso a internet

## 📞 Soporte

Si tienes problemas, verifica:
1. Que todas las dependencias estén instaladas
2. Que tengas conexión a internet
3. Que MT5 esté abierto (opcional)

¡Disfruta tu SMC Pro App! ⚡📈