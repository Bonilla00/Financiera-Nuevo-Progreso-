import os
from datetime import date, datetime, timedelta
from functools import wraps
from io import BytesIO
from itertools import groupby

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
    jsonify,
)
from werkzeug.security import check_password_hash, generate_password_hash

import db
import recibos
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


@app.before_request
def _ensure_db_schema():
    if app.config.get("DB_SCHEMA_READY"):
        return
    db.ensure_schema_migrations()
    app.config["DB_SCHEMA_READY"] = True


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
    return int(uid), session.get("username", ""), session.get("is_admin", False), session.get("rol", "solo_lectura")


def require_role(roles):
    def decorator(f):
        @wraps(f)
        def decorated_function(*args, **kwargs):
            if 'user_id' not in session:
                return redirect(url_for('login', next=request.path))
            if session.get('rol') not in roles and not session.get('is_admin'):
                flash("No tienes permiso para acceder a esta sección.", "error")
                return redirect(url_for('index'))
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
        elif not row['activo']:
            flash("Cuenta desactivada. Contacta al administrador.", "error")
        elif not check_password_hash(row['password_hash'], p):
            flash("Usuario o clave incorrectos.", "error")
        else:
            session["user_id"] = row['id']
            session["username"] = row['username']
            session["is_admin"] = (row['rol'] == "admin")
            session["rol"] = row['rol']
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
    uid, _, is_admin, _ = ctx_user()
    stats = db.obtener_stats_dashboard(uid, is_admin)

    # Alertas para el dashboard
    mora_list = db.listar_prestamos("p.estado = 'ACTIVO' AND p.proximo_pago IS NOT NULL AND p.proximo_pago <> '' AND p.proximo_pago::date < CURRENT_DATE", (), uid, is_admin)
    vencen_manana = db.listar_prestamos("p.estado = 'ACTIVO' AND p.proximo_pago IS NOT NULL AND p.proximo_pago <> '' AND p.proximo_pago::date = CURRENT_DATE + 1", (), uid, is_admin)

    return render_template(
        "index.html",
        stats=stats,
        mora=mora_list,
        vencen_manana=vencen_manana,
    )


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
    uid, _, is_admin, _ = ctx_user()
    filtro = request.args.get("estado", "todo")
    rows = db.listar_clientes_filtrado(filtro, uid, is_admin)
    return render_template("clientes.html", clientes=rows, filtro=filtro)


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
    uid, _, is_admin, _ = ctx_user()
    filtro = request.args.get("estado", "activos")

    where = ""
    params = ()

    try:
        if filtro == "activos":
            where, params = "p.estado = %s", ("ACTIVO",)
        elif filtro == "pagados":
            where, params = "p.estado = %s", ("PAGADO",)
        elif filtro == "mora":
            # Usamos la misma lógica segura del backend para filtrar
            where = "p.estado = 'ACTIVO' AND p.proximo_pago IS NOT NULL AND p.proximo_pago <> '' AND p.proximo_pago::date < CURRENT_DATE"
            params = ()

        rows = db.listar_prestamos(where, params, uid, is_admin)
        return render_template("prestamos.html", prestamos=rows, filtro=filtro)

    except Exception as e:
        # Logging del error para debug
        print(f"Error en /prestamos: {e}")
        flash("Ocurrió un error al cargar la lista de préstamos.", "error")
        return redirect(url_for("index"))


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
            flash("Préstamo creado.", "ok")
            return redirect(url_for("prestamos_list"))
        except Exception as e:
            flash(str(e), "error")
    return render_template("prestamo_nuevo.html", clientes=clientes)


@app.route("/prestamos/<int:pid>/editar", methods=["GET", "POST"])
@require_role(['admin'])
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
            ok = db.actualizar_prestamo(
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
                    "Préstamo actualizado. Se recalcularon montos y próximo pago; los pagos anteriores se conservan.",
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
    uid, _, is_admin, _ = ctx_user()
    prestamo_filtro = request.args.get("prestamo_id", type=int)
    rows = db.listar_pagos(prestamo_filtro, uid, is_admin)
    grupos = [(fecha, list(items)) for fecha, items in groupby(rows, key=lambda r: r[3])]
    return render_template(
        "pagos.html",
        pagos_grupos=grupos,
        filtro_prestamo_id=prestamo_filtro,
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
        action = request.form.get("action")
        if action == "crear":
            u = request.form.get("username", "").strip()
            p1 = request.form.get("password", "")
            rol = request.form.get("rol", "usuario")
            if len(u) < 3:
                flash("Usuario muy corto.", "error")
            elif len(p1) < 6:
                flash("Clave muy corta.", "error")
            else:
                try:
                    db.crear_usuario(u, generate_password_hash(p1), rol=rol)
                    flash(f"Usuario {u} creado.", "ok")
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
        return redirect(url_for("admin_usuarios"))

    usuarios = db.listar_usuarios()
    return render_template("admin_usuarios.html", usuarios=usuarios)


@app.route("/admin/usuarios/<int:uid>/toggle", methods=["POST"])
@admin_required
def admin_usuario_toggle(uid):
    if uid == session['user_id']:
        flash("No puedes desactivar tu propia cuenta.", "error")
    else:
        nuevo_estado = db.admin_toggle_activo(uid)
        estado_txt = "activado" if nuevo_estado else "desactivado"
        flash(f"Usuario {estado_txt}.", "ok")
    return redirect(url_for('admin_usuarios'))


@app.route("/admin/usuarios/<int:uid>/password", methods=["POST"])
@admin_required
def admin_usuario_password(uid):
    p = request.form.get("new_password", "")
    if len(p) < 6:
        flash("La clave debe tener al menos 6 caracteres.", "error")
    else:
        h = generate_password_hash(p)
        db.admin_cambiar_password(uid, h)
        flash("Contraseña actualizada correctamente.", "ok")
    return redirect(url_for('admin_usuarios'))


@app.route("/admin/reset_password", methods=["POST"])
@admin_required
def admin_reset_password_route():
    """Ruta para reseteo de contraseña recibiendo user_id por formulario."""
    uid = request.form.get("user_id")
    p = request.form.get("password")

    if not uid or not p:
        flash("Datos incompletos.", "error")
        return redirect(url_for('admin_usuarios'))

    if len(p) < 6:
        flash("La clave debe tener al menos 6 caracteres.", "error")
    else:
        h = generate_password_hash(p)
        db.actualizar_password_usuario(int(uid), h)
        flash("Contraseña restablecida con éxito.", "ok")
    return redirect(url_for('admin_usuarios'))


@app.route("/admin/usuarios/<int:uid>/eliminar", methods=["POST"])
@admin_required
def admin_usuario_eliminar(uid):
    if uid == session['user_id']:
        flash("No puedes eliminar tu propia cuenta.", "error")
    else:
        db.admin_eliminar_usuario(uid)
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
        return redirect(url_for("prestamos_list"))
    row = db.listar_prestamos("p.id = %s", (pid,), uid, is_admin)
    notas = row[0]['notas'] if row else ""
    return render_template("prestamos_notas.html", pid=pid, nombre=info[2], notas=notas or "")


@app.route("/prestamos/<int:pid>/recibo/<int:pago_id>")
@login_required
def descargar_recibo(pid, pago_id):
    uid, _, is_admin, _ = ctx_user()
    info = db.obtener_prestamo(pid, uid, is_admin)
    if not info:
        abort(404)
    nombre = info[2]
    datos = session.get("_ultimo_pago", {})
    if datos.get("pago_id") != pago_id or datos.get("pid") != pid:
        datos = {"pid": pid, "pago_id": pago_id, "num_cuota": 1, "valor": 0, "fecha": today_str(), "interes_mora": 0, "valor_cuota_base": 0}
    buf = recibos.generar_recibo_pdf(
        nombre,
        pid,
        datos.get("num_cuota", 1),
        datos.get("valor", 0),
        datos.get("fecha", today_str()),
        uid,
        is_admin,
        valor_cuota_base=datos.get("valor_cuota_base", 0),
        interes_mora=datos.get("interes_mora", 0),
    )
    return send_file(
        buf,
        as_attachment=True,
        download_name=f"recibo_{pid}_{pago_id}.pdf",
        mimetype="application/pdf",
    )


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


@app.route("/gastos", methods=["GET", "POST"])
@login_required
def gastos():
    from datetime import datetime
    uid, _, is_admin, _ = ctx_user()
    db.ensure_gastos_table()
    año = int(request.args.get("año", datetime.now().year))
    mes = int(request.args.get("mes", datetime.now().month))
    if request.method == "POST":
        fecha = request.form.get("fecha", today_str())
        desc = request.form.get("descripcion", "").strip()
        valor = float(request.form.get("valor", "0"))
        categoria = request.form.get("categoria", "Otro")
        if desc and valor > 0:
            db.registrar_gasto(uid, fecha, desc, valor, categoria)
            flash("Gasto registrado.", "ok")
        return redirect(url_for("gastos", año=año, mes=mes))
    rows = db.listar_gastos_mes(uid, is_admin, año, mes)
    total = sum(float(r[3] or 0) for r in rows)
    return render_template("gastos.html", rows=rows, total=total, año=año, mes=mes)


@app.route("/gastos/<int:gasto_id>/eliminar", methods=["POST"])
@require_role(['admin'])
def eliminar_gasto(gasto_id):
    uid, _, is_admin, _ = ctx_user()
    from datetime import datetime
    año = int(request.form.get("año", datetime.now().year))
    mes = int(request.form.get("mes", datetime.now().month))
    if db.eliminar_gasto(gasto_id, uid, is_admin):
        flash("Gasto eliminado.", "ok")
    else:
        flash("No se pudo eliminar.", "error")
    return redirect(url_for("gastos", año=año, mes=mes))
