
# FinancieraNuevoProgresoApp

Aplicación financiera moderna con soporte para escritorio (Windows) y web (PWA), migrada completamente a **PostgreSQL**.

## Tecnologías
- Python 3.10+
- Tkinter + CustomTkinter (Escritorio)
- Flask (Web/PWA)
- PostgreSQL (Persistencia centralizada)
- fpdf2 para generar recibos PDF
- Pillow para manejo de imágenes
- PyInstaller para empaquetar a `.exe`

## Estructura
```
FinancieraNuevoProgresoApp/
├─ main.py       (App de escritorio)
├─ app.py        (App web Flask)
├─ db.py         (Capa de datos PostgreSQL)
├─ utils.py      (Utilidades comunes)
├─ recibos.py    (Generación de PDFs)
├─ config.py     (Configuración de temas y rutas)
├─ requirements.txt
├─ build.bat
└─ assets/       (Logos y recursos)
```

## Configuración Obligatoria
El proyecto requiere una base de datos PostgreSQL. Debes configurar la variable de entorno:
`DATABASE_URL=postgresql://usuario:password@host:puerto/nombre_db`

## Uso (modo desarrollo)
1) Instala Python 3.10+
2) En consola (CMD) dentro de la carpeta del proyecto:
```
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
python main.py
```
> Al iniciar por primera vez, si no existen usuarios, la app creará automáticamente un usuario:
> - **Usuario:** `admin`
> - **Clave:** `admin123`

## Copias de Seguridad
El sistema utiliza volcados SQL estándar:
- **Exportar:** Genera un archivo `.sql` con toda la estructura y datos.
- **Importar:** Restaura la base de datos desde un archivo `.sql` (sobrescribe datos actuales).
Las copias se almacenan por defecto en la carpeta `./backups/`.

## Empaquetar a .exe (Windows)
Ejecuta `build.bat`. Esto generará `dist/FinancieraNuevoProgreso.exe`. Asegúrate de que el entorno donde se ejecute el `.exe` tenga acceso a la base de datos PostgreSQL configurada.
