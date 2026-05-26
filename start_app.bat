@echo off
cd /d "%~dp0"
echo.
echo  ================================
echo   SMC Pro - MT5 Bridge Local
echo  ================================
echo  Servidor Railway: https://web-production-c5a95d.up.railway.app
echo  Visor local:      http://localhost:8501
echo.
echo  Abriendo navegador en 3 segundos...
timeout /t 3 /nobreak >nul
start "" "http://localhost:8501"
echo.
if exist ".venv\Scripts\streamlit.exe" (
    .venv\Scripts\streamlit.exe run local_viewer.py --server.port=8501 --server.headless=true
) else (
    python -m streamlit run local_viewer.py --server.port=8501 --server.headless=true
)
pause
