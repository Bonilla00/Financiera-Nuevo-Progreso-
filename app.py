import os
from datetime import datetime
from functools import wraps
from io import BytesIO

import psycopg2
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
)
from werkzeug.security import check_password_hash, generate_password_hash

import db
import recibos
from utils_web import add_days

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "cambia-esto-en-produccion")


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


def prestamo_en_mora_fila(row, hoy: datetime) -> bool:
    if row[9] != "ACTIVO":
        return False
    prox = _parse_fecha_iso(row[13] if len(row) > 13 else None)
    venc = _parse_fecha_iso(row[8])
    ref = prox if prox is not None else venc
    if ref is None:
        return False
    return ref < hoy


def ctx_user():
    uid = session.get("user_id")
    if not uid:
        return None, None, False
    return int(uid), session.get("username", ""), session.get("is_admin", False)


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
    _, __, is_admin = ctx_user()
    return {"fmt_money": fmt_money, "today_str": today_str, "is_admin": is_admin}


@app.route("/setup", methods=["GET", "POST"])
def setup():
    if db.count_usuarios() > 0:
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
    if db.count_usuarios() == 0:
        return redirect(url_for("setup"))
    if request.method == "POST":
        u = request.form.get("username", "").strip()
        p = request.form.get("password", "")
        row = db.obtener_usuario_por_username(u)
        if not row:
            flash("Usuario o clave incorrectos.", "error")
        elif not row[4]:
            flash("Cuenta desactivada. Contacta al administrador.", "error")
        elif not check_password_hash(row[2], p):
            flash("Usuario o clave incorrectos.", "error")
        else:
            session["user_id"] = row[0]
            session["username"] = row[1]
            session["is_admin"] = row[3] == "admin"
            nxt = request.args.get("next") or url_for("index")
            return redirect(nxt)
    return render_template("login.html")


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.route("/")
@login_required
def index():
    uid, _, is_admin = ctx_user()
    prs = db.listar_prestamos("", (), uid, is_admin)
    hoy = datetime.strptime(today_str(), "%Y-%m-%d")
    total = len(prs)
    activos = sum(1 for p in prs if p[9] == "ACTIVO")
    pagados = sum(1 for p in prs if p[9] == "PAGADO")
    mora = sum(1 for p in prs if prestamo_en_mora_fila(p, hoy))
    cobrado_hoy = db.sum_pagos_hoy(uid, is_admin)
    return render_template(
        "index.html",
        total=total,
        activos=activos,
        pagados=pagados,
        mora=mora,
        cobrado_hoy=cobrado_hoy,
    )


@app.route("/clientes")
@login_required
def clientes_list():
    uid, _, is_admin = ctx_user()
    rows = db.listar_clientes(uid, is_admin)
    return render_template("clientes.html", clientes=rows)


@app.route("/clientes/nuevo", methods=["GET", "POST"])
@login_required
def clientes_nuevo():
    uid, _, is_admin = ctx_user()
    if request.method == "POST":
        db.get_or_create_cliente(
            request.form.get("nombre", "").strip(),
            request.form.get("identificacion", "").strip(),
            request.form.get("telefono", "").strip(),
            request.form.get("barrio", "").strip(),
            request.form.get("direccion", "").strip(),
            uid,
        )
        flash("Cliente guardado.", "ok")
        return redirect(url_for("clientes_list"))
    return render_template("cliente_form.html", cliente=None)


@app.route("/clientes/<int:cid>/editar", methods=["GET", "POST"])
@login_required
def clientes_editar(cid):
    uid, _, is_admin = ctx_user()
    row = db.obtener_cliente(cid, uid, is_admin)
    if not row:
        abort(404)
    if request.method == "POST":
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
        return redirect(url_for("clientes_list"))
    return render_template("cliente_form.html", cliente=row)


@app.route("/clientes/<int:cid>/eliminar", methods=["POST"])
@login_required
def clientes_eliminar(cid):
    uid, _, is_admin = ctx_user()
    if db.eliminar_cliente_y_todo(cid, uid, is_admin):
        flash("Cliente y su historial eliminados.", "ok")
    else:
        flash("No se pudo eliminar.", "error")
    return redirect(url_for("clientes_list"))


@app.route("/prestamos")
@login_required
def prestamos_list():
    uid, _, is_admin = ctx_user()
    filtro = request.args.get("estado", "activos")
    hoy = today_str()
    if filtro == "activos":
        where, params = "p.estado = %s", ("ACTIVO",)
    elif filtro == "pagados":
        where, params = "p.estado = %s", ("PAGADO",)
    elif filtro == "mora":
        where, params = "p.estado = %s", ("ACTIVO",)
    else:
        where, params = "", ()
    rows = db.listar_prestamos(where, params, uid, is_admin)
    if filtro == "mora":
        hoy_dt = datetime.strptime(hoy, "%Y-%m-%d")
        rows = [r for r in rows if prestamo_en_mora_fila(r, hoy_dt)]
    return render_template("prestamos.html", prestamos=rows, filtro=filtro)


@app.route("/prestamos/nuevo", methods=["GET", "POST"])
@login_required
def prestamos_nuevo():
    uid, _, is_admin = ctx_user()
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
            db.nuevo_prestamo(
                cid, fecha, freq, cuotas, monto, tasa, interes, total, cuota, venc, uid, is_admin
            )
            flash("Préstamo creado.", "ok")
            return redirect(url_for("prestamos_list"))
        except Exception as e:
            flash(str(e), "error")
    return render_template("prestamo_nuevo.html", clientes=clientes)


@app.route("/prestamos/<int:pid>/pago", methods=["POST"])
@login_required
def prestamos_pago(pid):
    uid, _, is_admin = ctx_user()
    try:
        valor = float(request.form.get("valor", "0"))
        fecha = request.form.get("fecha", today_str())
        pago_id, num_cuota = db.registrar_pago(pid, fecha, valor, uid, is_admin)
        info = db.obtener_prestamo(pid, uid, is_admin)
        nombre = info[2]
        buf = recibos.generar_recibo_pdf(nombre, pid, num_cuota, valor, fecha, uid, is_admin)
        flash("Pago registrado. Descarga el recibo.", "ok")
        return send_file(
            buf,
            as_attachment=True,
            download_name=f"recibo_{pid}_{pago_id}.pdf",
            mimetype="application/pdf",
        )
    except Exception as e:
        flash(str(e), "error")
        return redirect(url_for("prestamos_list"))


@app.route("/pagos")
@login_required
def pagos_list():
    uid, _, is_admin = ctx_user()
    rows = db.listar_pagos(None, uid, is_admin)
    return render_template("pagos.html", pagos=rows)


@app.route("/pagos/<int:pago_id>/eliminar", methods=["POST"])
@login_required
def pagos_eliminar(pago_id):
    uid, _, is_admin = ctx_user()
    prestamo_id = int(request.form.get("prestamo_id", "0"))
    if db.eliminar_pago_y_actualizar(prestamo_id, pago_id, uid, is_admin):
        flash("Pago eliminado.", "ok")
    else:
        flash("No se pudo eliminar el pago.", "error")
    return redirect(url_for("pagos_list"))


@app.route("/configuracion", methods=["GET", "POST"])
@login_required
def configuracion():
    uid, _, __ = ctx_user()
    if request.method == "POST":
        actual = request.form.get("password_actual", "")
        n1 = request.form.get("password_nueva", "")
        n2 = request.form.get("password_nueva2", "")
        row = db.obtener_usuario_por_id(uid)
        if not row or not check_password_hash(row[2], actual):
            flash("La clave actual no es correcta.", "error")
        elif len(n1) < 6:
            flash("La nueva clave debe tener al menos 6 caracteres.", "error")
        elif n1 != n2:
            flash("Las claves nuevas no coinciden.", "error")
        else:
            db.actualizar_password_usuario(uid, generate_password_hash(n1))
            flash("Clave actualizada.", "ok")
            return redirect(url_for("configuracion"))
    return render_template("configuracion.html")


@app.route("/admin/usuarios", methods=["GET", "POST"])
@admin_required
def admin_usuarios():
    if request.method == "POST":
        action = request.form.get("action")
        if action == "crear":
            u = request.form.get("username", "").strip()
            p1 = request.form.get("password", "")
            rol = request.form.get("rol", "usuario")
            if len(u) < 3:
                flash("Usuario muy corto.", "error")
            elif len(p1) < 6:
                flash("Clave muy corta.", "error")
            elif rol not in ("admin", "usuario"):
                flash("Rol inválido.", "error")
            else:
                try:
                    db.crear_usuario(u, generate_password_hash(p1), rol=rol)
                    flash("Usuario creado.", "ok")
                except psycopg2.IntegrityError:
                    flash("Ese nombre de usuario ya existe.", "error")
                except Exception as e:
                    flash(str(e), "error")
        elif action == "actualizar":
            uid = int(request.form.get("user_id", "0"))
            rol = request.form.get("rol", "usuario")
            activo = request.form.get("activo") == "on"
            admins = [r for r in db.listar_usuarios() if r[2] == "admin" and r[3]]
            if not activo and uid == session["user_id"]:
                flash("No puedes desactivarte a ti mismo.", "error")
            elif rol != "admin" and uid == session["user_id"]:
                flash("No puedes quitarte el rol admin a ti mismo.", "error")
            elif rol != "admin" and len(admins) == 1 and admins[0][0] == uid:
                flash("Debe existir al menos un administrador activo.", "error")
            else:
                db.admin_actualizar_usuario(uid, rol, activo)
                flash("Usuario actualizado.", "ok")
        elif action == "reset_password":
            uid = int(request.form.get("user_id", "0"))
            p1 = request.form.get("new_password", "")
            if len(p1) < 6:
                flash("Clave muy corta.", "error")
            else:
                db.admin_reset_password(uid, generate_password_hash(p1))
                flash("Clave restablecida.", "ok")
        return redirect(url_for("admin_usuarios"))
    usuarios = db.listar_usuarios()
    return render_template("admin_usuarios.html", usuarios=usuarios)


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


@app.route("/prestamos/<int:pid>/notas", methods=["GET", "POST"])
@login_required
def prestamos_notas(pid):
    uid, _, is_admin = ctx_user()
    info = db.obtener_prestamo(pid, uid, is_admin)
    if not info:
        abort(404)
    if request.method == "POST":
        nota = request.form.get("notas", "")
        db.actualizar_nota_prestamo(pid, nota, uid, is_admin)
        flash("Observaciones guardadas.", "ok")
        return redirect(url_for("prestamos_list"))
    row = db.listar_prestamos("p.id = %s", (pid,), uid, is_admin)
    notas = row[0][14] if row else ""
    return render_template("prestamos_notas.html", pid=pid, nombre=info[2], notas=notas or "")
