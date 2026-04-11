import os
import logging
from logging.handlers import RotatingFileHandler
import tkinter as tk
from tkinter import ttk, messagebox, simpledialog, filedialog
from datetime import datetime, timedelta
from PIL import Image
import customtkinter as ctk

from config import APP_TITLE, THEME, FONT_FAMILY, app_base_dir
from utils import fmt_money, today_str, add_days
import db
import backup_db
from recibos import generar_recibo

AUTO_BACKUP_MAX_ARCHIVOS = 30

# Escritorio: mismo alcance que listar_prestamos() por defecto (admin = sin filtro por owner).
_DESKTOP_USER_ID = 0
_DESKTOP_IS_ADMIN = True
# Clientes nuevos desde el escritorio se asocian al usuario dueño indicado (típicamente id=1 admin).
_DESKTOP_OWNER_USER_ID = 1


def setup_logging() -> None:
    """
    Configura logging global de la app:
    - Archivo rotativo en logs/app.log
    - Salida por consola para desarrollo
    """
    root_logger = logging.getLogger()
    if root_logger.handlers:
        return

    logs_dir = os.path.join(app_base_dir(), "logs")
    os.makedirs(logs_dir, exist_ok=True)
    log_file = os.path.join(logs_dir, "app.log")

    formatter = logging.Formatter(
        "%(asctime)s | %(levelname)s | %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    file_handler = RotatingFileHandler(
        log_file,
        maxBytes=1_000_000,
        backupCount=5,
        encoding="utf-8",
    )
    file_handler.setLevel(logging.INFO)
    file_handler.setFormatter(formatter)

    console_handler = logging.StreamHandler()
    console_handler.setLevel(logging.INFO)
    console_handler.setFormatter(formatter)

    root_logger.setLevel(logging.INFO)
    root_logger.addHandler(file_handler)
    root_logger.addHandler(console_handler)


def fecha_humana_es(fecha: datetime | None = None) -> str:
    if fecha is None:
        fecha = datetime.now()
    dias = [
        "lunes",
        "martes",
        "miercoles",
        "jueves",
        "viernes",
        "sabado",
        "domingo",
    ]
    meses = [
        "enero",
        "febrero",
        "marzo",
        "abril",
        "mayo",
        "junio",
        "julio",
        "agosto",
        "septiembre",
        "octubre",
        "noviembre",
        "diciembre",
    ]
    return f"{dias[fecha.weekday()]} {fecha.day} de {meses[fecha.month - 1]} de {fecha.year}"


def _parse_fecha_iso(val) -> datetime | None:
    if val is None or str(val).strip() == "":
        return None
    try:
        return datetime.strptime(str(val).strip()[:10], "%Y-%m-%d")
    except ValueError:
        return None


def prestamo_en_mora_desde_campos(estado: str, vencimiento, proximo_pago, hoy: datetime) -> bool:
    """Préstamo ACTIVO y próximo_pago (o vencimiento si falta) ya pasó."""
    if estado != "ACTIVO":
        return False
    prox = _parse_fecha_iso(proximo_pago)
    venc = _parse_fecha_iso(vencimiento)
    ref = prox if prox is not None else venc
    if ref is None:
        return False
    return ref < hoy


def prestamo_en_mora(row: tuple, hoy: datetime) -> bool:
    """
    Fila de db.listar_prestamos(): índices 8=vencimiento, 9=estado, 13=proximo_pago.
    """
    if len(row) < 10:
        return False
    return prestamo_en_mora_desde_campos(
        row[9],
        row[8],
        row[13] if len(row) > 13 else None,
        hoy,
    )


# ---------- helpers de estilo ----------
def setup_ctk(app: ctk.CTk):
    ctk.set_appearance_mode("dark")
    ctk.set_default_color_theme("dark-blue")

    THEME.update({
        "bg": "#0F172A",
        "panel": "#111827",
        "panel_alt": "#1F2937",
        "text": "#E5E7EB",
        "muted_text": "#94A3B8",
        "accent": "#22D3EE",
        "accent_alt": "#0891B2",
    })

    app.configure(fg_color=THEME["bg"])

    def styled_button(master, text, command=None, width=140):
        return ctk.CTkButton(
            master,
            text=text,
            command=command,
            fg_color=THEME["accent"],
            hover_color=THEME["accent_alt"],
            text_color="#0B1120",
            font=(FONT_FAMILY, 12, "bold"),
            corner_radius=12,
            border_width=1,
            border_color="#67E8F9",
            height=38,
            width=width
        )
    app.make_button = styled_button


# ---------- Cards ----------
class Card(ctk.CTkFrame):
    def __init__(self, master, title: str, value: str = "0", color=None, **kwargs):
        super().__init__(
            master,
            fg_color=color or THEME["panel"],
            corner_radius=16,
            border_width=1,
            border_color="#253042",
            **kwargs,
        )
        self.grid_columnconfigure(0, weight=1)
        self.title_lbl = ctk.CTkLabel(self, text=title, font=(FONT_FAMILY, 13, "bold"), text_color=THEME["muted_text"])
        self.title_lbl.grid(row=0, column=0, sticky="w", padx=12, pady=(10, 0))
        self.value_lbl = ctk.CTkLabel(self, text=value, font=(FONT_FAMILY, 28, "bold"), text_color=THEME["accent"])
        self.value_lbl.grid(row=1, column=0, sticky="w", padx=12, pady=(2, 10))


# ---------- Splash Screen (moderna con barra animada) ----------
class SplashScreen(ctk.CTk):
    def __init__(self, logo_path: str, main_app_callback):
        super().__init__()
        self.title("Financiera Nuevo Progreso")
        self.geometry("640x420")
        self.configure(fg_color=THEME.get("bg", "#0F172A"))
        self.overrideredirect(True)

        # Centrar
        self.update_idletasks()
        width, height = 640, 420
        x = (self.winfo_screenwidth() // 2) - (width // 2)
        y = (self.winfo_screenheight() // 2) - (height // 2)
        self.geometry(f"{width}x{height}+{x}+{y}")

        # Logo
        if os.path.exists(logo_path):
            try:
                img = Image.open(logo_path)
                logo_ctk = ctk.CTkImage(light_image=img, dark_image=img, size=(180, 180))
                lbl_logo = ctk.CTkLabel(self, image=logo_ctk, text="")
                lbl_logo.image = logo_ctk
                lbl_logo.pack(pady=(50, 12))
            except Exception as e:
                print("Error cargando logo splash:", e)

        # Título + subtítulo
        ctk.CTkLabel(self, text="Bienvenido a Financiera Nuevo Progreso",
                     font=(FONT_FAMILY, 22, "bold"), text_color=THEME.get("accent", "#22D3EE")).pack(pady=(0, 6))
        ctk.CTkLabel(self, text="Cargando sistema...", font=(FONT_FAMILY, 14), text_color=THEME.get("text", "#E5E7EB")).pack(pady=(0, 18))

        # Barra de progreso
        self.progress = ctk.CTkProgressBar(self, width=460, height=16,
                                           progress_color=THEME.get("accent", "#22D3EE"),
                                           fg_color=THEME.get("panel_alt", "#1F2937"),
                                           corner_radius=10)
        self.progress.pack(pady=(0, 10))
        self.progress.set(0)

        self._step = 0
        self._main_app_callback = main_app_callback
        self.after(40, self._animate)

    def _animate(self):
        self._step += 1
        val = min(1.0, self._step / 50.0)  # ~2s
        self.progress.set(val)
        if val < 1.0:
            self.after(40, self._animate)
        else:
            self.destroy()
            self._main_app_callback()


# ---------- Aplicación principal ----------
def abrir_observaciones(app_self):
    # Obtener selección
    sel = app_self.tree.selection()
    if not sel:
        return
    vals = app_self.tree.item(sel[0])["values"]
    pid = int(vals[-1])
    nombre = str(vals[1]).strip()
    monto_fmt = vals[3]

    info = db.obtener_prestamo(pid, _DESKTOP_USER_ID, _DESKTOP_IS_ADMIN)
    nota_actual = (str(info[16]).strip() if info and info[16] is not None else "") or ""

    # Ventana modal
    top = ctk.CTkToplevel(app_self)
    top.title(f"Observaciones - {nombre}")
    w, h = 600, 400
    x = (top.winfo_screenwidth() // 2) - (w // 2)
    y = (top.winfo_screenheight() // 2) - (h // 2)
    top.geometry(f"{w}x{h}+{x}+{y}")
    top.grab_set()

    frm = ctk.CTkFrame(top, fg_color=THEME["panel"], corner_radius=14, border_width=1, border_color="#253042")
    frm.pack(fill="both", expand=True, padx=12, pady=12)
    frm.grid_columnconfigure(0, weight=1)

    lbl_nombre = ctk.CTkLabel(frm, text=nombre, font=(FONT_FAMILY, 18, "bold"), text_color=THEME["text"])
    lbl_nombre.grid(row=0, column=0, pady=(6, 0), sticky="n")
    lbl_monto = ctk.CTkLabel(frm, text=f"Monto del prestamo: {monto_fmt}", font=(FONT_FAMILY, 14), text_color=THEME["text"])
    lbl_monto.grid(row=1, column=0, pady=(2, 8), sticky="n")

    txt = ctk.CTkTextbox(frm, width=560, height=220)
    txt.grid(row=2, column=0, padx=6, pady=6, sticky="nsew")
    txt.insert("1.0", nota_actual)

    confirm = ctk.CTkLabel(frm, text="", text_color="#78e08f", font=(FONT_FAMILY, 12))
    confirm.grid(row=4, column=0, pady=(4, 4), sticky="n")

    def guardar_y_cerrar():
        nota = txt.get("1.0", "end").strip()
        db.actualizar_nota_prestamo(pid, nota, _DESKTOP_USER_ID, _DESKTOP_IS_ADMIN)
        confirm.configure(text="✅ Nota guardada correctamente")
        # refresca lista para mostrar/ocultar el indicador
        app_self.refresh_prestamos()
        # desaparece mensaje y cerrar
        app_self.after(3000, lambda: confirm.configure(text=""))
        top.after(3100, top.destroy)

    app_self.make_button(frm, "Guardar y cerrar", guardar_y_cerrar, width=180).grid(row=3, column=0, pady=(8,0), sticky="n")


class App(ctk.CTk):
    def __init__(self):
        super().__init__()
        db.ensure_schema_migrations()

        self.title(APP_TITLE)
        self.geometry("1200x750")
        self.minsize(1000, 640)
        setup_ctk(self)

        # Estilo Treeview
        style = ttk.Style(self)
        style.theme_use("clam")
        style.configure("Treeview",
                        background=THEME["panel"],
                        fieldbackground=THEME["panel"],
                        foreground=THEME["text"],
                        rowheight=32,
                        borderwidth=0,
                        font=(FONT_FAMILY, 10))
        style.configure("Treeview.Heading",
                        background=THEME["panel_alt"],
                        foreground=THEME["text"],
                        relief="flat",
                        padding=(8, 6),
                        font=(FONT_FAMILY, 10, "bold"))
        style.map("Treeview", background=[("selected", THEME["accent_alt"])], foreground=[("selected", "#FFFFFF")])

        # Tabla de Préstamos: lectura más clara (zebra, cabecera, scroll oscuro)
        style.configure(
            "Prestamos.Treeview",
            background=THEME["panel"],
            fieldbackground=THEME["panel"],
            foreground=THEME["text"],
            rowheight=36,
            borderwidth=0,
            font=(FONT_FAMILY, 11),
        )
        style.configure(
            "Prestamos.Treeview.Heading",
            background="#1E293B",
            foreground=THEME["text"],
            relief="flat",
            borderwidth=0,
            font=(FONT_FAMILY, 11, "bold"),
            padding=(10, 10),
        )
        style.map(
            "Prestamos.Treeview.Heading",
            background=[("active", "#243246")],
        )
        style.map(
            "Prestamos.Treeview",
            background=[("selected", THEME["accent_alt"])],
            foreground=[("selected", "#FFFFFF")],
        )
        style.layout("Prestamos.Treeview", style.layout("Treeview"))
        style.layout("Prestamos.Treeview.Heading", style.layout("Treeview.Heading"))
        style.configure(
            "Prestamos.Vertical.TScrollbar",
            troughcolor="#0F172A",
            background="#334155",
            darkcolor="#334155",
            lightcolor="#334155",
            bordercolor="#253042",
            arrowcolor=THEME["muted_text"],
        )
        style.map(
            "Prestamos.Vertical.TScrollbar",
            background=[("active", THEME["accent_alt"]), ("pressed", THEME["accent"])],
        )

        style.configure(
            "Pagos.Treeview",
            background=THEME["panel"],
            fieldbackground=THEME["panel"],
            foreground=THEME["text"],
            rowheight=36,
            borderwidth=0,
            font=(FONT_FAMILY, 11),
        )
        style.configure(
            "Pagos.Treeview.Heading",
            background="#1E293B",
            foreground=THEME["text"],
            relief="flat",
            borderwidth=0,
            font=(FONT_FAMILY, 11, "bold"),
            padding=(10, 10),
        )
        style.map(
            "Pagos.Treeview.Heading",
            background=[("active", "#243246")],
        )
        style.map(
            "Pagos.Treeview",
            background=[("selected", THEME["accent_alt"])],
            foreground=[("selected", "#FFFFFF")],
        )
        style.layout("Pagos.Treeview", style.layout("Treeview"))
        style.layout("Pagos.Treeview.Heading", style.layout("Treeview.Heading"))
        style.configure(
            "Pagos.Vertical.TScrollbar",
            troughcolor="#0F172A",
            background="#334155",
            darkcolor="#334155",
            lightcolor="#334155",
            bordercolor="#253042",
            arrowcolor=THEME["muted_text"],
        )
        style.map(
            "Pagos.Vertical.TScrollbar",
            background=[("active", THEME["accent_alt"]), ("pressed", THEME["accent"])],
        )

        container = ctk.CTkFrame(self, fg_color="transparent")
        container.pack(fill="both", expand=True, padx=16, pady=16)
        container.grid_rowconfigure(1, weight=1)
        container.grid_columnconfigure(0, weight=1)

        topbar = ctk.CTkFrame(
            container,
            fg_color=THEME["panel"],
            corner_radius=14,
            border_width=1,
            border_color="#253042",
            height=58,
        )
        topbar.grid(row=0, column=0, sticky="ew", pady=(0, 12))
        topbar.grid_columnconfigure(0, weight=1)
        topbar.grid_columnconfigure(1, weight=1)
        topbar.grid_columnconfigure(2, weight=1)

        ctk.CTkLabel(
            topbar,
            text="Financiera Nuevo Progreso",
            font=(FONT_FAMILY, 16, "bold"),
            text_color=THEME["text"],
        ).grid(row=0, column=0, sticky="w", padx=16, pady=12)

        self.lbl_hoy = ctk.CTkLabel(
            topbar,
            text=f"Hoy: {fecha_humana_es()}",
            font=(FONT_FAMILY, 12),
            text_color=THEME["muted_text"],
        )
        self.lbl_hoy.grid(row=0, column=1, pady=12)

        self.lbl_estado = ctk.CTkLabel(
            topbar,
            text="Sistema listo",
            font=(FONT_FAMILY, 12, "bold"),
            text_color=THEME["accent"],
        )
        self.lbl_estado.grid(row=0, column=2, sticky="e", padx=16, pady=12)

        self.tabs = ctk.CTkTabview(
            container,
            fg_color=THEME["panel_alt"],
            segmented_button_fg_color=THEME["panel"],
            segmented_button_selected_color=THEME["accent_alt"],
            segmented_button_selected_hover_color=THEME["accent"],
            segmented_button_unselected_color=THEME["panel"],
            segmented_button_unselected_hover_color="#243244",
            text_color=THEME["text"],
        )
        self.tabs.grid(row=1, column=0, sticky="nsew")

        self.tab_dashboard = self.tabs.add("Inicio")
        self.tab_nuevo = self.tabs.add("Nuevo")
        self.tab_prestamos = self.tabs.add("Prestamos")
        self.tab_pagos = self.tabs.add("Pagos")
        self.tab_respaldo = self.tabs.add("Respaldo")

        self.build_dashboard(self.tab_dashboard)
        self.build_nuevo(self.tab_nuevo)
        self.build_prestamos(self.tab_prestamos)
        self.build_pagos(self.tab_pagos)
        self.build_respaldo(self.tab_respaldo)

        self.protocol("WM_DELETE_WINDOW", self._on_close_app)
        self.after(1200, self._auto_backup_inicio)

    def _auto_backup_inicio(self):
        """
        Crea un backup automático solo una vez por día y aplica retención.
        Se ejecuta en silencio para no interrumpir el flujo del usuario.
        """
        try:
            if not backup_db.existe_backup_automatico_hoy():
                backup_db.hacer_backup_automatico("auto")
            backup_db.limpiar_backups(AUTO_BACKUP_MAX_ARCHIVOS)
        except Exception as e:
            print("Auto-backup inicio:", e)

    def _on_close_app(self):
        """
        Antes de cerrar: backup automático y retención.
        Si falla, se cierra igualmente.
        """
        try:
            backup_db.hacer_backup_automatico("close")
            backup_db.limpiar_backups(AUTO_BACKUP_MAX_ARCHIVOS)
        except Exception as e:
            print("Auto-backup cierre:", e)
        finally:
            self.destroy()

    # ---------- RESPALDO ----------
    def build_respaldo(self, parent):
        parent.grid_columnconfigure(0, weight=1)
        parent.configure(fg_color=THEME["bg"])

        wrap = ctk.CTkFrame(parent, fg_color="transparent")
        wrap.grid(row=0, column=0, sticky="nsew", padx=24, pady=24)
        wrap.grid_columnconfigure(0, weight=1)

        card = ctk.CTkFrame(
            wrap,
            fg_color=THEME["panel"],
            corner_radius=16,
            border_width=1,
            border_color="#253042",
        )
        card.grid(row=0, column=0, sticky="ew", pady=(0, 16))
        card.grid_columnconfigure(0, weight=1)

        ctk.CTkLabel(
            card,
            text="Copia de seguridad",
            font=(FONT_FAMILY, 20, "bold"),
            text_color=THEME["accent"],
            anchor="w",
        ).grid(row=0, column=0, sticky="w", padx=22, pady=(22, 6))

        ctk.CTkLabel(
            card,
            text="Guarda o recupera el archivo de la base de datos (financiera.db). "
            "Las copias se guardan en la carpeta backups junto al programa.",
            font=(FONT_FAMILY, 12),
            text_color=THEME["muted_text"],
            wraplength=720,
            justify="left",
            anchor="w",
        ).grid(row=1, column=0, sticky="ew", padx=22, pady=(0, 16))

        lbl_ruta = ctk.CTkLabel(
            card,
            text="",
            font=(FONT_FAMILY, 11),
            text_color=THEME["muted_text"],
            wraplength=720,
            justify="left",
            anchor="w",
        )
        lbl_ruta.grid(row=2, column=0, sticky="ew", padx=22, pady=(0, 18))

        def actualizar_texto_rutas():
            base = app_base_dir()
            carpeta = backup_db.backups_dir()
            lbl_ruta.configure(
                text=f"Base actual: {os.path.join(base, 'financiera.db')}\n"
                f"Carpeta de copias: {carpeta}"
            )

        actualizar_texto_rutas()

        btns = ctk.CTkFrame(card, fg_color="transparent")
        btns.grid(row=3, column=0, sticky="w", padx=18, pady=(0, 22))
        self.make_button(btns, "Hacer backup", self._accion_hacer_backup, width=180).pack(side="left", padx=6)
        self.make_button(btns, "Restaurar backup", self._accion_restaurar_backup, width=180).pack(side="left", padx=6)

    def _accion_hacer_backup(self):
        try:
            path = backup_db.hacer_backup()
            messagebox.showinfo("Backup correcto", f"Se guardó la copia en:\n{path}")
        except Exception as e:
            messagebox.showerror("Error al respaldar", str(e))

    def _accion_restaurar_backup(self):
        carpeta = backup_db.backups_dir()
        path = filedialog.askopenfilename(
            parent=self,
            title="Seleccionar copia de seguridad (.db)",
            initialdir=carpeta,
            filetypes=[("Base de datos SQLite", "*.db"), ("Todos los archivos", "*.*")],
        )
        if not path:
            return
        if not messagebox.askyesno(
            "Confirmar restauración",
            "Se reemplazará la base de datos actual por la copia seleccionada.\n"
            "Esta acción no se puede deshacer.\n\n¿Deseas continuar?",
        ):
            return
        try:
            backup_db.restaurar_desde(path)
            messagebox.showinfo(
                "Restauración completada",
                "La base de datos se restauró. Si algo no coincide con lo que ves en pantalla, "
                "cierra y vuelve a abrir la aplicación.",
            )
            db.ensure_schema_migrations()
            self.refresh_dashboard()
            self.refresh_prestamos()
            self.refresh_pagos()
        except Exception as e:
            messagebox.showerror("Error al restaurar", str(e))

    # ---------- DASHBOARD ----------
    def build_dashboard(self, parent):
        parent.grid_columnconfigure(0, weight=1)
        parent.configure(fg_color=THEME["bg"])

        # LOGO + Título centrados
        header = ctk.CTkFrame(parent, fg_color=THEME["bg"])
        header.grid(row=0, column=0, sticky="nsew", pady=(16, 8))
        header.grid_columnconfigure(0, weight=1)

        logo_path = os.path.join(os.path.dirname(__file__), "assets", "logo_dark.png")
        if not os.path.exists(logo_path):
            logo_path = os.path.join(os.path.dirname(__file__), "assets", "logo.png")
        if os.path.exists(logo_path):
            try:
                img = Image.open(logo_path)
                logo_ctk = ctk.CTkImage(light_image=img, dark_image=img, size=(180, 180))
                lbl_logo = ctk.CTkLabel(header, image=logo_ctk, text="")
                lbl_logo.image = logo_ctk
                lbl_logo.grid(row=0, column=0, pady=(0, 6))
            except Exception as e:
                print("Error cargando logo:", e)
        ctk.CTkLabel(
            header,
            text="Panel de control",
            font=(FONT_FAMILY, 18, "bold"),
            text_color=THEME["text"],
        ).grid(row=1, column=0, pady=(0, 8))

        # Tarjetas fila superior
        cards = ctk.CTkFrame(parent, fg_color="transparent")
        cards.grid(row=1, column=0, sticky="ew", padx=20, pady=(0, 10))
        for i in range(4):
            cards.grid_columnconfigure(i, weight=1, uniform="cards")

        self.lbl_total = Card(cards, "Total prestamos")
        self.lbl_activos = Card(cards, "Activos")
        self.lbl_pagados = Card(cards, "Pagados")
        self.lbl_mora = Card(cards, "En mora")

        self.lbl_total.grid(row=0, column=0, padx=8, pady=8, sticky="nsew")
        self.lbl_activos.grid(row=0, column=1, padx=8, pady=8, sticky="nsew")
        self.lbl_pagados.grid(row=0, column=2, padx=8, pady=8, sticky="nsew")
        self.lbl_mora.grid(row=0, column=3, padx=8, pady=8, sticky="nsew")

        # Fila inferior: Invertido / Recuperado / Intereses
        subcards = ctk.CTkFrame(parent, fg_color="transparent")
        subcards.grid(row=2, column=0, sticky="ew", padx=20, pady=(0, 30))
        subcards.grid_columnconfigure(0, weight=1)
        subcards.grid_columnconfigure(1, weight=1)
        subcards.grid_columnconfigure(2, weight=1)

        self.lbl_invertido = Card(subcards, "Invertido", "$0", color=THEME["panel"])
        self.lbl_recuperado = Card(subcards, "Recuperado", "$0", color=THEME["panel"])
        self.lbl_ganado = Card(subcards, "Intereses ganados", "$0", color=THEME["panel"])

        self.lbl_invertido.grid(row=0, column=0, padx=8, pady=8, sticky="nsew")
        self.lbl_recuperado.grid(row=0, column=1, padx=8, pady=8, sticky="nsew")
        self.lbl_ganado.grid(row=0, column=2, padx=8, pady=8, sticky="nsew")

        self.refresh_dashboard()

    def refresh_dashboard(self):
        prs = db.listar_prestamos()
        total = len(prs)
        activos = sum(1 for p in prs if p[9] == "ACTIVO")
        pagados = sum(1 for p in prs if p[9] == "PAGADO")
        hoy = datetime.strptime(today_str(), "%Y-%m-%d")
        mora = sum(1 for p in prs if prestamo_en_mora(p, hoy))
        invertido = db.sum_montos_por_rango(
            "0000-01-01", today_str(), _DESKTOP_USER_ID, _DESKTOP_IS_ADMIN
        )
        cobrado = db.sum_pagos_por_rango(
            "0000-01-01", today_str(), _DESKTOP_USER_ID, _DESKTOP_IS_ADMIN
        )
        recuperado = cobrado
        intereses = cobrado - invertido
        if intereses < 0:
            intereses = 0

        self.lbl_total.value_lbl.configure(text=str(total))
        self.lbl_activos.value_lbl.configure(text=str(activos))
        self.lbl_pagados.value_lbl.configure(text=str(pagados))
        self.lbl_mora.value_lbl.configure(text=str(mora))
        self.lbl_invertido.value_lbl.configure(text=fmt_money(invertido))
        self.lbl_recuperado.value_lbl.configure(text=fmt_money(recuperado))
        self.lbl_ganado.value_lbl.configure(text=fmt_money(intereses))
        self.lbl_hoy.configure(text=f"Hoy: {fecha_humana_es()}")
        self.lbl_estado.configure(text=f"{activos} activos | {pagados} pagados | {mora} en mora")

    # ---------- NUEVO PRÉSTAMO ----------
    def build_nuevo(self, parent):
        parent.grid_columnconfigure(0, weight=1)
        frm = ctk.CTkFrame(parent, fg_color=THEME["panel"], corner_radius=14, border_width=1, border_color="#253042")
        frm.grid(row=0, column=0, sticky="nsew", padx=12, pady=12)
        frm.grid_columnconfigure(1, weight=1)
        self.entries = {}

        def row(idx, label, var, default=""):
            ctk.CTkLabel(frm, text=label, font=(FONT_FAMILY, 12), text_color=THEME["text"]).grid(row=idx, column=0, sticky="w", padx=8, pady=6)
            e = ctk.CTkEntry(frm, fg_color=THEME["panel_alt"], border_color="#314158")
            e.grid(row=idx, column=1, sticky="ew", padx=8, pady=6)
            if default:
                e.insert(0, default)
            self.entries[var] = e

        row(0, "Nombre:", "nombre")
        row(1, "Identificación:", "ident")
        row(2, "Teléfono:", "tel")
        row(3, "Barrio:", "barrio")
        row(4, "Dirección:", "dir")
        row(5, "Fecha:", "fecha", today_str())

        # Frecuencia modificada
        ctk.CTkLabel(frm, text="Frecuencia:", text_color=THEME["text"]).grid(
            row=6, column=0, sticky="w", padx=8, pady=6
        )
        self.cmb_freq = ctk.CTkComboBox(
            frm,
            width=180,
            values=["diaria", "semanal", "quincenal", "mensual"]
        )
        self.cmb_freq.set("mensual")
        self.cmb_freq.grid(
            row=6, column=1, sticky="w", padx=8, pady=6
        )

        row(7, "Cuotas:", "cuotas")
        row(8, "Monto:", "monto")
        row(9, "Tasa % (sobre monto):", "tasa")

        self.lbl_res = ctk.CTkLabel(frm, text="Interes: $0 | Total: $0 | Cuota: $0",
                                    font=(FONT_FAMILY, 12, "bold"), text_color=THEME["accent"])
        self.lbl_res.grid(row=10, column=0, columnspan=2, sticky="w", padx=8, pady=(6, 8))

        btns = ctk.CTkFrame(frm, fg_color="transparent")
        btns.grid(row=11, column=0, columnspan=2, sticky="w", padx=6, pady=6)
        self.make_button(btns, "Calcular", self.calcular).grid(row=0, column=0, padx=6)
        self.make_button(btns, "Guardar", self.guardar).grid(row=0, column=1, padx=6)
        self.make_button(btns, "Limpiar", self.limpiar).grid(row=0, column=2, padx=6)

    def calcular(self):
        try:
            monto = float(self.entries["monto"].get())
            tasa = float(self.entries["tasa"].get())
            cuotas = int(self.entries["cuotas"].get())
        except (TypeError, ValueError):
            messagebox.showerror("Error", "Verifica monto, tasa y cuotas.")
            return
        interes = monto * (tasa / 100.0)
        total = monto + interes
        cuota = total / max(1, cuotas)
        self.lbl_res.configure(text=f"Interés: {fmt_money(interes)} | Total: {fmt_money(total)} | Cuota: {fmt_money(cuota)}")

    def limpiar(self):
        for e in self.entries.values():
            e.delete(0, tk.END)
        self.entries["fecha"].insert(0, today_str())
        self.lbl_res.configure(text="Interes: $0 | Total: $0 | Cuota: $0")
        self.cmb_freq.set("mensual")

    def guardar(self):
        try:
            nombre = self.entries["nombre"].get().strip()
            ident  = self.entries["ident"].get().strip()
            tel    = self.entries["tel"].get().strip()
            barrio = self.entries["barrio"].get().strip()
            dire   = self.entries["dir"].get().strip()
            fecha  = self.entries["fecha"].get().strip()
            freq   = self.cmb_freq.get().strip().lower()
            cuotas = int(self.entries["cuotas"].get())
            monto  = float(self.entries["monto"].get())
            tasa   = float(self.entries["tasa"].get())
        except Exception as e:
            messagebox.showerror("Error", f"Datos inválidos: {e}")
            return

        interes = monto * (tasa / 100.0)
        total = monto + interes
        cuota = total / max(1, cuotas)
        dias = {"diaria": 1, "semanal": 7, "quincenal": 15, "mensual": 30}.get(freq, 30)
        venc = add_days(fecha, dias * cuotas)

        cid = db.get_or_create_cliente(
            nombre, ident, tel, barrio, dire, _DESKTOP_OWNER_USER_ID
        )
        pid = db.nuevo_prestamo(
            cid,
            fecha,
            freq,
            cuotas,
            monto,
            tasa,
            interes,
            total,
            cuota,
            venc,
            _DESKTOP_USER_ID,
            _DESKTOP_IS_ADMIN,
        )
        messagebox.showinfo("OK", f"Préstamo creado (ID {pid}).")
        self.limpiar()
        self.refresh_dashboard()

    # ---------- PRÉSTAMOS ----------
    def build_prestamos(self, parent):
        parent.grid_columnconfigure(0, weight=1)
        parent.grid_rowconfigure(1, weight=1)

        top = ctk.CTkFrame(parent, fg_color="transparent")
        top.grid(row=0, column=0, sticky="ew", padx=8, pady=4)

        ctk.CTkLabel(top, text="Mostrar:", text_color=THEME["text"]).pack(side="left", padx=(4, 6))
        self.cmb_estado = ctk.CTkComboBox(top, values=["Activos", "Todos", "Pagados", "Vencen hoy"], state="readonly")
        self.cmb_estado.set("Activos")
        self.cmb_estado.pack(side="left")
        self.make_button(top, "Filtrar", self.refresh_prestamos, width=100).pack(side="left", padx=6)

        legend = ctk.CTkFrame(top, fg_color=THEME["panel_alt"], corner_radius=10, border_width=1, border_color="#314158")
        legend.pack(side="right", padx=(12, 4))
        ctk.CTkLabel(legend, text="Leyenda:", font=(FONT_FAMILY, 11, "bold"), text_color=THEME["muted_text"]).pack(side="left", padx=(10, 6), pady=6)
        ctk.CTkLabel(legend, text="●", font=(FONT_FAMILY, 12), text_color="#34D399").pack(side="left", padx=(0, 2))
        ctk.CTkLabel(legend, text="Observaciones", font=(FONT_FAMILY, 11), text_color=THEME["text"]).pack(side="left", padx=(0, 12), pady=6)
        ctk.CTkLabel(legend, text="◆", font=(FONT_FAMILY, 12), text_color="#FB7185").pack(side="left", padx=(0, 2))
        ctk.CTkLabel(legend, text="En mora", font=(FONT_FAMILY, 11), text_color=THEME["text"]).pack(side="left", padx=(0, 10), pady=6)

        table_fr = ctk.CTkFrame(parent, fg_color=THEME["panel"], corner_radius=12, border_width=1, border_color="#253042")
        table_fr.grid(row=1, column=0, sticky="nsew", padx=8, pady=8)
        table_fr.grid_rowconfigure(0, weight=1)
        table_fr.grid_columnconfigure(0, weight=1)

        cols = (
            "senal",
            "cliente",
            "ident",
            "monto",
            "tasa",
            "cuotas",
            "cuota",
            "fecha",
            "venc",
            "frecuencia",
            "estado",
            "pagadas",
            "proximo_pago",
            "pid",
        )
        headers = (
            "",
            "Cliente",
            "Ident",
            "Monto",
            "Tasa %",
            "Cuotas",
            "Cuota",
            "Fecha",
            "Vencimiento",
            "Frecuencia",
            "Estado",
            "Pagadas",
            "Próx. Pago",
            "",
        )

        self.tree = ttk.Treeview(table_fr, columns=cols, show="headings", style="Prestamos.Treeview")
        anchors = {
            "senal": "center",
            "cliente": "w",
            "ident": "center",
            "monto": "center",
            "tasa": "center",
            "cuotas": "center",
            "cuota": "center",
            "fecha": "center",
            "venc": "center",
            "frecuencia": "center",
            "estado": "center",
            "pagadas": "center",
            "proximo_pago": "center",
            "pid": "center",
        }
        widths = (44, 188, 118, 120, 72, 72, 120, 102, 118, 100, 88, 80, 112, 0)
        for c, h in zip(cols, headers):
            self.tree.heading(c, text=h, anchor=anchors.get(c, "center"))
        for c in cols:
            self.tree.column(c, anchor=anchors.get(c, "center"))
        for c, w in zip(cols, widths):
            self.tree.column(c, width=w)
        self.tree.column("pid", width=0, stretch=False, minwidth=0)
        self.tree.column("senal", stretch=False, minwidth=40)

        vsb = ttk.Scrollbar(table_fr, orient="vertical", command=self.tree.yview, style="Prestamos.Vertical.TScrollbar")
        self.tree.configure(yscroll=vsb.set)
        self.tree.grid(row=0, column=0, sticky="nsew")
        vsb.grid(row=0, column=1, sticky="ns")

        btns = ctk.CTkFrame(parent, fg_color="transparent")
        btns.grid(row=2, column=0, sticky="w", padx=8, pady=8)

        self.make_button(btns, "Actualizar", self.refresh_prestamos).pack(side="left", padx=4)
        self.make_button(btns, "Cobrar", self.abonar).pack(side="left", padx=4)
        self.make_button(btns, "Historial pagos", self.ver_pagos).pack(side="left", padx=4)
        self.make_button(btns, "Editar cliente", self.editar_o_eliminar).pack(side="left", padx=4)
        self.make_button(btns, "Observaciones", lambda: abrir_observaciones(self)).pack(side="left", padx=4)

        self.refresh_prestamos()

    def refresh_prestamos(self):
        estado = self.cmb_estado.get()
        hoy = today_str()

        if estado == "Activos":
            where = "p.estado='ACTIVO'"

        elif estado == "Pagados":
            where = "p.estado='PAGADO'"

        elif estado == "Vencen hoy":
            where = f"p.estado='ACTIVO' AND p.vencimiento='{hoy}'"

        else:
            where = ""

        # limpiar filas actuales

        for i in self.tree.get_children():
            self.tree.delete(i)

        hoy = datetime.strptime(today_str(), "%Y-%m-%d")

        # Filas: franjas alternas suaves; observaciones = borde visual vía fondo tintado; mora = texto suave
        self.tree.tag_configure("stripe_even", background=THEME["panel"])
        self.tree.tag_configure("stripe_odd", background="#151C2E")
        self.tree.tag_configure(
            "has_note",
            background="#14532D",
            foreground=THEME["text"],
        )
        self.tree.tag_configure(
            "mora",
            background="#4C1D2A",
            foreground=THEME["text"],
        )
        self.tree.tag_configure(
            "nota_y_mora",
            background="#3F4A2E",
            foreground=THEME["text"],
        )

        for idx, r in enumerate(db.listar_prestamos(where)):
            en_mora = prestamo_en_mora(r, hoy)

            tiene_nota = False
            if len(r) > 14 and r[14] and str(r[14]).strip() != "":
                tiene_nota = True

            senal_parts = []
            if tiene_nota:
                senal_parts.append("●")
            if en_mora:
                senal_parts.append("◆")
            senal_txt = " ".join(senal_parts)

            tags = ["stripe_odd" if idx % 2 else "stripe_even"]
            if tiene_nota and en_mora:
                tags.append("nota_y_mora")
            elif tiene_nota:
                tags.append("has_note")
            elif en_mora:
                tags.append("mora")

            nombre = str(r[1]).strip()
            if nombre:
                nombre = nombre.title()

            self.tree.insert(
                "",
                "end",
                values=(
                    senal_txt,
                    nombre,
                    r[2],
                    fmt_money(r[3]),
                    r[4],
                    r[5],
                    fmt_money(r[6]),
                    r[7],
                    r[8],
                    r[12],
                    r[9],
                    r[10],
                    r[13],
                    r[0],
                ),
                tags=tags,
            )

    def _selected_pid(self):
        sel = self.tree.selection()
        if not sel:
            return None
        vals = self.tree.item(sel[0])["values"]
        return int(vals[-1])

    def abonar(self):
        pid = self._selected_pid()
        if not pid: return
        valor = simpledialog.askfloat("Abono", "Valor a pagar:")
        if not valor: return
        try:
            pago_id, _, _, _ = db.registrar_pago(
                pid, today_str(), float(valor), _DESKTOP_USER_ID, _DESKTOP_IS_ADMIN
            )
        except Exception as e:
            messagebox.showerror("Error", str(e))
            return
        info = db.obtener_prestamo(pid, _DESKTOP_USER_ID, _DESKTOP_IS_ADMIN)
        nombre = info[2]
        cuota  = info[14] + 1
        path = generar_recibo(nombre, pid, cuota, float(valor), today_str())
        messagebox.showinfo("OK", f"Pago registrado (ID {pago_id}).\nRecibo: {path}")
        self.refresh_prestamos(); self.refresh_dashboard(); self.refresh_pagos()


    def ver_pagos(self):
        pid = self._selected_pid()
        if not pid:
            return

        info = db.obtener_prestamo(pid, _DESKTOP_USER_ID, _DESKTOP_IS_ADMIN)
        if not info:
            messagebox.showerror("Error", "No se encontro el prestamo.")
            return

        pagos = db.listar_pagos(pid, _DESKTOP_USER_ID, _DESKTOP_IS_ADMIN)
        total_pagar = float(info[10])
        total_pagado = sum(float(p[4]) for p in pagos)
        total_faltante = max(0.0, total_pagar - total_pagado)
        cuotas_restantes = max(0, int(info[6]) - int(info[14]))
        puede_renovar = info[13] == "ACTIVO" and cuotas_restantes == 1 and total_faltante > 0

        win = ctk.CTkToplevel(self)
        win.title(f"Historial de pagos - {info[2]}")
        win.geometry("820x500")
        win.minsize(760, 460)
        win.transient(self)
        win.grab_set()

        root = ctk.CTkFrame(win, fg_color=THEME["panel"], corner_radius=14, border_width=1, border_color="#253042")
        root.pack(fill="both", expand=True, padx=12, pady=12)
        root.grid_columnconfigure(0, weight=1)
        root.grid_rowconfigure(2, weight=1)

        ctk.CTkLabel(
            root,
            text=f"Cliente: {info[2]}",
            font=(FONT_FAMILY, 17, "bold"),
            text_color=THEME["text"],
        ).grid(row=0, column=0, sticky="w", padx=12, pady=(12, 4))

        resumen = ctk.CTkFrame(root, fg_color=THEME["panel_alt"], corner_radius=12, border_width=1, border_color="#314158")
        resumen.grid(row=1, column=0, sticky="ew", padx=12, pady=(2, 8))
        resumen.grid_columnconfigure(0, weight=1)
        resumen.grid_columnconfigure(1, weight=1)
        resumen.grid_columnconfigure(2, weight=1)

        ctk.CTkLabel(
            resumen,
            text=f"Total prestamo: {fmt_money(total_pagar)}",
            font=(FONT_FAMILY, 12, "bold"),
            text_color=THEME["muted_text"],
        ).grid(row=0, column=0, sticky="w", padx=10, pady=10)
        ctk.CTkLabel(
            resumen,
            text=f"Pagado: {fmt_money(total_pagado)}",
            font=(FONT_FAMILY, 12, "bold"),
            text_color="#22C55E",
        ).grid(row=0, column=1, sticky="w", padx=10, pady=10)
        ctk.CTkLabel(
            resumen,
            text=f"Faltante: {fmt_money(total_faltante)}",
            font=(FONT_FAMILY, 12, "bold"),
            text_color="#F59E0B",
        ).grid(row=0, column=2, sticky="w", padx=10, pady=10)

        table_wrap = ctk.CTkFrame(root, fg_color=THEME["panel_alt"], corner_radius=12, border_width=1, border_color="#314158")
        table_wrap.grid(row=2, column=0, sticky="nsew", padx=12, pady=(0, 10))
        table_wrap.grid_rowconfigure(0, weight=1)
        table_wrap.grid_columnconfigure(0, weight=1)

        cols = ("id", "fecha", "valor", "cuota", "saldo")
        headers = ("Pago", "Fecha", "Valor", "Cuota", "Saldo restante")
        widths_h = (90, 140, 150, 90, 168)
        tree = ttk.Treeview(table_wrap, columns=cols, show="headings", style="Pagos.Treeview")
        for c, h, w in zip(cols, headers, widths_h):
            tree.heading(c, text=h, anchor="center")
            tree.column(c, width=w, anchor="center")

        vsb = ttk.Scrollbar(table_wrap, orient="vertical", command=tree.yview, style="Pagos.Vertical.TScrollbar")
        tree.configure(yscroll=vsb.set)
        tree.grid(row=0, column=0, sticky="nsew")
        vsb.grid(row=0, column=1, sticky="ns")

        tree.tag_configure("stripe_even", background=THEME["panel"])
        tree.tag_configure("stripe_odd", background="#151C2E")

        for idx, p in enumerate(reversed(pagos)):
            tag = "stripe_odd" if idx % 2 else "stripe_even"
            tree.insert(
                "",
                "end",
                values=(p[0], p[3], fmt_money(p[4]), p[5], fmt_money(p[6])),
                tags=(tag,),
            )

        if not pagos:
            tree.insert("", "end", values=("-", "-", "Sin pagos registrados", "-", fmt_money(total_pagar)))

        btns = ctk.CTkFrame(root, fg_color="transparent")
        btns.grid(row=3, column=0, sticky="e", padx=12, pady=(0, 10))
        self.make_button(
            btns,
            "Historial completo",
            lambda: self.ver_historial_completo_cliente(int(info[1]), str(info[2])),
            width=170,
        ).pack(side="left", padx=(0, 8))
        self.make_button(
            btns,
            "Renovar credito",
            lambda: self.renovar_credito_desde_historial(pid, info, total_faltante, win),
            width=170,
        ).pack(side="right", padx=(0, 8))
        if not puede_renovar:
            for child in btns.winfo_children():
                if isinstance(child, ctk.CTkButton) and child.cget("text") == "Renovar credito":
                    child.configure(state="disabled")
        self.make_button(btns, "Cerrar", win.destroy, width=120).pack(side="right")

    def renovar_credito_desde_historial(self, prestamo_id, info_prestamo, faltante_actual, ventana_historial):
        monto_original = float(info_prestamo[7])

        if faltante_actual <= 0:
            messagebox.showerror("Renovacion", "Este prestamo ya no tiene saldo pendiente.")
            return

        valor_entrega = max(0.0, monto_original - float(faltante_actual))

        dlg = ctk.CTkToplevel(self)
        dlg.title("Simulación de renovación")
        dlg.configure(fg_color=THEME["bg"])
        dlg.transient(self)
        dlg.grab_set()
        dlg.resizable(False, False)

        w, h = 520, 458
        dlg.update_idletasks()
        x = (dlg.winfo_screenwidth() // 2) - (w // 2)
        y = (dlg.winfo_screenheight() // 2) - (h // 2)
        dlg.geometry(f"{w}x{h}+{x}+{y}")

        shell = ctk.CTkFrame(dlg, fg_color="transparent")
        shell.pack(fill="both", expand=True, padx=16, pady=16)

        card = ctk.CTkFrame(
            shell,
            fg_color=THEME["panel"],
            corner_radius=16,
            border_width=1,
            border_color="#253042",
        )
        card.pack(fill="both", expand=True)

        header = ctk.CTkFrame(card, fg_color="transparent")
        header.pack(fill="x", padx=22, pady=(22, 6))
        ctk.CTkLabel(
            header,
            text="Simulación de renovación",
            font=(FONT_FAMILY, 20, "bold"),
            text_color=THEME["accent"],
            anchor="w",
        ).pack(fill="x")
        ctk.CTkLabel(
            header,
            text=f"Préstamo actual · ID {prestamo_id}",
            font=(FONT_FAMILY, 13),
            text_color=THEME["muted_text"],
            anchor="w",
        ).pack(fill="x", pady=(4, 0))

        def stat_line(parent, label: str, value: str):
            row = ctk.CTkFrame(parent, fg_color="transparent")
            row.pack(fill="x", padx=22, pady=8)
            ctk.CTkLabel(
                row,
                text=label,
                font=(FONT_FAMILY, 12),
                text_color=THEME["muted_text"],
                anchor="w",
            ).pack(side="left", fill="x", expand=True)
            ctk.CTkLabel(
                row,
                text=value,
                font=(FONT_FAMILY, 13, "bold"),
                text_color=THEME["text"],
                anchor="e",
            ).pack(side="right")

        stat_line(card, "Saldo pendiente (última cuota)", fmt_money(faltante_actual))
        stat_line(card, "Monto base para renovar", fmt_money(monto_original))

        highlight = ctk.CTkFrame(
            card,
            fg_color=THEME["panel_alt"],
            corner_radius=14,
            border_width=1,
            border_color="#67E8F9",
        )
        highlight.pack(fill="x", padx=18, pady=(12, 14))
        inner = ctk.CTkFrame(highlight, fg_color="transparent")
        inner.pack(fill="x", padx=18, pady=16)
        ctk.CTkLabel(
            inner,
            text="Dinero sugerido para entregar",
            font=(FONT_FAMILY, 12),
            text_color=THEME["muted_text"],
            anchor="w",
        ).pack(fill="x")
        ctk.CTkLabel(
            inner,
            text=fmt_money(valor_entrega),
            font=(FONT_FAMILY, 28, "bold"),
            text_color=THEME["accent"],
            anchor="w",
        ).pack(fill="x", pady=(6, 0))

        ctk.CTkLabel(
            card,
            text="No se realizaron cambios en el préstamo ni en los pagos. Esta ventana solo orienta el cobro o la entrega en una renovación.",
            font=(FONT_FAMILY, 11),
            text_color=THEME["muted_text"],
            wraplength=460,
            justify="left",
            anchor="w",
        ).pack(fill="x", padx=22, pady=(0, 8))

        footer = ctk.CTkFrame(card, fg_color="transparent")
        footer.pack(fill="x", padx=22, pady=(4, 20))
        self.make_button(footer, "Aceptar", dlg.destroy, width=130).pack(side="right")

        dlg.focus_set()

    def ver_historial_completo_cliente(self, cliente_id, nombre_cliente):
        prestamos_base = db.listar_prestamos(
            "p.cliente_id = %s",
            (cliente_id,),
            _DESKTOP_USER_ID,
            _DESKTOP_IS_ADMIN,
        )
        pagos_base = []
        for prestamo in prestamos_base:
            pagos_base.extend(
                db.listar_pagos(prestamo[0], _DESKTOP_USER_ID, _DESKTOP_IS_ADMIN)
            )

        win = ctk.CTkToplevel(self)
        win.title(f"Historial completo - {nombre_cliente}")
        win.geometry("980x620")
        win.minsize(900, 560)
        win.transient(self)
        win.grab_set()

        root = ctk.CTkFrame(
            win,
            fg_color=THEME["panel"],
            corner_radius=14,
            border_width=1,
            border_color="#253042",
        )
        root.pack(fill="both", expand=True, padx=12, pady=12)
        root.grid_columnconfigure(0, weight=1)
        root.grid_rowconfigure(2, weight=1)
        root.grid_rowconfigure(4, weight=1)

        ctk.CTkLabel(
            root,
            text=f"Cliente: {nombre_cliente}",
            font=(FONT_FAMILY, 17, "bold"),
            text_color=THEME["text"],
        ).grid(row=0, column=0, sticky="w", padx=12, pady=(12, 6))

        filtros = ctk.CTkFrame(
            root,
            fg_color=THEME["panel_alt"],
            corner_radius=12,
            border_width=1,
            border_color="#314158",
        )
        filtros.grid(row=1, column=0, sticky="ew", padx=12, pady=(0, 8))
        for i in range(7):
            filtros.grid_columnconfigure(i, weight=1)

        ctk.CTkLabel(filtros, text="Filtro:", text_color=THEME["text"]).grid(row=0, column=0, padx=(10, 6), pady=10, sticky="e")
        filtro_tipo = ctk.CTkComboBox(
            filtros,
            values=["Todo", "Hoy", "Este mes", "Ultimos 3 meses", "Personalizado"],
            width=170,
            state="readonly",
        )
        filtro_tipo.set("Todo")
        filtro_tipo.grid(row=0, column=1, padx=6, pady=10, sticky="w")

        ctk.CTkLabel(filtros, text="Desde:", text_color=THEME["text"]).grid(row=0, column=2, padx=(6, 4), pady=10, sticky="e")
        entry_desde = ctk.CTkEntry(filtros, width=120, placeholder_text="YYYY-MM-DD")
        entry_desde.grid(row=0, column=3, padx=4, pady=10, sticky="w")
        ctk.CTkLabel(filtros, text="Hasta:", text_color=THEME["text"]).grid(row=0, column=4, padx=(6, 4), pady=10, sticky="e")
        entry_hasta = ctk.CTkEntry(filtros, width=120, placeholder_text="YYYY-MM-DD")
        entry_hasta.grid(row=0, column=5, padx=4, pady=10, sticky="w")

        resumen = ctk.CTkFrame(
            root,
            fg_color=THEME["panel_alt"],
            corner_radius=12,
            border_width=1,
            border_color="#314158",
        )
        resumen.grid(row=2, column=0, sticky="ew", padx=12, pady=(0, 8))
        for i in range(4):
            resumen.grid_columnconfigure(i, weight=1)

        lbl_prestamos = ctk.CTkLabel(resumen, font=(FONT_FAMILY, 12, "bold"), text_color=THEME["muted_text"])
        lbl_prestamos.grid(row=0, column=0, sticky="w", padx=10, pady=10)
        lbl_total = ctk.CTkLabel(resumen, font=(FONT_FAMILY, 12, "bold"), text_color=THEME["muted_text"])
        lbl_total.grid(row=0, column=1, sticky="w", padx=10, pady=10)
        lbl_pagado = ctk.CTkLabel(resumen, font=(FONT_FAMILY, 12, "bold"), text_color="#22C55E")
        lbl_pagado.grid(row=0, column=2, sticky="w", padx=10, pady=10)
        lbl_pendiente = ctk.CTkLabel(resumen, font=(FONT_FAMILY, 12, "bold"), text_color="#F59E0B")
        lbl_pendiente.grid(row=0, column=3, sticky="w", padx=10, pady=10)

        ctk.CTkLabel(root, text="Prestamos del cliente", font=(FONT_FAMILY, 13, "bold"), text_color=THEME["text"]).grid(row=3, column=0, sticky="nw", padx=12, pady=(4, 0))
        tabla_prestamos = ctk.CTkFrame(root, fg_color=THEME["panel_alt"], corner_radius=12, border_width=1, border_color="#314158")
        tabla_prestamos.grid(row=3, column=0, sticky="nsew", padx=12, pady=(28, 8))
        tabla_prestamos.grid_rowconfigure(0, weight=1)
        tabla_prestamos.grid_columnconfigure(0, weight=1)

        cols_pr = ("id", "fecha", "monto", "cuotas", "pagadas", "estado", "venc")
        tree_pr = ttk.Treeview(tabla_prestamos, columns=cols_pr, show="headings", height=8, style="Prestamos.Treeview")
        for col, title, width in (
            ("id", "Prestamo", 90),
            ("fecha", "Fecha", 120),
            ("monto", "Monto", 130),
            ("cuotas", "Cuotas", 80),
            ("pagadas", "Pagadas", 90),
            ("estado", "Estado", 90),
            ("venc", "Vencimiento", 130),
        ):
            tree_pr.heading(col, text=title, anchor="center")
            tree_pr.column(col, width=width, anchor="center")
        tree_pr.grid(row=0, column=0, sticky="nsew")
        vsb_pr = ttk.Scrollbar(tabla_prestamos, orient="vertical", command=tree_pr.yview, style="Prestamos.Vertical.TScrollbar")
        vsb_pr.grid(row=0, column=1, sticky="ns")
        tree_pr.configure(yscroll=vsb_pr.set)

        ctk.CTkLabel(root, text="Pagos del cliente (todos los prestamos)", font=(FONT_FAMILY, 13, "bold"), text_color=THEME["text"]).grid(row=4, column=0, sticky="nw", padx=12, pady=(4, 0))
        tabla_pagos = ctk.CTkFrame(root, fg_color=THEME["panel_alt"], corner_radius=12, border_width=1, border_color="#314158")
        tabla_pagos.grid(row=4, column=0, sticky="nsew", padx=12, pady=(28, 10))
        tabla_pagos.grid_rowconfigure(0, weight=1)
        tabla_pagos.grid_columnconfigure(0, weight=1)

        cols_pg = ("pago", "prestamo", "fecha", "valor", "cuota", "saldo")
        tree_pg = ttk.Treeview(tabla_pagos, columns=cols_pg, show="headings", height=9, style="Pagos.Treeview")
        for col, title, width in (
            ("pago", "Pago", 80),
            ("prestamo", "Prestamo", 90),
            ("fecha", "Fecha", 120),
            ("valor", "Valor", 130),
            ("cuota", "Cuota", 80),
            ("saldo", "Saldo", 130),
        ):
            tree_pg.heading(col, text=title, anchor="center")
            tree_pg.column(col, width=width, anchor="center")
        tree_pg.grid(row=0, column=0, sticky="nsew")
        vsb_pg = ttk.Scrollbar(tabla_pagos, orient="vertical", command=tree_pg.yview, style="Pagos.Vertical.TScrollbar")
        vsb_pg.grid(row=0, column=1, sticky="ns")
        tree_pg.configure(yscroll=vsb_pg.set)

        tree_pr.tag_configure("stripe_even", background=THEME["panel"])
        tree_pr.tag_configure("stripe_odd", background="#151C2E")
        tree_pg.tag_configure("stripe_even", background=THEME["panel"])
        tree_pg.tag_configure("stripe_odd", background="#151C2E")

        def parse_date(value):
            try:
                return datetime.strptime(value, "%Y-%m-%d")
            except (TypeError, ValueError):
                return None

        def in_range(date_str, start_dt, end_dt):
            dt = parse_date(date_str)
            if not dt:
                return False
            if start_dt and dt < start_dt:
                return False
            if end_dt and dt > end_dt:
                return False
            return True

        def resolve_range():
            now = datetime.now()
            kind = filtro_tipo.get()
            if kind == "Todo":
                return None, None
            if kind == "Hoy":
                return now.replace(hour=0, minute=0, second=0, microsecond=0), now.replace(hour=23, minute=59, second=59, microsecond=999999)
            if kind == "Este mes":
                start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
                return start, now.replace(hour=23, minute=59, second=59, microsecond=999999)
            if kind == "Ultimos 3 meses":
                return now - timedelta(days=90), now
            # Personalizado
            start = parse_date(entry_desde.get().strip())
            end = parse_date(entry_hasta.get().strip())
            if (entry_desde.get().strip() and not start) or (entry_hasta.get().strip() and not end):
                messagebox.showerror("Filtro", "Usa formato YYYY-MM-DD en fechas.")
                return "error", "error"
            if start and end and start > end:
                messagebox.showerror("Filtro", "La fecha 'Desde' no puede ser mayor que 'Hasta'.")
                return "error", "error"
            return start, end

        def aplicar_filtro():
            for item in tree_pr.get_children():
                tree_pr.delete(item)
            for item in tree_pg.get_children():
                tree_pg.delete(item)

            start_dt, end_dt = resolve_range()
            if start_dt == "error":
                return

            prestamos = [r for r in prestamos_base if in_range(r[7], start_dt, end_dt)] if (start_dt or end_dt) else list(prestamos_base)
            pagos = [p for p in pagos_base if in_range(p[3], start_dt, end_dt)] if (start_dt or end_dt) else list(pagos_base)

            total_prestado = sum(float(p[3]) for p in prestamos)
            total_pagado = sum(float(p[4]) for p in pagos)
            total_faltante = max(0.0, total_prestado - total_pagado)

            lbl_prestamos.configure(text=f"Prestamos: {len(prestamos)}")
            lbl_total.configure(text=f"Total prestado: {fmt_money(total_prestado)}")
            lbl_pagado.configure(text=f"Total pagado: {fmt_money(total_pagado)}")
            lbl_pendiente.configure(text=f"Pendiente: {fmt_money(total_faltante)}")

            for i, r in enumerate(prestamos):
                tag = "stripe_odd" if i % 2 else "stripe_even"
                tree_pr.insert("", "end", values=(r[0], r[7], fmt_money(r[3]), r[5], r[10], r[9], r[8]), tags=(tag,))
            if not prestamos:
                tree_pr.insert("", "end", values=("-", "-", "Sin prestamos", "-", "-", "-", "-"))

            for i, p in enumerate(sorted(pagos, key=lambda x: x[0], reverse=True)):
                tag = "stripe_odd" if i % 2 else "stripe_even"
                tree_pg.insert("", "end", values=(p[0], p[2], p[3], fmt_money(p[4]), p[5], fmt_money(p[6])), tags=(tag,))
            if not pagos:
                tree_pg.insert("", "end", values=("-", "-", "-", "Sin pagos registrados", "-", "-"))

        self.make_button(filtros, "Aplicar", aplicar_filtro, width=110).grid(row=0, column=6, padx=6, pady=10, sticky="w")
        self.make_button(
            filtros,
            "Limpiar",
            lambda: (filtro_tipo.set("Todo"), entry_desde.delete(0, tk.END), entry_hasta.delete(0, tk.END), aplicar_filtro()),
            width=100,
        ).grid(row=0, column=6, padx=(120, 6), pady=10, sticky="w")

        aplicar_filtro()

        actions = ctk.CTkFrame(root, fg_color="transparent")
        actions.grid(row=5, column=0, sticky="e", padx=12, pady=(0, 10))
        self.make_button(actions, "Cerrar", win.destroy, width=120).pack(side="right")

    # ---------- EDITAR / ELIMINAR ----------
    def editar_o_eliminar(self):
        pid = self._selected_pid()
        if not pid: return
        info = db.obtener_prestamo(pid, _DESKTOP_USER_ID, _DESKTOP_IS_ADMIN)
        if not info:
            messagebox.showerror("Error", "No se encontró el préstamo.")
            return
        cid = info[1]
        cli = db.obtener_cliente(cid, _DESKTOP_USER_ID, _DESKTOP_IS_ADMIN)
        if not cli:
            messagebox.showerror("Error", "No se encontró el cliente.")
            return

        win = ctk.CTkToplevel(self)
        win.title(f"Editar cliente - {info[2]}")
        win.geometry("640x560")
        fr = ctk.CTkFrame(win, fg_color=THEME["panel"], corner_radius=14, border_width=1, border_color="#253042")
        fr.pack(fill="both", expand=True, padx=12, pady=12)
        fr.grid_columnconfigure(1, weight=1)

        def row(r, label, init=""):
            ctk.CTkLabel(fr, text=label, text_color=THEME["text"]).grid(row=r, column=0, sticky="w", padx=6, pady=6)
            e = ctk.CTkEntry(fr, fg_color=THEME["panel_alt"], border_color="#314158")
            e.grid(row=r, column=1, sticky="ew", padx=6, pady=6)
            e.insert(0, init)
            return e

        ctk.CTkLabel(fr, text="--- DATOS DEL CLIENTE ---", text_color="#00B4D8").grid(row=0, column=0, columnspan=2, sticky="w", pady=(4, 6))
        ent_nombre = row(1, "Nombre:", cli[1])
        ent_ident  = row(2, "Identificación:", cli[2])
        ent_tel    = row(3, "Teléfono:", cli[3])
        ent_barrio = row(4, "Barrio:", cli[4])
        ent_dir    = row(5, "Dirección:", cli[5])

        ctk.CTkLabel(fr, text="--- DATOS DEL PRÉSTAMO ---", text_color="#00B4D8").grid(row=6, column=0, columnspan=2, sticky="w", pady=(10, 6))
        ent_fecha = row(7, "Fecha:", info[4])

        ctk.CTkLabel(fr, text="Frecuencia:", text_color=THEME["text"]).grid(row=8, column=0, sticky="w", padx=6, pady=6)
        cmb_freq = ctk.CTkComboBox(fr, values=["diaria","semanal","quincenal","mensual"])
        cmb_freq.set(info[5])
        cmb_freq.grid(row=8, column=1, sticky="ew", padx=6, pady=6)

        ent_cuotas = row(9, "Cuotas:", str(info[6]))
        ent_monto  = row(10,"Monto:",  str(info[7]))
        ent_tasa   = row(11,"Tasa %:", str(info[8]))

        btns = ctk.CTkFrame(fr, fg_color="transparent")
        btns.grid(row=12, column=0, columnspan=2, sticky="w", pady=8)
        self.make_button(btns, "Guardar cambios",
                         lambda: self.guardar_cambios(cid, pid, ent_nombre, ent_ident, ent_tel, ent_barrio, ent_dir, ent_fecha, cmb_freq, ent_cuotas, ent_monto, ent_tasa, win)).pack(side="left", padx=4)
        self.make_button(btns, "Eliminar cliente y todo",
                         lambda: self.eliminar_cliente(cid, win)).pack(side="left", padx=4)

    def guardar_cambios(self, cid, pid, ent_nombre, ent_ident, ent_tel, ent_barrio, ent_dir, ent_fecha, cmb_freq, ent_cuotas, ent_monto, ent_tasa, win):
        try:
            db.actualizar_cliente(
                cid,
                ent_nombre.get(),
                ent_ident.get(),
                ent_tel.get(),
                ent_barrio.get(),
                ent_dir.get(),
                _DESKTOP_USER_ID,
                _DESKTOP_IS_ADMIN,
            )
            dias = {"diaria":1, "semanal":7, "quincenal":15, "mensual":30}.get(cmb_freq.get(), 30)
            venc = add_days(ent_fecha.get(), dias * int(ent_cuotas.get()))
            db.actualizar_prestamo(
                pid,
                ent_fecha.get(),
                cmb_freq.get(),
                int(ent_cuotas.get()),
                float(ent_monto.get()),
                float(ent_tasa.get()),
                venc,
                _DESKTOP_USER_ID,
                _DESKTOP_IS_ADMIN,
            )
            messagebox.showinfo("OK", "Cambios guardados.")
            win.destroy()
            self.refresh_prestamos(); self.refresh_dashboard()
        except Exception as e:
            messagebox.showerror("Error", str(e))

    def eliminar_cliente(self, cid, win):
        if not messagebox.askyesno("Eliminar", "¿Seguro que deseas eliminar al cliente y todo su historial?"):
            return
        db.eliminar_cliente_y_todo(cid, _DESKTOP_USER_ID, _DESKTOP_IS_ADMIN)
        messagebox.showinfo("Eliminado", "Cliente eliminado con todo su historial.")
        win.destroy()
        self.refresh_prestamos(); self.refresh_dashboard(); self.refresh_pagos()


    # ---------- PAGOS ----------
    def build_pagos(self, parent):
        parent.grid_columnconfigure(0, weight=1)
        parent.grid_rowconfigure(1, weight=1)

        top = ctk.CTkFrame(parent, fg_color="transparent")
        top.grid(row=0, column=0, sticky="ew", padx=8, pady=(8, 4))
        ctk.CTkLabel(
            top,
            text="Pagos registrados",
            font=(FONT_FAMILY, 15, "bold"),
            text_color=THEME["text"],
        ).pack(side="left", padx=(4, 16))
        ctk.CTkLabel(
            top,
            text="Estado del préstamo al momento del listado (misma regla que Préstamos).",
            font=(FONT_FAMILY, 11),
            text_color=THEME["muted_text"],
        ).pack(side="left", padx=(0, 12))

        legend = ctk.CTkFrame(top, fg_color=THEME["panel_alt"], corner_radius=10, border_width=1, border_color="#314158")
        legend.pack(side="right", padx=(12, 4))
        ctk.CTkLabel(legend, text="Leyenda:", font=(FONT_FAMILY, 11, "bold"), text_color=THEME["muted_text"]).pack(side="left", padx=(10, 6), pady=6)
        ctk.CTkLabel(legend, text="●", font=(FONT_FAMILY, 12), text_color="#34D399").pack(side="left", padx=(0, 2))
        ctk.CTkLabel(legend, text="Observaciones", font=(FONT_FAMILY, 11), text_color=THEME["text"]).pack(side="left", padx=(0, 12), pady=6)
        ctk.CTkLabel(legend, text="◆", font=(FONT_FAMILY, 12), text_color="#FB7185").pack(side="left", padx=(0, 2))
        ctk.CTkLabel(legend, text="En mora", font=(FONT_FAMILY, 11), text_color=THEME["text"]).pack(side="left", padx=(0, 10), pady=6)

        table_fr = ctk.CTkFrame(parent, fg_color=THEME["panel"], corner_radius=12, border_width=1, border_color="#253042")
        table_fr.grid(row=1, column=0, sticky="nsew", padx=8, pady=8)
        table_fr.grid_rowconfigure(0, weight=1)
        table_fr.grid_columnconfigure(0, weight=1)

        cols = (
            "senal",
            "cliente",
            "prestamo",
            "fecha",
            "valor",
            "cuota",
            "saldo",
            "pago_id",
        )
        headers = ("", "Cliente", "Préstamo", "Fecha", "Valor", "Cuota", "Saldo", "")

        self.tree_pagos = ttk.Treeview(table_fr, columns=cols, show="headings", style="Pagos.Treeview")
        anchors = {
            "senal": "center",
            "cliente": "w",
            "prestamo": "center",
            "fecha": "center",
            "valor": "center",
            "cuota": "center",
            "saldo": "center",
            "pago_id": "center",
        }
        widths = (44, 200, 88, 108, 128, 72, 128, 0)
        for c, h in zip(cols, headers):
            self.tree_pagos.heading(c, text=h, anchor=anchors.get(c, "center"))
        for c in cols:
            self.tree_pagos.column(c, anchor=anchors.get(c, "center"))
        for c, w in zip(cols, widths):
            self.tree_pagos.column(c, width=w)
        self.tree_pagos.column("pago_id", width=0, stretch=False, minwidth=0)
        self.tree_pagos.column("senal", stretch=False, minwidth=40)

        vsb = ttk.Scrollbar(table_fr, orient="vertical", command=self.tree_pagos.yview, style="Pagos.Vertical.TScrollbar")
        self.tree_pagos.configure(yscroll=vsb.set)
        self.tree_pagos.grid(row=0, column=0, sticky="nsew")
        vsb.grid(row=0, column=1, sticky="ns")

        btns = ctk.CTkFrame(parent, fg_color="transparent")
        btns.grid(row=2, column=0, sticky="ew", padx=8, pady=(0, 8))
        self.make_button(btns, "Actualizar", self.refresh_pagos, width=120).pack(side="left", padx=4)
        self.make_button(btns, "Eliminar pago", self.eliminar_pago, width=160).pack(side="right", padx=4)

        self.refresh_pagos()

    def refresh_pagos(self):
        for i in self.tree_pagos.get_children():
            self.tree_pagos.delete(i)

        hoy = datetime.strptime(today_str(), "%Y-%m-%d")
        self.tree_pagos.tag_configure("stripe_even", background=THEME["panel"])
        self.tree_pagos.tag_configure("stripe_odd", background="#151C2E")
        self.tree_pagos.tag_configure(
            "has_note",
            background="#14532D",
            foreground=THEME["text"],
        )
        self.tree_pagos.tag_configure(
            "mora",
            background="#4C1D2A",
            foreground=THEME["text"],
        )
        self.tree_pagos.tag_configure(
            "nota_y_mora",
            background="#3F4A2E",
            foreground=THEME["text"],
        )

        for idx, r in enumerate(db.listar_pagos(None, _DESKTOP_USER_ID, _DESKTOP_IS_ADMIN)):
            estado_p = r[8]
            venc_p = r[7]
            prox_p = r[9]
            notas_p = r[10]
            en_mora = prestamo_en_mora_desde_campos(estado_p, venc_p, prox_p, hoy)
            tiene_nota = bool(notas_p and str(notas_p).strip())

            senal_parts = []
            if tiene_nota:
                senal_parts.append("●")
            if en_mora:
                senal_parts.append("◆")
            senal_txt = " ".join(senal_parts)

            tags = ["stripe_odd" if idx % 2 else "stripe_even"]
            if tiene_nota and en_mora:
                tags.append("nota_y_mora")
            elif tiene_nota:
                tags.append("has_note")
            elif en_mora:
                tags.append("mora")

            nombre = str(r[1]).strip()
            if nombre:
                nombre = nombre.title()

            self.tree_pagos.insert(
                "",
                "end",
                values=(
                    senal_txt,
                    nombre,
                    r[2],
                    r[3],
                    fmt_money(r[4]),
                    r[5],
                    fmt_money(r[6]),
                    r[0],
                ),
                tags=tags,
            )

    def eliminar_pago(self):
        sel = self.tree_pagos.selection()
        if not sel:
            messagebox.showerror("Error", "Selecciona un pago.")
            return
        vals = self.tree_pagos.item(sel[0])["values"]
        try:
            prestamo_id = int(vals[2])
            pago_id = int(vals[7])
        except (TypeError, ValueError, IndexError):
            messagebox.showerror("Error", "No se pudo identificar el pago.")
            return
        if not messagebox.askyesno("Confirmar", "¿Eliminar este pago y actualizar saldo?"):
            return
        ok = db.eliminar_pago_y_actualizar(
            prestamo_id, pago_id, _DESKTOP_USER_ID, _DESKTOP_IS_ADMIN
        )
        if ok:
            messagebox.showinfo("OK", "Pago eliminado.")
            self.refresh_pagos()
            self.refresh_prestamos()
            self.refresh_dashboard()
        else:
            messagebox.showerror("Error", "No se pudo eliminar el pago.")

if __name__ == "__main__":
    setup_logging()
    logo_path = os.path.join(os.path.dirname(__file__), "assets", "logo_dark.png")
    if not os.path.exists(logo_path):
        logo_path = os.path.join(os.path.dirname(__file__), "assets", "logo.png")

    def launch_main():
        app = App()
        app.mainloop()

    splash = SplashScreen(logo_path, launch_main)
    splash.mainloop()


    