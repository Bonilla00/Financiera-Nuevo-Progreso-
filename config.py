import os
import sys

APP_TITLE = "Financiera Nuevo Progreso"


def app_base_dir() -> str:
    """
    Carpeta de datos de la aplicación: junto al .exe (PyInstaller) o raíz del proyecto (desarrollo).
    No usar _MEIPASS: ahí solo van recursos embebidos de solo lectura.
    """
    if getattr(sys, "frozen", False):
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))

# Paleta alineada al logo (financiero moderno)
THEME = {
    "bg": "#0F172A",         # Fondo general
    "panel": "#111827",      # Tarjetas/paneles
    "panel_alt": "#1F2937",  # Panel secundario
    "text": "#E5E7EB",       # Texto principal
    "muted_text": "#94A3B8", # Texto secundario
    "accent": "#22D3EE",     # Cian principal
    "accent_alt": "#0891B2"  # Cian oscuro
}

# Tipografía preferida (si no está, se usa la del sistema)
FONT_FAMILY = "Segoe UI"  # o "Poppins"
