
# FinancieraNuevoProgresoApp

Aplicación **básica** de escritorio (Windows) para *Financiera Nuevo progreso* usando:
- Python 3.10+
- Tkinter + CustomTkinter
- SQLite (persistencia local)
- fpdf2 para generar recibos PDF
- Pillow para manejo de imagenes (logo/splash)
- PyInstaller para empaquetar a `.exe`

## Estructura
```
FinancieraNuevoProgresoApp/
├─ main.py
├─ db.py
├─ utils.py
├─ recibos.py
├─ config.py
├─ requirements.txt
├─ build.bat
├─ run.bat
└─ assets/
   └─ logo.txt   (placeholder, opcional)
```

La base de datos se guardará en:
`./financiera.db` (en la carpeta del proyecto)  
Los recibos PDF en:
`./reportes/`

## Uso (modo desarrollo)
1) Instala Python 3.10+
2) En consola (CMD) dentro de la carpeta del proyecto:
```
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
python main.py
```
> La app creará la base de datos y carpetas automáticamente si no existen.

## Ejecutar pruebas
Desde la raiz del proyecto:
```
python -m unittest discover -s tests -p "test_*.py"
```

## Calidad de codigo (opcional)
Instalar herramientas:
```
python -m pip install black ruff
```

Formatear:
```
python -m black .
```

Analizar lint:
```
python -m ruff check .
```

## Empaquetar a .exe (Windows)
Ejecuta:
```
build.bat
```
Esto:
- Crea/activa un entorno virtual `.venv`
- Instala dependencias
- Ejecuta PyInstaller con modo `--onefile --windowed` (sin consola)
- Genera `dist/FinancieraNuevoProgreso.exe`

## Notas
- El nombre de empresa en recibos y el título se fija a **"Financiera Nuevo progreso"**.
- Puedes ajustar colores y preferencias en `config.py`.
- Este proyecto es un **punto de partida**; puedes expandir módulos (morosos, liquidados, copias de seguridad, etc.).
- El proyecto incluye `.gitignore` para no versionar entorno virtual, DB local, PDFs y artefactos de build.
