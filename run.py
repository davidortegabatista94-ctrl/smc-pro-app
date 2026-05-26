"""
run.py — Entrypoint principal para Railway.

Arranca el background worker (análisis 24/7) en este proceso,
luego lanza Streamlit como subproceso hijo. Así el worker
corre independientemente de si hay usuarios conectados o no.
"""

import logging
import os
import subprocess
import sys

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    datefmt="%H:%M:%S",
)
_log = logging.getLogger("run")

# ── Arranca el background worker en este proceso ─────────────────────────────
try:
    import background_worker as _bgw
    _bgw.start_if_needed()
    _log.info("Background worker iniciado OK")
except Exception as _e:
    _log.warning("Background worker no disponible: %s", _e)

# ── Lanza Streamlit como subproceso (bloqueante) ─────────────────────────────
_port = os.environ.get("PORT", "8080")
_cmd = [
    sys.executable, "-m", "streamlit", "run", "smc_pro_app.py",
    f"--server.port={_port}",
    "--server.address=0.0.0.0",
    "--server.headless=true",
    "--server.enableCORS=false",
    "--server.enableXsrfProtection=false",
]
_log.info("Arrancando Streamlit en puerto %s …", _port)

try:
    _proc = subprocess.Popen(_cmd)
    _proc.wait()
except KeyboardInterrupt:
    _log.info("Interrupción manual — cerrando")
    _proc.terminate()
except Exception as _e:
    _log.error("Error arrancando Streamlit: %s", _e)
    sys.exit(1)
