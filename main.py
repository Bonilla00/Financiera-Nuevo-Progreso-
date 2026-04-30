import os
import logging
from logging.handlers import RotatingFileHandler
import tkinter as tk
from tkinter import ttk, messagebox, simpledialog, filedialog
from datetime import datetime, timedelta
from PIL import Image
import customtkinter as ctk
from werkzeug.security import generate_password_hash

from config import APP_TITLE, THEME, FONT_FAMILY, app_base_dir
from utils import fmt_money, today_str, add_days
import db
from recibos import generar_recibo_pdf

# Configuración para el entorno de escritorio
_DESKTOP_USER_ID = 0
_DESKTOP_IS_ADMIN = True
# Se asume que el primer usuario creado (admin) tendrá ID 1
_DESKTOP_OWNER_USER_ID = 1


def setup_logging() -> None:
    """Configura logging global de la app."""
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


def _ensure_admin_user():
    """Verifica si existen usuarios; si no, crea el admin por defecto."""
    try:
        if db.count_usuarios() == 0:
            h = generate_password_hash("admin123")
            db.crear_usuario("admin", h, rol="admin")
            logging.info("Seeder: Usuario 'admin' creado por defecto.")
    except Exception as e:
        logging.error(f"Error en seeder de usuario: {e}")


def fecha_humana_es(fecha: datetime | None = None) -> str:
    if fecha is None:
        fecha = datetime.now()
    dias = ["lunes", "martes", "miercoles", "jueves", "viernes", "sabado", "domingo"]
    meses = ["enero", "febrero", "marzo", "abril", "mayo", "junio", "julio", "agosto", "septiembre", "octubre", "noviembre", "diciembre"]
    return f"{dias[fecha.weekday()]} {fecha.day} de {meses[fecha.month - 1]} de {fecha.year}"


def _parse_fecha_iso(val) -> datetime | None:
    if val is None or str(val).strip() == "":
        return None
    try:
        return datetime.strptime(str(val).strip()[:10], "%Y-%m-%d")
    except ValueError:
        return None


def prestamo_en_mora_desde_campos(estado: str, vencimiento, proximo_pago, hoy: datetime) -> bool:
    if estado != "ACTIVO":
        return False
    prox = _parse_fecha_iso(proximo_pago)
    venc = _parse_fecha_iso(vencimiento)
    ref = prox if prox is not None else venc
    return ref < hoy if ref else False


def prestamo_en_mora(row: tuple, hoy: datetime) -> bool:
    if len(row) < 10:
        return False
    return prestamo_en_mora_desde_campos(
        row[9],
        row[8],
        row[13] if len(row) > 13 else None,
        hoy,
    )


def setup_ctk(app: ctk.CTk):
    ctk.set_appearance_mode("dark")
    ctk.set_default_color_theme("dark-blue")
    app.configure(fg_color=THEME["bg"])

    def styled_button(master, text, command=None, width=140):
        return ctk.CTkButton(
            master, text=text, command=command,
            fg_color=THEME["accent"], hover_color=THEME["accent_alt"],
            text_color="#0B1120", font=(FONT_FAMILY, 12, "bold"),
            corner_radius=12, border_width=1, border_color="#67E8F9",
            height=38, width=width
        )
    app.make_button = styled_button


class Card(ctk.CTkFrame):
    def __init__(self, master, title: str, value: str = "0", color=None, **kwargs):
        super().__init__(master, fg_color=color or THEME["panel"], corner_radius=16, border_width=1, border_color="#253042", **kwargs)
        self.grid_columnconfigure(0, weight=1)
        self.title_lbl = ctk.CTkLabel(self, text=title, font=(FONT_FAMILY, 13, "bold"), text_color=THEME["muted_text"])
        self.title_lbl.grid(row=0, column=0, sticky="w", padx=12, pady=(10, 0))
        self.value_lbl = ctk.CTkLabel(self, text=value, font=(FONT_FAMILY, 28, "bold"), text_color=THEME["accent"])
        self.value_lbl.grid(row=1, column=0, sticky="w", padx=12, pady=(2, 10))


class SplashScreen(ctk.CTk):
    def __init__(self, logo_path: str, main_app_callback):
        super().__init__()
        self.overrideredirect(True)
        width, height = 640, 420
        x = (self.winfo_screenwidth() // 2) - (width // 2)
        y = (self.winfo_screenheight() // 2) - (height // 2)
        self.geometry(f"{width}x{height}+{x}+{y}")
        self.configure(fg_color=THEME["bg"])

        if os.path.exists(logo_path):
            try:
                img = Image.open(logo_path)
                logo_ctk = ctk.CTkImage(light_image=img, dark_image=img, size=(180, 180))
                ctk.CTkLabel(self, image=logo_ctk, text="").pack(pady=(50, 12))
            except Exception: pass

        ctk.CTkLabel(self, text="Financiera Nuevo Progreso", font=(FONT_FAMILY, 22, "bold"), text_color=THEME["accent"]).pack(pady=(0, 6))
        self.progress = ctk.CTkProgressBar(self, width=460, height=16, progress_color=THEME["accent"], fg_color=THEME["panel_alt"], corner_radius=10)
        self.progress.pack(pady=(20, 10))
        self.progress.set(0)
        self._step = 0
        self._main_app_callback = main_app_callback
        self.after(40, self._animate)

    def _animate(self):
        self._step += 1
        val = min(1.0, self._step / 50.0)
        self.progress.set(val)
        if val < 1.0: self.after(40, self._animate)
        else:
            self.destroy()
            self._main_app_callback()


class App(ctk.CTk):
    def __init__(self):
        super().__init__()
        db.ensure_schema_migrations()
        _ensure_admin_user()

        self.title(APP_TITLE)
        self.geometry("1200x750")
        setup_ctk(self)

        style = ttk.Style(self)
        style.theme_use("clam")

        # Estilos compartidos para Treeviews
        for s_name in ["Prestamos.Treeview", "Pagos.Treeview"]:
            style.configure(s_name, background=THEME["panel"], fieldbackground=THEME["panel"], foreground=THEME["text"], rowheight=36, borderwidth=0, font=(FONT_FAMILY, 11))
            style.configure(s_name + ".Heading", background="#1E293B", foreground=THEME["text"], relief="flat", font=(FONT_FAMILY, 11, "bold"), padding=(10, 10))
            style.map(s_name, background=[("selected", THEME["accent_alt"])], foreground=[("selected", "#FFFFFF")])

        container = ctk.CTkFrame(self, fg_color="transparent")
        container.pack(fill="both", expand=True, padx=16, pady=16)
        container.grid_rowconfigure(1, weight=1)
        container.grid_columnconfigure(0, weight=1)

        # Topbar
        topbar = ctk.CTkFrame(container, fg_color=THEME["panel"], corner_radius=14, border_width=1, border_color="#253042", height=58)
        topbar.grid(row=0, column=0, sticky="ew", pady=(0, 12))
        topbar.grid_columnconfigure(1, weight=1)
        ctk.CTkLabel(topbar, text="Financiera Nuevo Progreso", font=(FONT_FAMILY, 16, "bold"), text_color=THEME["text"]).grid(row=0, column=0, padx=16, pady=12)
        self.lbl_hoy = ctk.CTkLabel(topbar, text=f"Hoy: {fecha_humana_es()}", font=(FONT_FAMILY, 12), text_color=THEME["muted_text"])
        self.lbl_hoy.grid(row=0, column=1)
        self.lbl_estado = ctk.CTkLabel(topbar, text="Sistema conectado a PostgreSQL", font=(FONT_FAMILY, 12, "bold"), text_color=THEME["accent"])
        self.lbl_estado.grid(row=0, column=2, padx=16)

        self.tabs = ctk.CTkTabview(container, fg_color=THEME["panel_alt"], segmented_button_selected_color=THEME["accent_alt"])
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

    def _get_backups_dir(self):
        path = os.path.join(app_base_dir(), "backups")
        os.makedirs(path, exist_ok=True)
        return path

    def build_respaldo(self, parent):
        parent.grid_columnconfigure(0, weight=1)
        card = ctk.CTkFrame(parent, fg_color=THEME["panel"], corner_radius=16, border_width=1, border_color="#253042")
        card.grid(row=0, column=0, sticky="ew", padx=24, pady=24)

        ctk.CTkLabel(card, text="Copia de seguridad SQL", font=(FONT_FAMILY, 20, "bold"), text_color=THEME["accent"]).pack(anchor="w", padx=22, pady=(22, 6))
        ctk.CTkLabel(card, text="Exporta o importa el estado completo de la base de datos PostgreSQL en formato .sql", font=(FONT_FAMILY, 12), text_color=THEME["muted_text"]).pack(anchor="w", padx=22, pady=(0, 16))

        btns = ctk.CTkFrame(card, fg_color="transparent")
        btns.pack(anchor="w", padx=18, pady=(0, 22))
        self.make_button(btns, "Exportar SQL", self._accion_hacer_backup, width=180).pack(side="left", padx=6)
        self.make_button(btns, "Importar SQL", self._accion_restaurar_backup, width=180).pack(side="left", padx=6)

    def _accion_hacer_backup(self):
        try:
            sql_text = db.export_database_sql()
            folder = self._get_backups_dir()
            filename = f"backup_{datetime.now().strftime('%Y-%m-%d_%H-%M-%S')}.sql"
            path = os.path.join(folder, filename)
            with open(path, "w", encoding="utf-8") as f:
                f.write(sql_text)
            messagebox.showinfo("Éxito", f"Backup SQL generado:\n{path}")
        except Exception as e:
            messagebox.showerror("Error", f"No se pudo exportar: {e}")

    def _accion_restaurar_backup(self):
        path = filedialog.askopenfilename(title="Seleccionar Backup SQL", filetypes=[("Archivos SQL", "*.sql")])
        if not path: return
        if not messagebox.askyesno("Confirmar", "Se reemplazará TODA la información actual. ¿Continuar?"): return
        try:
            with open(path, "r", encoding="utf-8") as f:
                sql_text = f.read()
            db.restore_database_sql(sql_text)
            messagebox.showinfo("Éxito", "Restauración completada.")
            self.refresh_dashboard(); self.refresh_prestamos(); self.refresh_pagos()
        except Exception as e:
            messagebox.showerror("Error", f"Fallo al restaurar: {e}")

    def build_dashboard(self, parent):
        parent.grid_columnconfigure(0, weight=1)
        cards = ctk.CTkFrame(parent, fg_color="transparent")
        cards.grid(row=0, column=0, sticky="ew", padx=20, pady=20)
        for i in range(4): cards.grid_columnconfigure(i, weight=1)

        self.lbl_total = Card(cards, "Total prestamos")
        self.lbl_activos = Card(cards, "Activos")
        self.lbl_pagados = Card(cards, "Pagados")
        self.lbl_mora = Card(cards, "En mora")

        self.lbl_total.grid(row=0, column=0, padx=8, pady=8, sticky="nsew")
        self.lbl_activos.grid(row=0, column=1, padx=8, pady=8, sticky="nsew")
        self.lbl_pagados.grid(row=0, column=2, padx=8, pady=8, sticky="nsew")
        self.lbl_mora.grid(row=0, column=3, padx=8, pady=8, sticky="nsew")
        self.refresh_dashboard()

    def refresh_dashboard(self):
        prs = db.listar_prestamos()
        activos = sum(1 for p in prs if p[9] == "ACTIVO")
        pagados = sum(1 for p in prs if p[9] == "PAGADO")
        hoy = datetime.strptime(today_str(), "%Y-%m-%d")
        mora = sum(1 for p in prs if prestamo_en_mora(p, hoy))
        self.lbl_total.value_lbl.configure(text=str(len(prs)))
        self.lbl_activos.value_lbl.configure(text=str(activos))
        self.lbl_pagados.value_lbl.configure(text=str(pagados))
        self.lbl_mora.value_lbl.configure(text=str(mora))

    def build_nuevo(self, parent):
        parent.grid_columnconfigure(0, weight=1)
        frm = ctk.CTkFrame(parent, fg_color=THEME["panel"], corner_radius=14, border_width=1, border_color="#253042")
        frm.grid(row=0, column=0, sticky="n", padx=12, pady=12)
        self.entries = {}
        fields = [("Nombre", "nombre"), ("Identificación", "ident"), ("Teléfono", "tel"), ("Monto", "monto"), ("Tasa %", "tasa"), ("Cuotas", "cuotas")]
        for i, (label, key) in enumerate(fields):
            ctk.CTkLabel(frm, text=label).grid(row=i, column=0, padx=10, pady=5, sticky="e")
            e = ctk.CTkEntry(frm, width=250)
            e.grid(row=i, column=1, padx=10, pady=5)
            self.entries[key] = e
        self.make_button(frm, "Guardar Préstamo", self.guardar_nuevo).grid(row=len(fields), column=0, columnspan=2, pady=20)

    def guardar_nuevo(self):
        try:
            monto = float(self.entries["monto"].get())
            tasa = float(self.entries["tasa"].get())
            cuotas = int(self.entries["cuotas"].get())
            interes = monto * (tasa / 100.0)
            total = monto + interes
            cuota = total / cuotas
            venc = add_days(today_str(), 30 * cuotas)
            cid = db.get_or_create_cliente(self.entries["nombre"].get(), self.entries["ident"].get(), self.entries["tel"].get(), "", "", _DESKTOP_OWNER_USER_ID)
            db.nuevo_prestamo(cid, today_str(), "mensual", cuotas, monto, tasa, interes, total, cuota, venc, _DESKTOP_USER_ID, _DESKTOP_IS_ADMIN)
            messagebox.showinfo("Éxito", "Préstamo creado.")
            self.refresh_dashboard(); self.refresh_prestamos()
        except Exception as e: messagebox.showerror("Error", str(e))

    def build_prestamos(self, parent):
        parent.grid_columnconfigure(0, weight=1)
        parent.grid_rowconfigure(0, weight=1)
        self.tree = ttk.Treeview(parent, columns=("id", "cliente", "monto", "estado", "venc"), show="headings", style="Prestamos.Treeview")
        for c, h in zip(self.tree["columns"], ("ID", "Cliente", "Monto", "Estado", "Vencimiento")):
            self.tree.heading(c, text=h)
        self.tree.grid(row=0, column=0, sticky="nsew", padx=10, pady=10)
        btns = ctk.CTkFrame(parent, fg_color="transparent")
        btns.grid(row=1, column=0, pady=10)
        self.make_button(btns, "Cobrar Cuota", self.abonar).pack(side="left", padx=5)
        self.make_button(btns, "Actualizar", self.refresh_prestamos).pack(side="left", padx=5)
        self.refresh_prestamos()

    def refresh_prestamos(self):
        for i in self.tree.get_children(): self.tree.delete(i)
        for r in db.listar_prestamos():
            self.tree.insert("", "end", values=(r[0], r[1], fmt_money(r[3]), r[9], r[8]))

    def abonar(self):
        sel = self.tree.selection()
        if not sel: return
        pid = self.tree.item(sel[0])["values"][0]
        valor = simpledialog.askfloat("Cobro", "Monto a recibir:")
        if not valor: return
        try:
            p_id, num_c, int_mora, v_base = db.registrar_pago(pid, today_str(), float(valor), _DESKTOP_USER_ID, _DESKTOP_IS_ADMIN)
            info = db.obtener_prestamo(pid, _DESKTOP_USER_ID, _DESKTOP_IS_ADMIN)

            # Generar PDF y guardar en carpeta reportes
            pdf_buf = generar_recibo_pdf(info[2], pid, num_c, float(valor), today_str(), _DESKTOP_USER_ID, _DESKTOP_IS_ADMIN, v_base, int_mora)
            rep_dir = os.path.join(app_base_dir(), "reportes")
            os.makedirs(rep_dir, exist_ok=True)
            path = os.path.join(rep_dir, f"recibo_{pid}_{p_id}.pdf")
            with open(path, "wb") as f: f.write(pdf_buf.getbuffer())

            messagebox.showinfo("Éxito", f"Pago registrado. Recibo generado en:\n{path}")
            self.refresh_dashboard(); self.refresh_prestamos(); self.refresh_pagos()
        except Exception as e: messagebox.showerror("Error", str(e))

    def build_pagos(self, parent):
        parent.grid_columnconfigure(0, weight=1)
        parent.grid_rowconfigure(0, weight=1)
        self.tree_pagos = ttk.Treeview(parent, columns=("id", "cliente", "fecha", "valor"), show="headings", style="Pagos.Treeview")
        for c, h in zip(self.tree_pagos["columns"], ("ID Pago", "Cliente", "Fecha", "Valor")):
            self.tree_pagos.heading(c, text=h)
        self.tree_pagos.grid(row=0, column=0, sticky="nsew", padx=10, pady=10)
        self.refresh_pagos()

    def refresh_pagos(self):
        for i in self.tree_pagos.get_children(): self.tree_pagos.delete(i)
        for r in db.listar_pagos(None, _DESKTOP_USER_ID, _DESKTOP_IS_ADMIN):
            self.tree_pagos.insert("", "end", values=(r[0], r[1], r[3], fmt_money(r[4])))


if __name__ == "__main__":
    setup_logging()
    logo = os.path.join(os.path.dirname(__file__), "assets", "logo.png")
    splash = SplashScreen(logo, lambda: App().mainloop())
    splash.mainloop()
