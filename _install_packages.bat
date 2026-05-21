@echo off
setlocal enabledelayedexpansion
chcp 65001 > nul

set "APPDIR=%~dp0"
set "LOG=%APPDIR%install_log.txt"

echo [%date% %time%] Iniciando instalacion de dependencias > "%LOG%"

:: ── 1. Detectar Python ──────────────────────────────────────────────────────
set "PYTHON_CMD="

py --version > nul 2>&1
if !errorlevel! == 0 set "PYTHON_CMD=py"

if "!PYTHON_CMD!"=="" (
    python --version > nul 2>&1
    if !errorlevel! == 0 set "PYTHON_CMD=python"
)

if "!PYTHON_CMD!"=="" (
    python3 --version > nul 2>&1
    if !errorlevel! == 0 set "PYTHON_CMD=python3"
)

:: Si Python no está, intentar instalar con winget
if "!PYTHON_CMD!"=="" (
    echo Python no encontrado. Intentando instalar Python 3.11 via winget... >> "%LOG%"
    winget install --id Python.Python.3.11 -e --silent --accept-package-agreements --accept-source-agreements >> "%LOG%" 2>&1

    :: Añadir rutas comunes de Python al PATH temporalmente
    for %%P in (
        "%LOCALAPPDATA%\Programs\Python\Python311"
        "%LOCALAPPDATA%\Programs\Python\Python312"
        "%LOCALAPPDATA%\Programs\Python\Python313"
        "C:\Python311"  "C:\Python312"
    ) do if exist "%%~P\python.exe" (
        set "PATH=%%~P\Scripts;%%~P;!PATH!"
    )

    py --version > nul 2>&1 && set "PYTHON_CMD=py"
    if "!PYTHON_CMD!"=="" (
        python --version > nul 2>&1 && set "PYTHON_CMD=python"
    )
)

if "!PYTHON_CMD!"=="" (
    echo ERROR: Python no pudo instalarse automaticamente. >> "%LOG%"
    powershell -Command "Add-Type -AssemblyName PresentationFramework; [System.Windows.MessageBox]::Show('Python no esta instalado.`n`nInstala Python 3.11+ desde:`nhttps://www.python.org/downloads/`n`nIMPORTANTE: marca la opcion `"Add Python to PATH`" durante la instalacion y luego ejecuta de nuevo el instalador.', 'SMC Pro App', 'OK', 'Error')"
    exit /b 1
)

for /f "tokens=*" %%v in ('!PYTHON_CMD! --version 2^>^&1') do (
    echo Python detectado: %%v >> "%LOG%"
)

:: ── 2. Crear entorno virtual ─────────────────────────────────────────────────
cd /d "%APPDIR%"

if not exist ".venv\Scripts\python.exe" (
    echo Creando entorno virtual .venv... >> "%LOG%"
    !PYTHON_CMD! -m venv .venv >> "%LOG%" 2>&1
)

if not exist ".venv\Scripts\python.exe" (
    echo ERROR: No se pudo crear el entorno virtual. >> "%LOG%"
    exit /b 1
)

:: ── 3. Actualizar pip ────────────────────────────────────────────────────────
echo Actualizando pip... >> "%LOG%"
".venv\Scripts\python.exe" -m pip install --upgrade pip --quiet >> "%LOG%" 2>&1

:: ── 4. Instalar dependencias ─────────────────────────────────────────────────
echo Instalando paquetes (esto puede tardar varios minutos)... >> "%LOG%"
".venv\Scripts\pip.exe" install -r requirements.txt >> "%LOG%" 2>&1
set "PIP_EXIT=!errorlevel!"

if !PIP_EXIT! == 0 (
    echo [%date% %time%] Instalacion completada con exito. >> "%LOG%"
    echo OK > "%APPDIR%install_status.txt"
) else (
    echo [%date% %time%] Advertencia: algunos paquetes no se instalaron. Revisa install_log.txt >> "%LOG%"
    echo PARTIAL > "%APPDIR%install_status.txt"
)

exit /b 0
