@echo off
cd /d "%~dp0"
title SMC Pro App

echo  ================================================
echo     SMC Pro App - Iniciando...
echo  ================================================
echo.

if exist ".venv\Scripts\streamlit.exe" (
    start "SMC Pro App - Servidor" cmd /k ".venv\Scripts\streamlit.exe run smc_pro_app.py"
) else (
    start "SMC Pro App - Servidor" cmd /k "python -m streamlit run smc_pro_app.py"
)

echo  Abriendo navegador en 5 segundos...
timeout /t 5 /nobreak > nul
start "" "http://localhost:8501"
echo.
echo  La aplicacion esta corriendo en http://localhost:8501
echo  Cierra la ventana "SMC Pro App - Servidor" para detenerla.
echo.
pause
