#!/usr/bin/env python3
"""
Launcher GUI para smc_pro_app.py

Funcionalidad:
- Muestra un formulario simple (tkinter) para pedir valores que normalmente
  se introducen a mano (API keys, credenciales MT5, opciones del bot).
- Guarda la configuración en `user_config.json` y arranca la app Streamlit
  usando el intérprete actual: `python -m streamlit run smc_pro_app.py`.

Uso para pruebas (sin arranque real):
  python launcher.py --test

Construir EXE (después de instalar dependencias):
  python -m pip install pyinstaller
  pyinstaller --onefile --noconsole launcher.py

El EXE resultante pedirá los datos y lanzará la app al hacer doble click.
"""
import os
import sys
import json
import subprocess
from pathlib import Path

try:
    import tkinter as tk
    from tkinter import ttk, messagebox
except Exception:
    tk = None

ROOT = Path(__file__).resolve().parent
CONFIG_FILE = ROOT / "user_config.json"


DEFAULTS = {
    "NEWS_API_KEY": "",
    "MT5_LOGIN": "",
    "MT5_PASSWORD": "",
    "MT5_SERVER": "",
    "BOT_ENABLED": False,
    "BOT_VOLUME": 0.01,
}


def save_config(cfg: dict):
    try:
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=2)
    except Exception as e:
        print("Error saving config:", e)


def start_streamlit_with_env(cfg: dict, test=False):
    env = os.environ.copy()
    # Export only the keys relevant for smc_pro_app
    for k in ["NEWS_API_KEY", "MT5_LOGIN", "MT5_PASSWORD", "MT5_SERVER"]:
        if cfg.get(k):
            env[k] = str(cfg[k])

    # Bot options
    env["BOT_ENABLED"] = str(bool(cfg.get("BOT_ENABLED", False)))
    env["BOT_VOLUME"] = str(cfg.get("BOT_VOLUME", 0.01))

    cmd = [sys.executable, "-m", "streamlit", "run", "smc_pro_app.py", "--server.headless", "true"]
    print("Starting Streamlit with command:", " ".join(cmd))
    if test:
        print("Test mode: not launching process")
        return

    # Launch in the project root so relative paths work
    subprocess.Popen(cmd, env=env, cwd=str(ROOT))


def build_gui_and_run():
    if tk is None:
        print("tkinter no disponible. Ejecuta en modo --test o instala tkinter.")
        return

    root = tk.Tk()
    root.title("SMC Tool - Launcher")

    frm = ttk.Frame(root, padding=12)
    frm.grid(row=0, column=0, sticky=(tk.N, tk.S, tk.E, tk.W))

    entries = {}

    def add_row(label, key, row, width=40):
        ttk.Label(frm, text=label).grid(column=0, row=row, sticky=tk.W, pady=4)
        e = ttk.Entry(frm, width=width)
        e.grid(column=1, row=row, sticky=(tk.W, tk.E), pady=4)
        e.insert(0, DEFAULTS.get(key, ""))
        entries[key] = e

    add_row("News API Key:", "NEWS_API_KEY", 0)
    add_row("MT5 login (opcional):", "MT5_LOGIN", 1)
    add_row("MT5 password (opcional):", "MT5_PASSWORD", 2)
    add_row("MT5 server (opcional):", "MT5_SERVER", 3)

    # Bot enabled checkbox
    bot_var = tk.BooleanVar(value=DEFAULTS.get("BOT_ENABLED", False))
    ttk.Checkbutton(frm, text="Habilitar bot automático", variable=bot_var).grid(column=0, row=4, columnspan=2, sticky=tk.W, pady=6)

    ttk.Label(frm, text="Bot volume (lotes):").grid(column=0, row=5, sticky=tk.W)
    vol_entry = ttk.Entry(frm, width=10)
    vol_entry.grid(column=1, row=5, sticky=tk.W)
    vol_entry.insert(0, str(DEFAULTS.get("BOT_VOLUME", 0.01)))

    def on_start():
        cfg = {
            "NEWS_API_KEY": entries["NEWS_API_KEY"].get().strip(),
            "MT5_LOGIN": entries["MT5_LOGIN"].get().strip(),
            "MT5_PASSWORD": entries["MT5_PASSWORD"].get().strip(),
            "MT5_SERVER": entries["MT5_SERVER"].get().strip(),
            "BOT_ENABLED": bool(bot_var.get()),
            "BOT_VOLUME": float(vol_entry.get() or 0.01),
        }
        save_config(cfg)
        try:
            start_streamlit_with_env(cfg, test=False)
        except Exception as e:
            messagebox.showerror("Error", f"No se pudo iniciar la app: {e}")
            return
        messagebox.showinfo("Iniciado", "La aplicación se ha iniciado en http://localhost:8501")

    btn = ttk.Button(frm, text="Iniciar SMC App", command=on_start)
    btn.grid(column=0, row=6, columnspan=2, pady=12)

    for child in frm.winfo_children():
        child.grid_configure(padx=6)

    root.mainloop()


def main():
    if "--test" in sys.argv:
        print("Modo prueba: validar launcher sin lanzar Streamlit")
        cfg = DEFAULTS.copy()
        save_config(cfg)
        start_streamlit_with_env(cfg, test=True)
        return

    if "--auto" in sys.argv:
        # Modo no-GUI para pruebas: guarda defaults y simula arranque
        cfg = DEFAULTS.copy()
        save_config(cfg)
        start_streamlit_with_env(cfg, test=True)
        return

    build_gui_and_run()


if __name__ == "__main__":
    main()
