@echo off
cd /d "%~dp0"
echo Iniciando SMC Pro App...
echo Abre tu navegador en: http://localhost:8501
echo.
if exist ".venv\Scripts\streamlit.exe" (
    .venv\Scripts\streamlit.exe run smc_pro_app.py
) else (
    python -m streamlit run smc_pro_app.py
)
pause