import os
import logging
import time
import json
from datetime import date, datetime, timedelta
from functools import wraps
from io import BytesIO
from itertools import groupby

import psycopg2
import requests
from flask import (
    Flask,
    abort,
    flash,
    redirect,
    render_template,
    request,
    send_file,
    session,
    url_for,
    jsonify,
)
from werkzeug.security import check_password_hash, generate_password_hash

import db
import recibos
from recibos import generar_recibo_imagen, generar_recibo_pdf
from utils_web import (
    add_days,
    fecha_proximo_pago_texto,
    frecuencia_label,
    url_maps,
    url_tel,
    url_whatsapp,
)

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "cambia-esto-en-produccion")

PER_PAGE = 20

# Configurar logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Evolution API config
EVOLUTION_API_URL = os.environ.get("EVOLUTION_API_URL", "http://localhost:8080")
EVOLUTION_API_KEY = os.environ.get("EVOLUTION_API_KEY", "")


def _evolution_headers():
    return {"apikey": EVOLUTION_API_KEY, "Content-Type": "application/json"}


def send_whatsapp_message(instance_name, to_number, message):
    """Envía un mensaje de texto por Evolution API."""
    if not EVOLUTION_API_KEY:
        logger.warning("EVOLUTION_API_KEY no configurada")
        return False
    try:
        resp = requests.post(
            f"{EVOLUTION_API_URL}/message/sendText/{instance_name}",
            json={"number": to_number, "text": message},
            headers=_evolution_headers(),
            timeout=10,
        )
        return resp.status_code == 200
    except Exception as e:
        logger.error(f"Error enviando WhatsApp: {e}")
        return False


def process_whatsapp_message(user_id, instance_id, from_number, message):
    """Máquina de estados para conversación WhatsApp."""
    session = db.get_or_create_session(instance_id, from_number)
    state = session.get("state", "idle")
    context = session.get("context") or {}

    msg = message.lower().strip()

    if msg in ["hola", "inicio", "menu"]:
        db.update_session_state(instance_id, from_number, "idle")
        return (
            "🏦 *Financiera Nuevo Progreso*\n\n"
            "Elige una opción:\n"
            "1️⃣ Consultar saldo\n"
            "2️⃣ Registrar pago\n"
            "3️⃣ Nuevo cliente\n"
            "4️⃣ Nuevo préstamo\n"
            "5️⃣ Ver vencimientos"
        )

    if state == "idle":
        if msg == "1":
            return _consultar_saldo(user_id, from_number)
        elif msg == "2":
            db.update_session_state(instance_id, from_number, "pago_monto")
            return "💰 Ingresa el monto del pago:"
        elif msg == "3":
            db.update_session_state(instance_id, from_number, "cliente_nombre")
            return "👤 Ingresa el nombre del cliente:"
        elif msg == "4":
            db.update_session_state(instance_id, from_number, "prestamo_cliente")
            return "📋 Ingresa la identificación del cliente:"
        elif msg == "5":
            return _ver_vencimientos(user_id)
        else:
            return "Opción no válida. Escribe *menu* para ver opciones."

    if state == "pago_monto":
        try:
            monto = float(msg)
            context["pago_monto"] = monto
            db.update_session_state(instance_id, from_number, "pago_confirmar", json.dumps(context))
            return f"✅ Pago de ${monto:,.0f}. ¿Confirmar? (si/no)"
        except ValueError:
            return "Monto inválido. Ingresa un número."

    if state == "pago_confirmar":
        if msg == "si":
            db.update_session_state(instance_id, from_number, "idle")
            return "✅ Pago registrado exitosamente."
        else:
            db.update_session_state(instance_id, from_number, "idle")
            return "Pago cancelado."

    if state == "cliente_nombre":
        context["cliente_nombre"] = msg
        db.update_session_state(instance_id, from_number, "cliente_identificacion", json.dumps(context))
        return "🆔 Ingresa la identificación:"

    if state == "cliente_identificacion":
        context["cliente_identificacion"] = msg
        db.update_session_state(instance_id, from_number, "cliente_telefono", json.dumps(context))
        return "📞 Ingresa el teléfono:"

    if state == "cliente_telefono":
        context["cliente_telefono"] = msg
        cid = db.get_or_create_cliente(
            context.get("cliente_nombre", ""),
            context.get("cliente_identificacion", ""),
            msg, "", "", user_id
        )
        db.update_session_state(instance_id, from_number, "idle")
        return f"✅ Cliente creado. ID: {cid}"

    if state == "prestamo_cliente":
        context["prestamo_cliente_id"] = msg
        db.update_session_state(instance_id, from_number, "prestamo_monto", json.dumps(context))
        return "💰 Ingresa el monto del préstamo:"

    if state == "prestamo_monto":
        try:
            context["prestamo_monto"] = float(msg)
            db.update_session_state(instance_id, from_number, "prestamo_tasa", json.dumps(context))
            return "📊 Ingresa la tasa de interés (%):"
        except ValueError:
            return "Monto inválido. Ingresa un número."

    if state == "prestamo_tasa":
        try:
            context["prestamo_tasa"] = float(msg)
            db.update_session_state(instance_id, from_number, "prestamo_cuotas", json.dumps(context))
            return "🔢 Ingresa el número de cuotas:"
        except ValueError:
            return "Tasa inválida. Ingresa un número."

    if state == "prestamo_cuotas":
        try:
            cuotas = int(msg)
            monto = context.get("prestamo_monto", 0)
            tasa = context.get("prestamo_tasa", 0)
            interes = monto * (tasa / 100)
            total = monto + interes
            cuota = total / cuotas
            db.update_session_state(instance_id, from_number, "idle")
            return (
                f"📋 *Resumen del préstamo*\n\n"
                f"Monto: ${monto:,.0f}\n"
                f"Interés: ${interes:,.0f}\n"
                f"Total: ${total:,.0f}\n"
                f"Cuota: ${cuota:,.0f} x {cuotas}\n\n"
                f"¿Confirmar? (si/no)"
            )
        except ValueError:
            return "Cuotas inválidas. Ingresa un número."

    return None


def _consultar_saldo(user_id, from_number):
    """Consulta saldos de préstamos activos del cliente."""
    clientes = db.listar_clientes(user_id, True)
    for c in clientes:
        if c[3] and c[3].replace(" ", "") == from_number.replace(" ", ""):
            prestamos = db.listar_prestamos_por_cliente(c[0], user_id, True)
            if not prestamos:
                return "No tienes préstamos activos."
            msg = f"📊 *Tus préstamos:*\n\n"
            for p in prestamos:
                saldo = p['total_pagar'] - db.sum_pagos_por_prestamo(p['id'], user_id, True)
                msg += f"• #{p['id']} - {p['estado']} - Saldo: ${saldo:,.0f}\n"
            return msg
    return "No encontramos tu número registrado. Contacta a tu asesor."


def _ver_vencimientos(user_id):
    """Devuelve préstamos con vencimiento próximo."""
    vencidos = db.get_vencimientos_hoy()
    if not vencidos:
        return "No hay vencimientos para hoy. ✅"
    msg = "📅 *Vencimientos de hoy:*\n\n"
    for v in vencidos:
        msg += f"• {v['nombre']} - ${v['valor_cuota']:,.0f}\n"
    return msg


@app.before_request
def before_request():
    endpoint = request.endpoint or ""
    public_endpoints = {"static", "login", "logout", "setup"}

    if endpoint in {"static", "logout"}:
        return None

    if not app.config.get("DB_SCHEMA_READY"):
        try:
            db.ensure_schema_migrations()
        except Exception as e:
            print(f"--- ERROR DB SCHEMA: {e} ---")
        app.config["DB_SCHEMA_READY"] = True

    if endpoint in public_endpoints:
        return None

    uid = session.get("user_id")
    if not uid:
        return redirect(url_for("login", next=request.path))

    try:
        if "rol" not in session or "is_admin" not in session or "username" not in session:
            row = db.obtener_usuario_por_id(int(uid))
            if not row or not row.get("activo", True):
                session.clear()
                flash("Tu sesión ya no está activa. Inicia sesión nuevamente.", "error")
                return redirect(url_for("login"))
            session["user_id"] = row.get("id")
            session["username"] = row.get("username")
            session["rol"] = row.get("rol") or "usuario"
            session["is_admin"] = session["rol"] == "admin"
    except Exception as e:
        print(f"--- ERROR BEFORE REQUEST: {e} ---")
        session.clear()
        return redirect(url_for("login"))

    if endpoint.startswith("admin_") and not session.get("is_admin", False):
        abort(403)

    return None


def _rango_periodo_dashboard(periodo: str) -> tuple[str, str, str]:
    """Devuelve (fecha_ini, fecha_fin, etiqueta)."""
    hoy = date.today()
    p = (periodo or "hoy").lower().strip()
    if p == "ayer":
        d = hoy - timedelta(days=1)
        s = d.isoformat()
        return s, s, "Ayer"
    if p in ("7d", "7", "semana"):
        ini = hoy - timedelta(days=6)
        return ini.isoformat(), hoy.isoformat(), "Últimos 7 días"
    if p in ("mes", "mes_actual"):
        ini = hoy.replace(day=1)
        return ini.isoformat(), hoy.isoformat(), "Este mes"
    s = hoy.isoformat()
    return s, s, "Hoy"


def fmt_money(valor):
    try:
        return f"${float(valor):,.0f}"
    except (TypeError, ValueError):
        return "$0"


def today_str():
    return datetime.now().strftime("%Y-%m-%d")


def _parse_fecha_iso(val):
    if val is None or str(val).strip() == "":
        return None
    try:
        return datetime.strptime(str(val).strip()[:10], "%Y-%m-%d")
    except ValueError:
        return None


def ctx_user():
    uid = session.get("user_id")
    if not uid:
        return None, None, False, "solo_lectura"
    rol = session.get("rol", "solo_lectura")
    is_admin = session.get("is_admin")
    if is_admin is None:
        is_admin = rol == "admin"
    return int(uid), session.get("username", ""), bool(is_admin), rol


def require_role(roles):
    def decorator(f):
        @wraps(f)
        def decorated_function(*args, **kwargs):
            if 'user_id' not in session:
                return redirect(url_for('login', next=request.path))
            if session.get('rol') not in roles and not session.get('is_admin'):
                flash("No tienes permiso para acceder a esta sección.", "error")
                return redirect(url_for('inicio'))
            return f(*args, **kwargs)
        return decorated_function
    return decorator


def login_required(f):
    @wraps(f)
    def w(*args, **kwargs):
        if not session.get("user_id"):
            return redirect(url_for("login", next=request.path))
        return f(*args, **kwargs)
    return w


def admin_required(f):
    @wraps(f)
    def w(*args, **kwargs):
        if not session.get("user_id"):
            return redirect(url_for("login", next=request.path))
        if not session.get("is_admin"):
            abort(403)
        return f(*args, **kwargs)
    return w


@app.context_processor
def inject_globals():
    _, __, is_admin, rol = ctx_user()
    return {
        "fmt_money": fmt_money,
        "today_str": today_str,
        "is_admin": is_admin,
        "rol": rol,
        "fecha_proximo_pago_texto": fecha_proximo_pago_texto,
        "frecuencia_label": frecuencia_label,
        "url_tel": url_tel,
        "url_whatsapp": url_whatsapp,
        "url_maps": url_maps,
    }


@app.route("/setup", methods=["GET", "POST"])
def setup():
    try:
        total = db.count_usuarios()
        db_error = False
    except Exception:
        total = 0
        db_error = True
        
    if not db_error and total > 0:
        return redirect(url_for("login"))
        
    if request.method == "POST":
        u = request.form.get("username", "").strip()
        p1 = request.form.get("password", "")
        p2 = request.form.get("password2", "")
        if len(u) < 3:
            flash("El usuario debe tener al menos 3 caracteres.", "error")
        elif len(p1) < 6:
            flash("La clave debe tener al menos 6 caracteres.", "error")
        elif p1 != p2:
            flash("Las claves no coinciden.", "error")
        else:
            h = generate_password_hash(p1)
            db.crear_usuario(u, h, rol="admin")
            flash("Administrador creado. Inicia sesión.", "ok")
            return redirect(url_for("login"))
    return render_template("setup.html")


@app.route("/login", methods=["GET", "POST"])
def login():
    try:
        total = db.count_usuarios()
        db_error = False
    except Exception:
        total = 0
        db_error = True
    
    if not db_error and total == 0:
        return redirect(url_for("setup"))
        
    if request.method == "POST":
        try:
            u = (request.form.get("username") or "").strip()
            p = (request.form.get("password") or "")
            ip = request.remote_addr or "127.0.0.1"

            if not u or not p:
                flash("Por favor, ingresa usuario y contraseña.", "error")
                return render_template("login.html")

            db.cleanup_old_attempts()
            failed = db.get_failed_attempts(ip)
            if failed >= 5:
                flash("Demasiados intentos fallidos. Espera 15 minutos antes de intentar de nuevo.", "error")
                return render_template("login.html")

            row = db.obtener_usuario_por_username(u)

            if not row:
                db.record_login_attempt(ip, u)
                flash("Usuario o clave incorrectos.", "error")
            elif not row.get('activo', True):
                db.record_login_attempt(ip, u)
                flash("Cuenta desactivada. Contacta al administrador.", "error")
            elif not check_password_hash(row.get('password_hash', ''), p):
                db.record_login_attempt(ip, u)
                flash("Usuario o clave incorrectos.", "error")
            else:
                session.clear()
                session["user_id"] = row.get('id')
                session["username"] = row.get('username')
                session["rol"] = row.get('rol')
                session["is_admin"] = (row.get('rol') == "admin")

                db.registrar_log(session["user_id"], "Inicio de sesión")

                if row.get('debe_cambiar_password'):
                    flash("Debes cambiar tu contraseña inicial por seguridad.", "error")
                    return redirect(url_for("cambiar_password"))

                nxt = request.args.get("next") or url_for("inicio")
                return redirect(nxt)
        except Exception as e:
            print(f"--- ERROR CRÍTICO EN LOGIN: {e} ---")
            flash("Error interno del servidor. Inténtalo de nuevo.", "error")
            return render_template("login.html")

    return render_template("login.html")


@app.route("/admin/dashboard")
@admin_required
def admin_dashboard():
    stats = db.obtener_metricas_globales()
    logs = db.obtener_logs_recientes(20)
    return render_template("admin_dashboard.html", stats=stats, logs=logs)


@app.route("/admin/usuarios_full")
@admin_required
def admin_usuarios_list():
    users = db.listar_usuarios_con_estadisticas()
    return render_template("admin_usuarios_list.html", users=users)


@app.route("/admin/usuarios/<int:uid>/detalle")
@admin_required
def admin_usuario_detalle(uid):
    user = db.obtener_usuario_por_id(uid)
    if not user: abort(404)

    # Obtenemos datos del usuario (actuamos como si fuéramos él para reutilizar funciones)
    clientes = db.listar_clientes(uid, False)
    prestamos = db.listar_prestamos("", (), uid, False)
    pagos = db.listar_pagos(None, uid, False)
    logs = db.obtener_logs_recientes(30, user_id=uid)

    return render_template(
        "admin_usuario_detalle.html",
        user=user,
        clientes=clientes,
        prestamos=prestamos,
        pagos=pagos,
        logs=logs
    )


@app.route("/admin/usuarios/<int:uid>/backup")
@admin_required
def admin_backup_user(uid):
    """Descarga backup JSON de un usuario."""
    data = db.export_user_data(uid)
    buf = BytesIO(json.dumps(data, indent=2, default=str).encode("utf-8"))
    buf.seek(0)
    return send_file(
        buf,
        as_attachment=True,
        download_name=f"backup_usuario_{uid}_{today_str()}.json",
        mimetype="application/json",
    )


@app.route("/admin/usuarios/<int:uid>/restore", methods=["POST"])
@admin_required
def admin_restore_user(uid):
    """Restaura datos de un usuario desde un archivo JSON."""
    f = request.files.get("backup_file")
    if not f:
        flash("Selecciona un archivo de backup.", "error")
        return redirect(url_for("admin_usuarios_list"))
    
    try:
        data = json.loads(f.read())
        if data.get("user_id") != uid:
            flash("El backup no corresponde a este usuario.", "error")
            return redirect(url_for("admin_usuarios_list"))
        
        db.restore_user_data(uid, data)
        db.registrar_log(session["user_id"], f"Restauración de usuario #{uid}")
        flash(f"Usuario #{uid} restaurado correctamente.", "ok")
    except Exception as e:
        flash(f"Error al restaurar: {e}", "error")
    
    return redirect(url_for("admin_usuarios_list"))


@app.route("/cambiar_password", methods=["GET", "POST"])
def cambiar_password():
    if 'user_id' not in session:
        return redirect(url_for("login"))

    if request.method == "POST":
        p1 = request.form.get("p1", "").strip()
        p2 = request.form.get("p2", "").strip()

        if len(p1) < 8:
            flash("La contraseña debe tener al menos 8 caracteres.", "error")
        elif p1 != p2:
            flash("Las contraseñas no coinciden.", "error")
        else:
            h = generate_password_hash(p1)
            # Usamos la nueva función que limpia el flag
            db.completar_cambio_password(session['user_id'], h)
            flash("Contraseña actualizada correctamente.", "ok")
            return redirect(url_for("inicio"))

    return render_template("cambiar_password.html")


@app.route("/logout")
def logout():
    """Limpia la sesión y redirige al login."""
    session.clear()
    flash("Has cerrado sesión correctamente.", "ok")
    return redirect(url_for("login"))


@app.route("/inicio")
@login_required
def inicio():
    """Dashboard principal con gráficas y métricas."""
    uid, _, is_admin, _ = ctx_user()
    periodo = request.args.get("periodo", "hoy")
    try:
        stats = db.obtener_stats_dashboard(uid, is_admin, periodo)
    except Exception as e:
        print(f"--- ERROR EN INICIO: {e} ---")
        stats = {
            "total_prestado": 0, "capital_cobrado": 0, "interes_cobrado": 0,
            "mora_cobrada": 0, "ganancia_neta": 0, "total_cobrado": 0,
            "activos": 0, "en_mora": 0, "pagados": 0, "periodo": periodo,
        }
    return render_template("inicio.html", stats=stats)


@app.route("/api/buscar_clientes")
@login_required
def api_buscar_clientes():
    q = request.args.get('q', '').strip()
    if not q:
        return jsonify([])
    uid, _, is_admin, _ = ctx_user()
    results = db.buscar_clientes_ajax(q, uid, is_admin)
    return jsonify(results)


@app.route("/clientes")
@login_required
def clientes_list():
    """Restricción: El admin no gestiona clientes."""
    try:
        if session.get('rol') == 'admin':
            return redirect(url_for('admin_usuarios'))

        uid, _, is_admin, _ = ctx_user()
        filtro = request.args.get("estado", "todo")
        page = request.args.get("page", 1, type=int)
        rows = db.listar_clientes_filtrado(filtro, uid, is_admin)
        total = len(rows)
        start = (page - 1) * PER_PAGE
        end = start + PER_PAGE
        paginated = rows[start:end]
        return render_template(
            "clientes.html",
            clientes=paginated,
            filtro=filtro,
            page=page,
            total_pages=(total + PER_PAGE - 1) // PER_PAGE,
            total=total,
        )
    except Exception as e:
        logger.exception("Error en ruta /clientes")
        flash("Error interno del servidor.", "error")
        return redirect(url_for("inicio"))


@app.route("/clientes/nuevo", methods=["GET", "POST"])
@require_role(['admin', 'cobrador'])
def clientes_nuevo():
    uid, _, is_admin, _ = ctx_user()
    if request.method == "POST":
        try:
            nombre = request.form.get("nombre", "").strip()
            identificacion = request.form.get("identificacion", "").strip()
            telefono = request.form.get("telefono", "").strip()
            barrio = request.form.get("barrio", "").strip()
            direccion = request.form.get("direccion", "").strip()

            if not nombre or not identificacion:
                raise ValueError("Nombre e identificación son obligatorios.")

            cid = db.get_or_create_cliente(
                nombre,
                identificacion,
                telefono,
                barrio,
                direccion,
                uid,
            )

            fecha = request.form.get("fecha", today_str())
            freq = request.form.get("frecuencia", "mensual").lower().strip()
            if freq not in ("diaria", "semanal", "quincenal", "mensual"):
                raise ValueError("Frecuencia inválida.")

            cuotas = int(request.form.get("cuotas", "1"))
            monto = float(request.form.get("monto", "0"))
            tasa = float(request.form.get("tasa", "0"))

            if cuotas < 1:
                raise ValueError("El número de cuotas debe ser mayor o igual a 1.")
            if monto <= 0:
                raise ValueError("El monto debe ser mayor a 0.")
            if tasa < 0:
                raise ValueError("La tasa no puede ser negativa.")

            interes = monto * (tasa / 100.0)
            total = monto + interes
            cuota = total / cuotas
            dias = {"diaria": 1, "semanal": 7, "quincenal": 15, "mensual": 30}[freq]
            venc = add_days(fecha, dias * cuotas)
            mora_on = request.form.get("mora_activa") == "on"
            tasa_mora = float(request.form.get("tasa_mora_diaria", "0") or 0)
            if mora_on and tasa_mora < 0:
                raise ValueError("La tasa de mora no puede ser negativa.")

            db.nuevo_prestamo(
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
                uid,
                is_admin,
                mora_activa=mora_on,
                tasa_mora_diaria=tasa_mora,
            )
            db.registrar_log(uid, f"Nuevo cliente ({nombre}) y préstamo de {fmt_money(monto)}")
            flash("Cliente y préstamo guardados.", "ok")
            return redirect(url_for("clientes_list"))
        except Exception as e:
            flash(str(e), "error")
            return render_template(
                "cliente_form.html",
                cliente=None,
                crear_prestamo_junto=True,
                form_data=request.form,
                hoy=today_str(),
            )
    return render_template(
        "cliente_form.html",
        cliente=None,
        crear_prestamo_junto=True,
        form_data={},
        hoy=today_str(),
    )


@app.route("/clientes/<int:cid>/perfil", methods=["GET", "POST"])
@login_required
def clientes_perfil(cid):
    uid, _, is_admin, rol = ctx_user()
    row = db.obtener_cliente(cid, uid, is_admin)
    if not row:
        abort(404)
    if request.method == "POST":
        if rol == 'solo_lectura':
            abort(403)
        if request.form.get("accion") != "guardar_datos":
            abort(400)
        db.actualizar_cliente(
            cid,
            request.form.get("nombre", "").strip(),
            request.form.get("identificacion", "").strip(),
            request.form.get("telefono", "").strip(),
            request.form.get("barrio", "").strip(),
            request.form.get("direccion", "").strip(),
            uid,
            is_admin,
        )
        flash("Información personal actualizada.", "ok")
        return redirect(url_for("clientes_perfil", cid=cid))

    prestamos_rows = db.listar_prestamos_por_cliente(cid, uid, is_admin)
    prestamos_view = []
    for p in prestamos_rows:
        pid = p['id']
        saldo = p['total_pagar'] - db.sum_pagos_por_prestamo(pid, uid, is_admin)
        prestamos_view.append(
            {
                "id": pid,
                "monto": p['monto'],
                "total_pagar": p['total_pagar'],
                "pagadas": p['pagadas'],
                "cuotas": p['cuotas'],
                "proximo_pago": p['proximo_pago'] or "",
                "estado": p['estado'],
                "saldo": max(0.0, round(float(saldo), 2)),
                "en_mora": p['en_mora']
            }
        )
    auditoria = []
    if prestamos_view:
        auditoria = db.listar_auditoria_prestamo(prestamos_view[0]["id"], uid, is_admin)
    return render_template(
        "cliente_perfil.html",
        cliente=row,
        prestamos=prestamos_view,
        is_admin=is_admin,
        auditoria=auditoria,
    )


@app.route("/clientes/<int:cid>/editar", methods=["GET", "POST"])
@require_role(['admin', 'cobrador'])
def clientes_editar(cid):
    if request.method == "GET":
        return redirect(url_for("clientes_perfil", cid=cid))
    uid, _, is_admin, _ = ctx_user()
    row = db.obtener_cliente(cid, uid, is_admin)
    if not row:
        abort(404)
    db.actualizar_cliente(
        cid,
        request.form.get("nombre", "").strip(),
        request.form.get("identificacion", "").strip(),
        request.form.get("telefono", "").strip(),
        request.form.get("barrio", "").strip(),
        request.form.get("direccion", "").strip(),
        uid,
        is_admin,
    )
    flash("Cliente actualizado.", "ok")
    return redirect(url_for("clientes_perfil", cid=cid))


@app.route("/clientes/<int:cid>/eliminar", methods=["POST"])
@require_role(['admin'])
def clientes_eliminar(cid):
    uid, _, is_admin, _ = ctx_user()
    if db.eliminar_cliente_y_todo(cid, uid, is_admin):
        flash("Cliente y su historial eliminados.", "ok")
    else:
        flash("No se pudo eliminar.", "error")
    return redirect(url_for("clientes_list"))


@app.route("/clientes/<int:cid>/foto", methods=["POST"])
@require_role(['admin', 'cobrador'])
def subir_foto_cliente(cid):
    from flask import jsonify
    import base64
    import io
    from PIL import Image
    
    uid, _, is_admin, _ = ctx_user()
    foto_file = request.files.get("foto")
    if not foto_file or not foto_file.filename:
        return jsonify({"ok": False, "error": "No hay archivo"})
    
    filename = foto_file.filename.lower()
    allowed = (".jpg", ".jpeg", ".png", ".webp")
    if not any(filename.endswith(ext) for ext in allowed):
        return jsonify({"ok": False, "error": "Extensión no permitida"})
    
    foto_file.seek(0, 2)
    size = foto_file.tell()
    foto_file.seek(0)
    if size > 2 * 1024 * 1024:
        return jsonify({"ok": False, "error": "Máximo 2MB"})
    
    try:
        img = Image.open(foto_file.stream)
        if img.mode != "RGB":
            img = img.convert("RGB")
        img.thumbnail((400, 400))
        buffer = io.BytesIO()
        img.save(buffer, format="JPEG", quality=85)
        foto_b64 = "data:image/jpeg;base64," + base64.b64encode(buffer.getvalue()).decode("utf-8")
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)})
    
    ok = db.actualizar_foto_cliente(cid, foto_b64, uid, is_admin)
    return jsonify({"ok": ok, "foto": foto_b64 if ok else None})


@app.route("/cuotas/vencidas")
@login_required
def cuotas_vencidas():
    uid, _, is_admin, _ = ctx_user()
    rows = db.listar_cuotas_vencidas(uid, is_admin)
    return render_template("cuotas_vencidas.html", rows=rows)


@app.route("/cuotas/vencer")
@login_required
def cuotas_vencer():
    uid, _, is_admin, _ = ctx_user()
    rows = db.listar_cuotas_vencer(uid, is_admin)
    return render_template("cuotas_vencer.html", rows=rows)


@app.route("/prestamos")
@login_required
def prestamos_list():
    """Restricción: El admin no gestiona préstamos."""
    if session.get('rol') == 'admin':
        return redirect(url_for('admin_usuarios'))

    uid, _, is_admin, _ = ctx_user()
    filtro = request.args.get("estado", "activos")
    page = request.args.get("page", 1, type=int)

    where = ""
    params = ()

    try:
        if filtro == "activos":
            where, params = "p.estado = %s", ("ACTIVO",)
        elif filtro == "pagados":
            where, params = "p.estado = %s", ("PAGADO",)
        elif filtro == "mora":
            where = "p.estado = 'ACTIVO' AND p.proximo_pago IS NOT NULL AND p.proximo_pago <> '' AND p.proximo_pago::date < CURRENT_DATE"
            params = ()

        rows = db.listar_prestamos(where, params, uid, is_admin)
        total = len(rows)
        start = (page - 1) * PER_PAGE
        end = start + PER_PAGE
        paginated = rows[start:end]
        return render_template(
            "prestamos.html",
            prestamos=paginated,
            filtro=filtro,
            page=page,
            total_pages=(total + PER_PAGE - 1) // PER_PAGE,
            total=total,
        )

    except Exception as e:
        # Logging del error para debug
        print(f"Error en /prestamos: {e}")
        flash("Ocurrió un error al cargar la lista de préstamos.", "error")
        return redirect(url_for("inicio"))


@app.route("/prestamos/nuevo", methods=["GET", "POST"])
@require_role(['admin', 'cobrador'])
def prestamos_nuevo():
    uid, _, is_admin, _ = ctx_user()
    clientes = db.listar_clientes(uid, is_admin)
    if not clientes:
        flash("Crea al menos un cliente antes de un préstamo.", "error")
        return redirect(url_for("clientes_nuevo"))
    if request.method == "POST":
        try:
            cid = int(request.form.get("cliente_id", "0"))
            fecha = request.form.get("fecha", today_str())
            freq = request.form.get("frecuencia", "mensual").lower()
            cuotas = int(request.form.get("cuotas", "1"))
            monto = float(request.form.get("monto", "0"))
            tasa = float(request.form.get("tasa", "0"))
            interes = monto * (tasa / 100.0)
            total = monto + interes
            cuota = total / max(1, cuotas)
            dias = {"diaria": 1, "semanal": 7, "quincenal": 15, "mensual": 30}.get(freq, 30)
            venc = add_days(fecha, dias * cuotas)
            mora_on = request.form.get("mora_activa") == "on"
            tasa_mora = float(request.form.get("tasa_mora_diaria", "0") or 0)
            if mora_on and tasa_mora < 0:
                raise ValueError("La tasa de mora no puede ser negativa.")
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
                uid,
                is_admin,
                mora_activa=mora_on,
                tasa_mora_diaria=tasa_mora,
            )
            cliente_nombre = next((c[1] for c in clientes if c[0] == cid), "desconocido")
            db.registrar_log(uid, f"Nuevo préstamo #{pid} para {cliente_nombre} por {fmt_money(monto)}")
            flash("Préstamo creado.", "ok")
            return redirect(url_for("prestamos_list"))
        except Exception as e:
            flash(str(e), "error")
    return render_template("prestamo_nuevo.html", clientes=clientes)


@app.route("/prestamos/<int:pid>/editar", methods=["GET", "POST"])
@require_role(['admin', 'cobrador'])
def prestamos_editar(pid):
    uid, _, is_admin, _ = ctx_user()
    info = db.obtener_prestamo(pid, uid, is_admin)
    if not info:
        abort(404)
    cid = int(info[1])
    if str(info[13]).upper() != "ACTIVO":
        flash("Solo se pueden editar préstamos activos.", "error")
        return redirect(url_for("clientes_perfil", cid=cid))

    if request.method == "POST":
        try:
            fecha = request.form.get("fecha", today_str())
            freq = request.form.get("frecuencia", "mensual").lower().strip()
            if freq not in ("diaria", "semanal", "quincenal", "mensual"):
                raise ValueError("Frecuencia inválida.")
            cuotas = int(request.form.get("cuotas", "1"))
            monto = float(request.form.get("monto", "0"))
            tasa = float(request.form.get("tasa", "0"))
            vencimiento = request.form.get("vencimiento", "").strip()
            if not vencimiento:
                raise ValueError("Indica la fecha de vencimiento.")
            if monto <= 0 or cuotas < 1:
                raise ValueError("Monto y cuotas deben ser válidos.")
            mora_on = request.form.get("mora_activa") == "on"
            tasa_mora = float(request.form.get("tasa_mora_diaria", "0") or 0)
            if mora_on and tasa_mora < 0:
                raise ValueError("La tasa de mora no puede ser negativa.")
            ok = db.editar_prestamo_inteligente(
                pid,
                fecha,
                freq,
                cuotas,
                monto,
                tasa,
                vencimiento,
                uid,
                is_admin,
                mora_activa=mora_on,
                tasa_mora_diaria=tasa_mora,
            )
            if not ok:
                flash("No se pudo actualizar el préstamo.", "error")
            else:
                flash(
                    "Préstamo actualizado inteligentemente. Se ajustó sobre el saldo restante; los pagos anteriores se conservan.",
                    "ok",
                )
            return redirect(url_for("clientes_perfil", cid=cid))
        except ValueError as e:
            flash(str(e), "error")
            return render_template("prestamo_editar.html", p=info, form_data=dict(request.form))
        except Exception as e:
            flash(str(e), "error")
            return render_template("prestamo_editar.html", p=info, form_data=dict(request.form))

    return render_template("prestamo_editar.html", p=info, form_data=None)


@app.route("/prestamos/<int:pid>/eliminar", methods=["POST"])
@require_role(['admin', 'cobrador'])
def prestamos_eliminar(pid):
    uid, _, is_admin, _ = ctx_user()
    info = db.obtener_prestamo(pid, uid, is_admin)
    if not info:
        abort(404)
    cid = int(info[1])
    if db.eliminar_prestamo(pid, uid, is_admin):
        db.registrar_log(uid, f"Préstamo #{pid} eliminado para cliente {info[2]}")
        flash("Préstamo eliminado con todos sus pagos.", "ok")
    else:
        flash("No se pudo eliminar el préstamo.", "error")
    return redirect(url_for("clientes_perfil", cid=cid))


@app.route("/prestamos/<int:pid>/cobrar")
@require_role(['admin', 'cobrador'])
def prestamos_cobrar(pid):
    uid, _, is_admin, _ = ctx_user()
    info = db.obtener_prestamo(pid, uid, is_admin)
    if not info or info[13] != "ACTIVO":
        abort(404)
    fecha = (request.args.get("fecha") or "").strip() or today_str()
    valor_cuota = float(info[11])
    prox = info[15]
    mora_act = bool(info[17])
    tasa_m = float(info[18] or 0)
    interes_mora = db.calcular_interes_mora(valor_cuota, prox, fecha, mora_act, tasa_m)
    total_sugerido = round(valor_cuota + interes_mora, 2)
    pagadas = info[14] or 0
    num_cuota = int(pagadas) + 1
    telefono = info[16] if len(info) > 16 else ""
    return render_template(
        "prestamos_cobrar.html",
        pid=pid,
        nombre=info[2],
        fecha=fecha,
        valor_cuota=valor_cuota,
        interes_mora=interes_mora,
        total_sugerido=total_sugerido,
        proximo_pago=prox or "",
        mora_activa=mora_act,
        telefono=telefono,
        num_cuota=num_cuota,
    )


@app.route("/prestamos/<int:pid>/pago", methods=["POST"])
@require_role(['admin', 'cobrador'])
def prestamos_pago(pid):
    uid, _, is_admin, _ = ctx_user()
    try:
        valor = float(request.form.get("valor", "0"))
        fecha = request.form.get("fecha", today_str())
        nota = request.form.get("nota", "").strip()
        pago_id, num_cuota, interes_mora, valor_cuota_base = db.registrar_pago(
            pid, fecha, valor, uid, is_admin, nota
        )
        db.registrar_log(uid, f"Pago registrado de {fmt_money(valor)} para préstamo #{pid}")
        session["_ultimo_pago"] = {
            "pid": pid,
            "pago_id": pago_id,
            "num_cuota": num_cuota,
            "valor": valor,
            "fecha": fecha,
            "interes_mora": interes_mora,
            "valor_cuota_base": valor_cuota_base,
        }
        info = db.obtener_prestamo(pid, uid, is_admin)
        if not info:
            raise ValueError("No se encontró el préstamo después de registrar el pago.")
        nombre = info[2]
        telefono = info[16] if len(info) > 16 else ""
        wa_url = url_whatsapp(telefono, nombre, valor, num_cuota) if telefono else ""
        flash("Pago registrado.", "ok")
        return render_template(
            "pago_exito.html",
            pid=pid,
            nombre=nombre,
            telefono=telefono,
            wa_url=wa_url,
            valor=valor,
            num_cuota=num_cuota,
            pdf_url=url_for("descargar_recibo", pid=pid, pago_id=pago_id),
        )
    except Exception as e:
        flash(str(e), "error")
        return redirect(url_for("prestamos_list"))


@app.route("/reportes")
@login_required
def reportes():
    uid, _, is_admin, _ = ctx_user()
    periodo = request.args.get("periodo", "hoy")
    f_ini, f_fin, periodo_etiqueta = _rango_periodo_dashboard(periodo)
    total_prestado = db.total_prestado_en_rango(f_ini, f_fin, uid, is_admin)
    total_cobrado = db.total_cobrado_en_rango(f_ini, f_fin, uid, is_admin)
    mora_cobrada = db.total_mora_cobrada_en_rango(f_ini, f_fin, uid, is_admin)
    capital_cobrado, interes_cobrado = db.desglose_capital_interes_cobrado_en_rango(
        f_ini, f_fin, uid, is_admin
    )
    ganancia_neta = interes_cobrado + mora_cobrada
    activos = db.contar_prestamos_activos(uid, is_admin)
    en_mora = db.contar_prestamos_en_mora(uid, is_admin)
    pagos_detalle = db.pagos_detalle_en_rango(f_ini, f_fin, uid, is_admin)
    chart_data = {
        "labels": [
            "Ganancia neta",
            "Total prestado",
            "Capital cobrado",
            "Interés cobrado",
            "Mora cobrada",
        ],
        "values": [
            round(ganancia_neta, 2),
            round(total_prestado, 2),
            round(capital_cobrado, 2),
            round(interes_cobrado, 2),
            round(mora_cobrada, 2),
        ],
    }
    return render_template(
        "reportes.html",
        periodo=periodo,
        periodo_etiqueta=periodo_etiqueta,
        f_ini=f_ini,
        f_fin=f_fin,
        total_prestado=total_prestado,
        total_cobrado=total_cobrado,
        capital_cobrado=capital_cobrado,
        interes_cobrado=interes_cobrado,
        mora_cobrada=mora_cobrada,
        ganancia_neta=ganancia_neta,
        activos=activos,
        en_mora=en_mora,
        pagos_detalle=pagos_detalle,
        chart_data=chart_data,
    )


@app.route("/reportes/pdf")
@login_required
def reportes_pdf():
    uid, _, is_admin, _ = ctx_user()
    periodo = request.args.get("periodo", "hoy")
    f_ini, f_fin, periodo_etiqueta = _rango_periodo_dashboard(periodo)
    total_prestado = db.total_prestado_en_rango(f_ini, f_fin, uid, is_admin)
    total_cobrado = db.total_cobrado_en_rango(f_ini, f_fin, uid, is_admin)
    mora_cobrada = db.total_mora_cobrada_en_rango(f_ini, f_fin, uid, is_admin)
    capital_cobrado, interes_cobrado = db.desglose_capital_interes_cobrado_en_rango(
        f_ini, f_fin, uid, is_admin
    )
    ganancia_neta = interes_cobrado + mora_cobrada
    activos = db.contar_prestamos_activos(uid, is_admin)
    en_mora = db.contar_prestamos_en_mora(uid, is_admin)
    pagos_detalle = db.pagos_detalle_en_rango(f_ini, f_fin, uid, is_admin)
    buf = recibos.generar_reporte_vision_pdf(
        periodo_etiqueta,
        f_ini,
        f_fin,
        total_prestado,
        capital_cobrado,
        interes_cobrado,
        mora_cobrada,
        ganancia_neta,
        total_cobrado,
        activos,
        en_mora,
        pagos_detalle,
    )
    safe = f"{f_ini}_{f_fin}".replace("/", "-")
    return send_file(
        buf,
        as_attachment=True,
        download_name=f"reporte_financiera_{safe}.pdf",
        mimetype="application/pdf",
    )


@app.route("/pagos")
@login_required
def pagos_list():
    """Restricción: El admin no gestiona pagos."""
    if session.get('rol') == 'admin':
        return redirect(url_for('admin_usuarios'))

    uid, _, is_admin, _ = ctx_user()
    prestamo_filtro = request.args.get("prestamo_id", default=None, type=int)
    page = request.args.get("page", 1, type=int)
    try:
        rows = db.listar_pagos(prestamo_filtro, uid, is_admin)
    except Exception as e:
        flash(f"No se pudo cargar el historial de pagos: {e}", "error")
        rows = []
    total = len(rows)
    start = (page - 1) * PER_PAGE
    end = start + PER_PAGE
    paginated_rows = rows[start:end]
    pagos = [
        {
            "id": r[0],
            "cliente": r[1] or "Sin nombre",
            "prestamo_id": r[2],
            "fecha": str(r[3] or ""),
            "valor": float(r[4] or 0),
            "cuota": int(r[5] or 0),
            "saldo_restante": float(r[6] or 0),
            "interes_mora": float(r[11] or 0),
            "nota": str(r[12] or "").strip(),
        }
        for r in paginated_rows
    ]
    grupos = [(fecha, list(items)) for fecha, items in groupby(pagos, key=lambda r: r["fecha"])]
    return render_template(
        "pagos.html",
        pagos_grupos=grupos,
        filtro_prestamo_id=prestamo_filtro,
        page=page,
        total_pages=(total + PER_PAGE - 1) // PER_PAGE,
        total=total,
    )


@app.route("/pagos/<int:pago_id>/eliminar", methods=["POST"])
@require_role(['admin'])
def pagos_eliminar(pago_id):
    uid, _, is_admin, _ = ctx_user()
    prestamo_id = int(request.form.get("prestamo_id", "0"))
    if db.eliminar_pago_y_actualizar(prestamo_id, pago_id, uid, is_admin):
        flash("Pago eliminado.", "ok")
    else:
        flash("No se pudo eliminar el pago.", "error")
    if prestamo_id:
        return redirect(url_for("pagos_list", prestamo_id=prestamo_id))
    return redirect(url_for("pagos_list"))


@app.route("/configuracion", methods=["GET", "POST"])
@login_required
def configuracion():
    uid, username, is_admin, _ = ctx_user()
    if request.method == "POST":
        action = request.form.get("accion", "")
        if action == "cambiar_usuario":
            nuevo = request.form.get("nuevo_usuario", "").strip()
            confirmar = request.form.get("confirmar_usuario", "").strip()
            if len(nuevo) < 3:
                flash("El usuario debe tener al menos 3 caracteres.", "error")
            elif nuevo != confirmar:
                flash("Los nombres de usuario no coinciden.", "error")
            else:
                exist = db.obtener_usuario_por_username(nuevo)
                if exist and exist['id'] != uid:
                    flash("Ese nombre de usuario ya está en uso.", "error")
                else:
                    db.actualizar_username_usuario(uid, nuevo)
                    session["username"] = nuevo
                    flash("Usuario actualizado.", "ok")
                    return redirect(url_for("configuracion"))
        elif action == "cambiar_password":
            actual = request.form.get("password_actual", "")
            n1 = request.form.get("password_nueva", "")
            n2 = request.form.get("password_nueva2", "")
            row = db.obtener_usuario_por_id(uid)
            if not row or not check_password_hash(row['password_hash'], actual):
                flash("La clave actual no es correcta.", "error")
            elif len(n1) < 6:
                flash("La nueva clave debe tener al menos 6 caracteres.", "error")
            elif n1 != n2:
                flash("Las claves nuevas no coinciden.", "error")
            else:
                db.actualizar_password_usuario(uid, generate_password_hash(n1))
                flash("Clave actualizada.", "ok")
                return redirect(url_for("configuracion"))
    row = db.obtener_usuario_por_id(uid)
    return render_template("configuracion.html", username_actual=username)


@app.route("/admin/usuarios", methods=["GET", "POST"])
@admin_required
def admin_usuarios():
    if request.method == "POST":
        # Lógica para Crear Usuario
        u = request.form.get("username", "").strip()
        p = request.form.get("password", "")
        r = request.form.get("rol", "cobrador")

        if db.obtener_usuario_por_username(u):
            flash("El nombre de usuario ya existe.", "error")
        else:
            h = generate_password_hash(p)
            db.crear_usuario(u, h, rol=r)
            flash(f"Usuario {u} creado correctamente.", "ok")
        return redirect(url_for('admin_usuarios'))

    usuarios = db.listar_usuarios_admin()
    return render_template("admin_usuarios.html", usuarios=usuarios)


@app.route("/admin/usuarios/<int:uid>/editar", methods=["GET", "POST"])
@admin_required
def admin_usuario_editar(uid):
    user = db.obtener_usuario_por_id(uid)
    if not user: abort(404)

    if request.method == "POST":
        new_u = request.form.get("username", "").strip()
        new_r = request.form.get("rol")
        db.admin_update_user_basic(uid, new_u, new_r)
        flash("Datos actualizados correctamente.", "ok")
        return redirect(url_for('admin_usuarios'))

    return render_template("admin_usuario_editar.html", u=user)


@app.route("/admin/usuarios/<int:uid>/password", methods=["POST"])
@admin_required
def admin_usuario_pass_update(uid):
    user = db.obtener_usuario_por_id(uid)
    if not user:
        flash("Usuario no encontrado.", "error")
        return redirect(url_for('admin_usuarios'))
    
    new_p = request.form.get("new_password", "")
    if len(new_p) < 8:
        flash("La contraseña debe tener al menos 8 caracteres.", "error")
    else:
        h = generate_password_hash(new_p)
        db.admin_update_user_password(uid, h)
        db.registrar_log(session["user_id"], f"Contraseña actualizada para usuario #{uid}")
        flash("Contraseña actualizada con éxito.", "ok")
    return redirect(url_for('admin_usuarios'))


@app.route("/admin/usuarios/<int:uid>/toggle", methods=["POST"])
@admin_required
def admin_usuario_toggle(uid):
    if uid == session['user_id']:
        flash("No puedes desactivar tu propia cuenta.", "error")
    else:
        # Reutilizamos la lógica de toggle pero mapeada a la nueva estructura si es necesario
        # En este caso db.py tenía una versión que borramos, vamos a restaurar la necesaria
        with db.get_conn() as conn:
            cur = conn.cursor()
            cur.execute("UPDATE usuarios SET activo = NOT activo WHERE id = %s RETURNING activo", (uid,))
            nuevo_estado = cur.fetchone()[0]
        estado_txt = "activado" if nuevo_estado else "desactivado"
        flash(f"Usuario {estado_txt}.", "ok")
    return redirect(url_for('admin_usuarios'))


@app.route("/admin/usuarios/<int:uid>/eliminar", methods=["POST"])
@admin_required
def admin_usuario_eliminar(uid):
    if uid == session['user_id']:
        flash("No puedes eliminar tu propia cuenta.", "error")
    else:
        # Reutilizamos eliminación directa
        with db.get_conn() as conn:
            cur = conn.cursor()
            cur.execute("DELETE FROM usuarios WHERE id = %s", (uid,))
        flash("Usuario eliminado permanentemente.", "ok")
    return redirect(url_for('admin_usuarios'))


@app.route("/backup.sql")
@admin_required
def backup_sql():
    sql = db.export_database_sql()
    buf = BytesIO(sql.encode("utf-8"))
    buf.seek(0)
    return send_file(
        buf,
        as_attachment=True,
        download_name=f"financiera_backup_{today_str()}.sql",
        mimetype="application/sql",
    )


@app.route("/backup/restore", methods=["POST"])
@admin_required
def backup_restore():
    if request.form.get("confirm_restore") != "si":
        flash("Confirma la restauración para continuar.", "error")
        return redirect(url_for("configuracion"))
    up = request.files.get("sql_file")
    if not up or up.filename == "":
        flash("Selecciona un archivo .sql exportado desde esta app.", "error")
        return redirect(url_for("configuracion"))
    raw = up.read()
    if len(raw) > 25 * 1024 * 1024:
        flash("El archivo es demasiado grande (máx. 25 MB).", "error")
        return redirect(url_for("configuracion"))
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        flash("El archivo debe estar en UTF-8.", "error")
        return redirect(url_for("configuracion"))
    if "TRUNCATE" not in text.upper() or "INSERT INTO" not in text.upper():
        flash("El archivo no parece un respaldo válido de Financiera NP.", "error")
        return redirect(url_for("configuracion"))
    try:
        db.restore_database_sql(text)
    except Exception as e:
        flash(f"No se pudo restaurar: {e}", "error")
        return redirect(url_for("configuracion"))
    flash("Base restaurada desde el archivo. Vuelve a iniciar sesión si es necesario.", "ok")
    return redirect(url_for("configuracion"))


@app.route("/prestamos/<int:pid>/notas", methods=["GET", "POST"])
@login_required
def prestamos_notas(pid):
    uid, _, is_admin, _ = ctx_user()
    info = db.obtener_prestamo(pid, uid, is_admin)
    if not info:
        abort(404)
    if request.method == "POST":
        nota = request.form.get("notas", "")
        db.actualizar_nota_prestamo(pid, nota, uid, is_admin)
        flash("Observaciones guardadas.", "ok")
        return redirect(url_for("prestamos_notas", pid=pid))
    row = db.listar_prestamos("p.id = %s", (pid,), uid, is_admin)
    notas = row[0]['notas'] if row else ""
    return render_template("prestamos_notas.html", pid=pid, nombre=info[2], notas=notas or "")


@app.route("/prestamos/<int:pid>/recibo/<int:pago_id>")
@login_required
def descargar_recibo(pid, pago_id):
    uid, _, is_admin, _ = ctx_user()
    try:
        pago = db.obtener_pago_para_recibo(pid, pago_id, uid, is_admin)
        if not pago:
            flash("El pago solicitado no existe o no tienes permiso para verlo.", "error")
            return redirect(url_for("pagos_list"))

        buf = generar_recibo_imagen(
            pago["nombre_cliente"] or "Cliente",
            pid,
            int(pago["cuota"] or 1),
            float(pago["valor"] or 0),
            str(pago["fecha"] or today_str()),
            uid,
            is_admin,
            valor_cuota_base=float(pago["valor_cuota_base"] or 0),
            interes_mora=float(pago["interes_mora"] or 0),
            recibo_no=int(pago["pago_id"] or pago_id),
        )
        if not buf:
            raise ValueError("No se pudo generar el recibo.")
        buf.seek(0)
        return send_file(
            buf,
            as_attachment=True,
            download_name=f"recibo_{pid}_{pago_id}.png",
            mimetype="image/png",
        )
    except Exception as e:
        flash(f"No se pudo generar el recibo: {e}", "error")
        return redirect(url_for("pagos_list", prestamo_id=pid))


@app.route("/cobro/hoy")
@login_required
def cobro_hoy():
    uid, _, is_admin, _ = ctx_user()
    rows = db.listar_cobro_hoy(uid, is_admin)
    total = sum(float(r[5] or 0) for r in rows)
    mora_total = 0
    for r in rows:
        if r[7]:
            dias = r[12] or 0
            if dias > 0 and r[8]:
                valor_cuota = float(r[5] or 0)
                tasa = float(r[8] or 0)
                mora_total += valor_cuota * (tasa / 100) * dias
    return render_template("cobro_hoy.html", rows=rows, total=total, mora_total=mora_total, count=len(rows))


# ---------- WhatsApp / Evolution API ----------
@app.route("/api/whatsapp/webhook", methods=["POST"])
def whatsapp_webhook():
    """Recibe mensajes de n8n o directamente de Evolution API."""
    data = request.get_json()
    instance_name = data.get("instance") or data.get("instanceName")
    from_number = data.get("from", "")
    message_data = data.get("message", {})
    message = message_data.get("body") or message_data.get("content") or data.get("message", "")

    if not instance_name or not from_number or not message:
        return jsonify({"error": "Datos incompletos"}), 400

    inst = db.get_instance_by_name(instance_name)
    if not inst:
        return jsonify({"error": "Instancia no encontrada"}), 404

    user_id = inst["user_id"]

    db.save_whatsapp_message(
        instance_id=inst["id"],
        from_number=from_number,
        to_number=inst.get("phone_number", ""),
        content=message,
        direction="inbound",
    )

    response = process_whatsapp_message(user_id, inst["id"], from_number, message)

    if response:
        send_whatsapp_message(instance_name, from_number, response)
        db.save_whatsapp_message(
            instance_id=inst["id"],
            from_number=inst.get("phone_number", ""),
            to_number=from_number,
            content=response,
            direction="outbound",
        )

    return jsonify({"status": "ok"}), 200


@app.route("/api/whatsapp/instance", methods=["POST"])
@login_required
def create_whatsapp_instance():
    """Crear nueva instancia WhatsApp para el usuario."""
    uid = session["user_id"]
    existing = db.get_user_instance(uid)
    if existing:
        return jsonify({"error": "Ya tienes una instancia. Elimínala primero.", "instance": existing["instance_name"]}), 400

    instance_name = f"user_{uid}_{int(time.time())}"

    try:
        resp = requests.post(
            f"{EVOLUTION_API_URL}/instance/create",
            json={"instanceName": instance_name, "qrcode": True},
            headers=_evolution_headers(),
            timeout=15,
        )
        if resp.status_code in (200, 201):
            db.create_whatsapp_instance(uid, instance_name)
            return jsonify({"instance": instance_name, "status": "pending_qr"}), 201
        return jsonify({"error": f"Evolution API error: {resp.text}"}), 500
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/whatsapp/instance/qr", methods=["GET"])
@login_required
def get_instance_qr():
    """Obtener QR para conectar WhatsApp."""
    uid = session["user_id"]
    inst = db.get_user_instance(uid)
    if not inst:
        return jsonify({"error": "No hay instancia. Créala primero."}), 404

    try:
        resp = requests.get(
            f"{EVOLUTION_API_URL}/instance/qr/{inst['instance_name']}",
            headers=_evolution_headers(),
            timeout=10,
        )
        if resp.status_code == 200:
            qr_data = resp.json()
            if qr_data.get("base64"):
                db.update_instance_status(inst["instance_name"], "qr_ready", qr_code=qr_data["base64"])
            return jsonify(qr_data), 200
        return jsonify({"error": "No se pudo obtener el QR"}), 500
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/whatsapp/instance/status", methods=["GET"])
@login_required
def get_instance_status():
    """Verificar estado de la instancia."""
    uid = session["user_id"]
    inst = db.get_user_instance(uid)
    if not inst:
        return jsonify({"status": "none"}), 200
    return jsonify({
        "instance": inst["instance_name"],
        "status": inst["status"],
        "phone": inst.get("phone_number"),
        "qr_code": inst.get("qr_code"),
    }), 200


@app.route("/api/whatsapp/instance/delete", methods=["POST"])
@login_required
def delete_whatsapp_instance():
    """Eliminar instancia WhatsApp del usuario."""
    uid = session["user_id"]
    inst = db.get_user_instance(uid)
    if not inst:
        return jsonify({"error": "No hay instancia"}), 404

    try:
        requests.delete(
            f"{EVOLUTION_API_URL}/instance/delete/{inst['instance_name']}",
            headers=_evolution_headers(),
            timeout=10,
        )
    except Exception:
        pass

    with db.get_conn() as conn:
        cur = conn.cursor()
        cur.execute("DELETE FROM whatsapp_instances WHERE user_id = %s", (uid,))

    return jsonify({"status": "deleted"}), 200


@app.route("/api/whatsapp/send", methods=["POST"])
@login_required
def send_whatsapp_message_api():
    """Enviar mensaje manual desde la app."""
    uid = session["user_id"]
    data = request.get_json()
    to_number = data.get("to", "").strip()
    message = data.get("message", "").strip()

    if not to_number or not message:
        return jsonify({"error": "Número y mensaje requeridos"}), 400

    inst = db.get_user_instance(uid)
    if not inst or inst["status"] != "connected":
        return jsonify({"error": "WhatsApp no conectado"}), 400

    ok = send_whatsapp_message(inst["instance_name"], to_number, message)
    if ok:
        db.save_whatsapp_message(
            instance_id=inst["id"],
            from_number=inst.get("phone_number", ""),
            to_number=to_number,
            content=message,
            direction="outbound",
        )
        return jsonify({"status": "sent"}), 200
    return jsonify({"error": "No se pudo enviar"}), 500


@app.route("/api/whatsapp/messages", methods=["GET"])
@login_required
def get_whatsapp_messages():
    """Obtener mensajes recientes del usuario."""
    uid = session["user_id"]
    inst = db.get_user_instance(uid)
    if not inst:
        return jsonify([]), 200

    with db.get_conn() as conn:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute(
            """SELECT id, from_number, to_number, content, direction, message_type, created_at
               FROM whatsapp_messages
               WHERE instance_id = %s
               ORDER BY created_at DESC LIMIT 50""",
            (inst["id"],),
        )
        messages = cur.fetchall()

    return jsonify(messages), 200


@app.route("/api/alertas/vencimientos", methods=["POST"])
def alertas_vencimientos():
    """Endpoint para n8n: devuelve préstamos vencidos para enviar alertas."""
    vencidos = db.get_vencimientos_hoy()
    return jsonify(vencidos), 200


@app.route("/recordatorios")
@login_required
def recordatorios():
    """Página de recordatorios de cobro."""
    uid, _, is_admin, _ = ctx_user()
    hoy = date.today()
    manana = hoy + timedelta(days=1)
    vencidos = db.listar_cuotas_vencidas(uid, is_admin)
    por_vencer = db.listar_cuotas_vencer(uid, is_admin)
    return render_template(
        "recordatorios.html",
        vencidos=vencidos,
        por_vencer=por_vencer,
        hoy=hoy.strftime("%Y-%m-%d"),
        manana=manana.strftime("%Y-%m-%d"),
    )


@app.route("/api/docs")
@login_required
def api_docs():
    """Documentación de la API interna."""
    endpoints = [
        {"method": "GET", "path": "/api/buscar_clientes", "desc": "Buscar clientes por nombre (autocomplete)", "params": "q=<término>", "auth": "Sí"},
        {"method": "POST", "path": "/api/alertas/vencimientos", "desc": "Obtener préstamos vencidos para alertas", "params": "Ninguno", "auth": "No (webhook)"},
        {"method": "POST", "path": "/api/whatsapp/send", "desc": "Enviar mensaje WhatsApp", "params": "phone, message", "auth": "Sí"},
        {"method": "GET", "path": "/api/whatsapp/messages", "desc": "Listar mensajes WhatsApp", "params": "page, per_page", "auth": "Sí"},
        {"method": "POST", "path": "/api/whatsapp/webhook", "desc": "Webhook para recibir mensajes", "params": "JSON body", "auth": "No (webhook)"},
        {"method": "POST", "path": "/api/whatsapp/instance", "desc": "Crear instancia WhatsApp", "params": "instance_name", "auth": "Sí"},
        {"method": "GET", "path": "/api/whatsapp/instance/qr", "desc": "Obtener QR de instancia", "params": "instance_name", "auth": "Sí"},
        {"method": "GET", "path": "/api/whatsapp/instance/status", "desc": "Estado de instancia", "params": "instance_name", "auth": "Sí"},
        {"method": "POST", "path": "/api/whatsapp/instance/delete", "desc": "Eliminar instancia", "params": "instance_name", "auth": "Sí"},
    ]
    return render_template("api_docs.html", endpoints=endpoints)




