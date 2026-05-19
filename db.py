"""
Capa de datos PostgreSQL para la PWA (Railway DATABASE_URL).
Los clientes pertenecen a un usuario (owner_user_id); el rol admin ve todo.
"""
from __future__ import annotations

import os
from contextlib import contextmanager
from datetime import datetime, timedelta
from typing import Optional

import psycopg2
import psycopg2.extras
from werkzeug.security import generate_password_hash


def _dsn() -> str:
    url = os.environ.get("DATABASE_URL")
    if not url:
        raise RuntimeError("Falta la variable de entorno DATABASE_URL")
    if url.startswith("postgres://"):
        url = url.replace("postgres://", "postgresql://", 1)
    return url


@contextmanager
def get_conn():
    conn = psycopg2.connect(_dsn())
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def ensure_schema_migrations() -> None:
    """ALTER seguro al arrancar (Railway / Postgres)."""
    stmts = [
        "ALTER TABLE prestamos ADD COLUMN IF NOT EXISTS mora_activa BOOLEAN NOT NULL DEFAULT FALSE",
        "ALTER TABLE prestamos ADD COLUMN IF NOT EXISTS tasa_mora_diaria DOUBLE PRECISION NOT NULL DEFAULT 0",
        "ALTER TABLE pagos ADD COLUMN IF NOT EXISTS interes_mora DOUBLE PRECISION NOT NULL DEFAULT 0",
        "ALTER TABLE pagos ADD COLUMN IF NOT EXISTS nota TEXT",
        "ALTER TABLE clientes ADD COLUMN IF NOT EXISTS foto TEXT",
        "ALTER TABLE clientes ADD COLUMN IF NOT EXISTS owner_user_id INTEGER REFERENCES usuarios(id) ON DELETE CASCADE",
        "CREATE INDEX IF NOT EXISTS idx_clientes_owner ON clientes(owner_user_id)",
        "ALTER TABLE usuarios DROP CONSTRAINT IF EXISTS usuarios_rol_check",
        "ALTER TABLE usuarios ADD CONSTRAINT usuarios_rol_check CHECK (rol IN ('admin', 'cobrador', 'solo_lectura', 'usuario'))",
        "ALTER TABLE usuarios ADD COLUMN IF NOT EXISTS debe_cambiar_password BOOLEAN DEFAULT TRUE",
        "ALTER TABLE usuarios ADD COLUMN IF NOT EXISTS activo BOOLEAN DEFAULT TRUE",
        "CREATE TABLE IF NOT EXISTS logs (id SERIAL PRIMARY KEY, user_id INT, accion TEXT, fecha TIMESTAMP DEFAULT CURRENT_TIMESTAMP)",
        "UPDATE usuarios SET activo = TRUE WHERE activo IS NULL",
        "UPDATE usuarios SET debe_cambiar_password = TRUE WHERE debe_cambiar_password IS NULL",
        "CREATE TABLE IF NOT EXISTS whatsapp_instances (id SERIAL PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES usuarios(id) ON DELETE CASCADE, phone_number VARCHAR(20), instance_name VARCHAR(50) UNIQUE, status VARCHAR(20) DEFAULT 'pending', qr_code TEXT, connected_at TIMESTAMPTZ, created_at TIMESTAMPTZ DEFAULT NOW())",
        "CREATE TABLE IF NOT EXISTS whatsapp_messages (id SERIAL PRIMARY KEY, instance_id INTEGER NOT NULL REFERENCES whatsapp_instances(id) ON DELETE CASCADE, from_number VARCHAR(20) NOT NULL, to_number VARCHAR(20) NOT NULL, message_type VARCHAR(20) DEFAULT 'text', content TEXT, media_url TEXT, direction VARCHAR(10) CHECK (direction IN ('inbound', 'outbound')), created_at TIMESTAMPTZ DEFAULT NOW())",
        "CREATE TABLE IF NOT EXISTS whatsapp_sessions (id SERIAL PRIMARY KEY, instance_id INTEGER NOT NULL REFERENCES whatsapp_instances(id) ON DELETE CASCADE, client_phone VARCHAR(20) NOT NULL, state VARCHAR(30) DEFAULT 'idle', context JSONB DEFAULT '{}', last_activity TIMESTAMPTZ DEFAULT NOW(), UNIQUE(instance_id, client_phone))",
        "CREATE INDEX IF NOT EXISTS idx_wa_messages_instance ON whatsapp_messages(instance_id)",
        "CREATE INDEX IF NOT EXISTS idx_wa_messages_from ON whatsapp_messages(from_number)",
        "CREATE INDEX IF NOT EXISTS idx_wa_sessions_state ON whatsapp_sessions(state)",
        "CREATE TABLE IF NOT EXISTS login_attempts (id SERIAL PRIMARY KEY, ip_address VARCHAR(45) NOT NULL, username VARCHAR(80), failed_at TIMESTAMPTZ DEFAULT NOW())",
        "CREATE INDEX IF NOT EXISTS idx_login_attempts_ip ON login_attempts(ip_address)",
        "CREATE INDEX IF NOT EXISTS idx_login_attempts_time ON login_attempts(failed_at)",
    ]
    for s in stmts:
        try:
            with get_conn() as conn:
                cur = conn.cursor()
                cur.execute(s)
        except Exception as e:
            print(f"Info migración: {e}")

    ensure_auditoria_table()
    crear_admin_inicial()


def registrar_log(user_id: int | None, accion: str):
    """Registra una acción en la tabla de logs."""
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute("INSERT INTO logs (user_id, accion) VALUES (%s, %s)", (user_id, accion))


def obtener_metricas_globales():
    """Obtiene métricas totales de todo el sistema para el admin."""
    with get_conn() as conn:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("SELECT COUNT(*) as total FROM usuarios")
        u = cur.fetchone()['total']
        cur.execute("SELECT COUNT(*) as total FROM clientes")
        c = cur.fetchone()['total']
        cur.execute("SELECT COUNT(*) as total FROM prestamos")
        pr = cur.fetchone()['total']
        cur.execute("SELECT COUNT(*) as total FROM pagos")
        pa = cur.fetchone()['total']
        return {"usuarios": u, "clientes": c, "prestamos": pr, "pagos": pa}


def listar_usuarios_con_estadisticas():
    """Lista usuarios con su conteo de clientes asociados."""
    query = """
        SELECT u.id, u.username, u.rol, u.activo,
               (SELECT COUNT(*) FROM clientes WHERE owner_user_id = u.id) as num_clientes
        FROM usuarios u
        ORDER BY u.username ASC
    """
    with get_conn() as conn:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute(query)
        return cur.fetchall()


def obtener_logs_recientes(limit=50, user_id=None):
    """Obtiene los logs más recientes con el nombre de usuario."""
    params = [limit]
    extra_where = ""
    if user_id:
        extra_where = "WHERE l.user_id = %s"
        params.insert(0, user_id)

    query = f"""
        SELECT l.fecha, l.accion, u.username
        FROM logs l
        LEFT JOIN usuarios u ON l.user_id = u.id
        {extra_where}
        ORDER BY l.fecha DESC
        LIMIT %s
    """
    with get_conn() as conn:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute(query, tuple(params))
        return cur.fetchall()


def crear_admin_inicial():
    """Crea el usuario admin por defecto solo si no existe."""
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute("SELECT id FROM usuarios WHERE username = 'admin'")
        if cur.fetchone():
            return
        h = generate_password_hash(os.environ.get("ADMIN_DEFAULT_PASSWORD", "admin123"))
        cur.execute("""
            INSERT INTO usuarios (username, password_hash, rol, debe_cambiar_password, activo)
            VALUES ('admin', %s, 'admin', TRUE, TRUE)
        """, (h,))


def ensure_auditoria_table() -> None:
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute("""
            CREATE TABLE IF NOT EXISTS auditoria_prestamos (
                id SERIAL PRIMARY KEY,
                prestamo_id INTEGER NOT NULL,
                usuario_id INTEGER NOT NULL,
                fecha TIMESTAMP NOT NULL DEFAULT NOW(),
                campo_modificado VARCHAR(50) NOT NULL,
                valor_anterior TEXT,
                valor_nuevo TEXT
            )
        """)


def calcular_interes_mora(
    valor_cuota: float,
    proximo_pago_iso: Optional[str],
    fecha_pago_iso: str,
    mora_activa: bool,
    tasa_mora_diaria: float,
) -> float:
    if not mora_activa or tasa_mora_diaria <= 0 or not proximo_pago_iso:
        return 0.0
    try:
        d0 = datetime.strptime(str(proximo_pago_iso).strip()[:10], "%Y-%m-%d").date()
        d1 = datetime.strptime(str(fecha_pago_iso).strip()[:10], "%Y-%m-%d").date()
    except ValueError:
        return 0.0
    dias = (d1 - d0).days
    if dias <= 0:
        return 0.0
    return round(float(valor_cuota) * (float(tasa_mora_diaria) / 100.0) * dias, 2)


def proxima_fecha_pago(fecha_inicio, frecuencia, pagadas, cuotas):
    try:
        base = datetime.strptime(str(fecha_inicio)[:10], "%Y-%m-%d")
    except Exception:
        return None
    f = (frecuencia or "").lower()
    if f == "diaria":
        delta = timedelta(days=1)
    elif f == "semanal":
        delta = timedelta(weeks=1)
    elif f == "quincenal":
        delta = timedelta(days=15)
    else:
        delta = timedelta(days=30)
    if pagadas is None:
        pagadas = 0
    if pagadas >= cuotas:
        return None
    return (base + delta * (pagadas + 1)).strftime("%Y-%m-%d")


# ---------- usuarios / auth ----------
def count_usuarios() -> int:
    try:
        with get_conn() as conn:
            cur = conn.cursor()
            cur.execute("SELECT COUNT(*) FROM usuarios")
            return int(cur.fetchone()[0])
    except Exception:
        return 0


def actualizar_username_usuario(uid: int, username: str) -> None:
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute(
            "UPDATE usuarios SET username = %s WHERE id = %s",
            (username.strip().lower(), uid),
        )


def crear_usuario(username: str, password_hash: str, rol: str = "usuario") -> int:
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO usuarios (username, password_hash, rol)
            VALUES (%s, %s, %s)
            RETURNING id
            """,
            (username.strip().lower(), password_hash, rol),
        )
        return int(cur.fetchone()[0])


def obtener_usuario_por_username(username: str) -> Optional[dict]:
    with get_conn() as conn:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute(
            """
            SELECT id, username, password_hash, rol, activo, debe_cambiar_password
            FROM usuarios WHERE LOWER(username) = LOWER(%s)
            """,
            (username.strip(),),
        )
        return cur.fetchone()


def obtener_usuario_por_id(uid: int) -> Optional[dict]:
    with get_conn() as conn:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute(
            "SELECT id, username, password_hash, rol, activo, debe_cambiar_password FROM usuarios WHERE id = %s",
            (uid,),
        )
        return cur.fetchone()


def listar_usuarios() -> list[tuple]:
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT id, username, rol, activo, creado_en
            FROM usuarios ORDER BY id
            """
        )
        return cur.fetchall()


def actualizar_password_usuario(uid: int, password_hash: str) -> None:
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute(
            "UPDATE usuarios SET password_hash = %s WHERE id = %s",
            (password_hash, uid),
        )


def listar_usuarios_admin():
    """Lista todos los usuarios para la gestión administrativa."""
    with get_conn() as conn:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("SELECT id, username, rol, activo, creado_en FROM usuarios ORDER BY id ASC")
        return cur.fetchall()


def admin_update_user_basic(uid: int, username: str, rol: str):
    """Actualiza solo datos básicos (no password)."""
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute("UPDATE usuarios SET username = %s, rol = %s WHERE id = %s",
                    (username.strip().lower(), rol, uid))
        return cur.rowcount > 0


def admin_update_user_password(uid: int, password_hash: str):
    """Actualiza únicamente la contraseña de un usuario."""
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute("UPDATE usuarios SET password_hash = %s, debe_cambiar_password = FALSE WHERE id = %s",
                    (password_hash, uid))


def admin_reset_password(uid: int, password_hash: str) -> None:
    actualizar_password_usuario(uid, password_hash)


def completar_cambio_password(uid: int, password_hash: str) -> None:
    """Actualiza la clave y marca que ya no debe cambiarla."""
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute(
            "UPDATE usuarios SET password_hash = %s, debe_cambiar_password = FALSE WHERE id = %s",
            (password_hash, uid),
        )


# ---------- scope SQL ----------
def _filtro_owner(alias: str, user_id: int, is_admin: bool) -> tuple[str, tuple]:
    if is_admin:
        return "", ()
    return f" AND {alias}.owner_user_id = %s", (user_id,)


# ---------- clientes ----------
def get_or_create_cliente(
    nombre, identificacion, telefono, barrio, direccion, owner_user_id: int
) -> int:
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute(
            "SELECT id FROM clientes WHERE owner_user_id = %s AND identificacion = %s",
            (owner_user_id, identificacion),
        )
        row = cur.fetchone()
        if row:
            return int(row[0])
        cur.execute(
            """
            INSERT INTO clientes (nombre, identificacion, telefono, barrio, direccion, owner_user_id)
            VALUES (%s, %s, %s, %s, %s, %s)
            RETURNING id
            """,
            (nombre, identificacion, telefono, barrio, direccion, owner_user_id),
        )
        return int(cur.fetchone()[0])


def obtener_cliente(cid: int, user_id: int, is_admin: bool):
    extra, params = _filtro_owner("c", user_id, is_admin)
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute(
            f"""
            SELECT c.id, c.nombre, c.identificacion, c.telefono, c.barrio, c.direccion, c.foto
            FROM clientes c WHERE c.id = %s {extra}
            """,
            (cid,) + params,
        )
        return cur.fetchone()


def actualizar_foto_cliente(cliente_id: int, foto_base64: str, user_id: int, is_admin: bool) -> bool:
    if is_admin:
        extra, params = "", ()
    else:
        extra, params = " AND owner_user_id = %s", (user_id,)
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute(
            f"UPDATE clientes SET foto = %s WHERE id = %s {extra}",
            (foto_base64, cliente_id) + params,
        )
        return cur.rowcount > 0


def listar_clientes(user_id: int, is_admin: bool) -> list[tuple]:
    extra, params = _filtro_owner("c", user_id, is_admin)
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute(
            f"""
            SELECT c.id, c.nombre, c.identificacion, c.telefono, c.barrio, c.direccion
            FROM clientes c WHERE 1=1 {extra}
            ORDER BY c.nombre
            """,
            params,
        )
        return cur.fetchall()


def buscar_clientes_ajax(q: str, user_id: int, is_admin: bool):
    params = [f"%{q}%", f"%{q}%", f"%{q}%", user_id]
    query = """
        SELECT id, nombre, identificacion, telefono, barrio
        FROM clientes c
        WHERE (nombre ILIKE %s OR identificacion ILIKE %s OR telefono ILIKE %s)
        AND c.owner_user_id = %s
        ORDER BY nombre ASC LIMIT 20
    """
    with get_conn() as conn:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute(query, params)
        return cur.fetchall()


def get_clientes(user_id: int) -> list[tuple]:
    """Obtiene todos los clientes de un usuario."""
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute("""
            SELECT id, nombre, identificacion, telefono, barrio, direccion
            FROM clientes
            WHERE owner_user_id = %s
            ORDER BY nombre
        """, (user_id,))
        return cur.fetchall()


def listar_clientes_filtrado(filtro: str, user_id: int, is_admin: bool) -> list[tuple]:
    """filtro: todo | activo | pago_hoy | pendiente_hoy | sin_activo"""
    hoy = datetime.now().strftime("%Y-%m-%d")
    q = """
        SELECT c.id, c.nombre, c.identificacion, c.telefono, c.barrio, c.direccion
        FROM clientes c
        WHERE c.owner_user_id = %s
    """
    args = [user_id]
    f = (filtro or "todo").lower().strip()
    if f == "activo":
        q += """
          AND EXISTS (
            SELECT 1 FROM prestamos p
            WHERE p.cliente_id = c.id AND p.estado = 'ACTIVO'
          )
        """
    elif f == "pago_hoy":
        q += """
          AND EXISTS (
            SELECT 1 FROM prestamos p
            JOIN pagos pg ON pg.prestamo_id = p.id
            WHERE p.cliente_id = c.id AND pg.fecha = %s
          )
        """
        args.append(hoy)
    elif f == "pendiente_hoy":
        q += """
          AND EXISTS (
            SELECT 1 FROM prestamos p
            WHERE p.cliente_id = c.id AND p.estado = 'ACTIVO'
              AND p.proximo_pago IS NOT NULL AND TRIM(p.proximo_pago) <> ''
              AND p.proximo_pago = %s
          )
        """
        args.append(hoy)
    elif f == "sin_activo":
        q += """
          AND NOT EXISTS (
            SELECT 1 FROM prestamos p
            WHERE p.cliente_id = c.id AND p.estado = 'ACTIVO'
          )
        """
    q += " ORDER BY c.nombre"
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute(q, tuple(args))
        return cur.fetchall()


def actualizar_cliente(
    cid: int,
    nombre: str,
    identificacion: str,
    telefono: str,
    barrio: str,
    direccion: str,
    user_id: int,
    is_admin: bool,
) -> bool:
    extra, params = _filtro_owner("c", user_id, is_admin)
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute(
            f"""
            UPDATE clientes SET nombre=%s, identificacion=%s, telefono=%s, barrio=%s, direccion=%s
            WHERE id=%s {extra}
            """,
            (nombre, identificacion, telefono, barrio, direccion, cid) + params,
        )
        return cur.rowcount > 0


def eliminar_cliente_y_todo(cid: int, user_id: int, is_admin: bool) -> bool:
    extra, params = _filtro_owner("c", user_id, is_admin)
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute(f"SELECT id FROM clientes c WHERE c.id = %s {extra}", (cid,) + params)
        if not cur.fetchone():
            return False
        cur.execute(
            "SELECT id FROM prestamos WHERE cliente_id = %s",
            (cid,),
        )
        pids = [r[0] for r in cur.fetchall()]
        if pids:
            cur.execute(
                "DELETE FROM pagos WHERE prestamo_id = ANY(%s)",
                (pids,),
            )
            cur.execute(
                "DELETE FROM prestamos WHERE id = ANY(%s)",
                (pids,),
            )
        cur.execute("DELETE FROM clientes WHERE id = %s", (cid,))
        return True


# ---------- préstamos ----------
def nuevo_prestamo(
    cliente_id,
    fecha,
    frecuencia,
    cuotas,
    monto,
    tasa,
    interes_total,
    total_pagar,
    valor_cuota,
    vencimiento,
    user_id: int,
    is_admin: bool,
    mora_activa: bool = False,
    tasa_mora_diaria: float = 0.0,
) -> int:
    extra, params = _filtro_owner("c", user_id, is_admin)
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute(
            f"""
            SELECT 1 FROM prestamos p JOIN clientes c ON c.id = p.cliente_id
            WHERE p.cliente_id = %s AND p.monto = %s AND p.fecha = %s {extra}
            """,
            (cliente_id, monto, fecha) + params,
        )
        if cur.fetchone():
            raise ValueError("Ya existe un préstamo igual para este cliente en la misma fecha.")

        cur.execute(
            f"SELECT 1 FROM clientes c WHERE c.id = %s {extra}",
            (cliente_id,) + params,
        )
        if not cur.fetchone():
            raise ValueError("Cliente no encontrado o sin permiso.")

        proximo_pago = proxima_fecha_pago(fecha, frecuencia, 0, cuotas)
        cur.execute(
            """
            INSERT INTO prestamos
            (cliente_id, fecha, frecuencia, cuotas, monto, tasa,
             interes_total, total_pagar, valor_cuota, vencimiento, estado, pagadas, proximo_pago,
             mora_activa, tasa_mora_diaria)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'ACTIVO', 0, %s, %s, %s)
            RETURNING id
            """,
            (
                cliente_id,
                fecha,
                frecuencia,
                cuotas,
                monto,
                tasa,
                interes_total,
                total_pagar,
                valor_cuota,
                vencimiento,
                proximo_pago,
                bool(mora_activa),
                float(tasa_mora_diaria or 0),
            ),
        )
        return int(cur.fetchone()[0])


def listar_prestamos(
    where: str = "",
    params: tuple = (),
    user_id: int = 0,
    is_admin: bool = True,
):
    """
    Lista préstamos con alias explícitos para evitar colisiones de ID
    y cálculo de mora seguro para PostgreSQL.
    """
    scope, sparams = _filtro_owner("c", user_id, is_admin)

    # Query con alias explícitos y manejo seguro de fechas
    q = f"""
        SELECT
            p.id as id,
            p.monto, p.tasa, p.cuotas, p.valor_cuota,
            p.fecha, p.vencimiento, p.estado, p.pagadas,
            p.total_pagar, p.frecuencia, p.proximo_pago, p.notas,
            c.id as cid,
            c.nombre, c.identificacion, c.telefono, c.barrio,
            CASE
                WHEN p.estado = 'ACTIVO'
                     AND p.proximo_pago IS NOT NULL
                     AND p.proximo_pago <> ''
                     AND p.proximo_pago::date < CURRENT_DATE
                THEN TRUE
                ELSE FALSE
            END as en_mora
        FROM prestamos p
        JOIN clientes c ON c.id = p.cliente_id
        WHERE 1=1 {scope}
    """

    args = list(sparams)
    if where:
        q += " AND " + where
        args.extend(params)

    q += " ORDER BY p.id DESC"

    with get_conn() as conn:
        # Usamos RealDictCursor para acceder por nombre de columna en el template
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute(q, tuple(args))
        return cur.fetchall()


def obtener_stats_dashboard(user_id: int, is_admin: bool):
    scope, sparams = _filtro_owner("c", user_id, is_admin)

    with get_conn() as conn:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

        # Conteo por estado
        cur.execute(f"""
            SELECT p.estado, COUNT(*) as cantidad
            FROM prestamos p
            JOIN clientes c ON p.cliente_id = c.id
            WHERE 1=1 {scope}
            GROUP BY p.estado
        """, sparams)
        estados_rows = cur.fetchall()
        estados = {row['estado']: row['cantidad'] for row in estados_rows}

        # Préstamos en mora (específico)
        cur.execute(f"""
            SELECT COUNT(*) as cantidad
            FROM prestamos p
            JOIN clientes c ON p.cliente_id = c.id
            WHERE p.estado = 'ACTIVO'
              AND p.proximo_pago IS NOT NULL AND p.proximo_pago <> ''
              AND p.proximo_pago::date < CURRENT_DATE
              {scope}
        """, sparams)
        estados['MORA'] = cur.fetchone()['cantidad']

        # Dinero prestado vs Cobrado
        cur.execute(f"SELECT SUM(monto) as total_prestado FROM prestamos p JOIN clientes c ON p.cliente_id = c.id WHERE 1=1 {scope}", sparams)
        prestado = cur.fetchone()['total_prestado'] or 0

        cur.execute(f"SELECT SUM(valor) as total_cobrado FROM pagos pg JOIN prestamos p ON pg.prestamo_id = p.id JOIN clientes c ON p.cliente_id = c.id WHERE 1=1 {scope}", sparams)
        cobrado = cur.fetchone()['total_cobrado'] or 0

        return {
            "estados": estados,
            "total_prestado": prestado,
            "total_cobrado": cobrado
        }


def listar_cuotas_vencidas(user_id: int, is_admin: bool) -> list[tuple]:
    """Préstamos ACTIVOS con próximo pago vencido (mora)."""
    scope, sparams = _filtro_owner("c", user_id, is_admin)
    q = f"""
        SELECT p.id, c.nombre, p.valor_cuota, p.proximo_pago,
               GREATEST(0, (CURRENT_DATE - (p.proximo_pago::date)))::int AS dias_atraso,
               c.telefono
        FROM prestamos p
        JOIN clientes c ON c.id = p.cliente_id
        WHERE p.estado = 'ACTIVO'
          AND p.proximo_pago IS NOT NULL AND TRIM(p.proximo_pago) <> ''
          AND (p.proximo_pago::date) < CURRENT_DATE
          {scope}
        ORDER BY p.proximo_pago ASC NULLS LAST, c.nombre
    """
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute(q, tuple(sparams))
        return cur.fetchall()


def listar_cuotas_vencer(user_id: int, is_admin: bool) -> list[tuple]:
    """Próximo pago hoy o mañana."""
    scope, sparams = _filtro_owner("c", user_id, is_admin)
    q = f"""
        SELECT p.id, c.nombre, p.valor_cuota, p.proximo_pago, c.telefono
        FROM prestamos p
        JOIN clientes c ON c.id = p.cliente_id
        WHERE p.estado = 'ACTIVO'
          AND p.proximo_pago IS NOT NULL AND TRIM(p.proximo_pago) <> ''
          AND (p.proximo_pago::date) IN (CURRENT_DATE, CURRENT_DATE + 1)
          {scope}
        ORDER BY p.proximo_pago::date ASC, c.nombre
    """
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute(q, tuple(sparams))
        return cur.fetchall()


def listar_cobro_hoy(user_id: int, is_admin: bool) -> list[tuple]:
    """Préstamos con próximo_pago hoy o vencido, ordenados por barrio."""
    scope, sparams = _filtro_owner("c", user_id, is_admin)
    q = f"""
        SELECT p.id, c.nombre, c.barrio, c.direccion, c.telefono,
               p.valor_cuota, p.proximo_pago, p.mora_activa, p.tasa_mora_diaria,
               p.total_pagar, p.pagadas, p.cuotas,
               GREATEST(0, (CURRENT_DATE - (p.proximo_pago::date)))::int AS dias_mora
        FROM prestamos p
        JOIN clientes c ON c.id = p.cliente_id
        WHERE p.estado = 'ACTIVO'
          AND p.proximo_pago IS NOT NULL AND TRIM(p.proximo_pago) <> ''
          AND (p.proximo_pago::date) <= CURRENT_DATE
          {scope}
        ORDER BY c.barrio ASC NULLS LAST, c.nombre ASC
    """
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute(q, tuple(sparams))
        return cur.fetchall()


def sum_saldo_restante_total(user_id: int, is_admin: bool) -> float:
    """Suma de saldos restantes de todos los préstamos activos."""
    scope, sparams = _filtro_owner("c", user_id, is_admin)
    q = f"""
        SELECT COALESCE(SUM(
            p.total_pagar - (p.pagadas * p.valor_cuota)
        ), 0)
        FROM prestamos p
        JOIN clientes c ON c.id = p.cliente_id
        WHERE p.estado = 'ACTIVO' {scope}
    """
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute(q, tuple(sparams))
        return float(cur.fetchone()[0] or 0)


def guardar_auditoria_prestamo(prestamo_id: int, usuario_id: int, campo: str, anterior: str, nuevo: str) -> None:
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO auditoria_prestamos (prestamo_id, usuario_id, campo_modificado, valor_anterior, valor_nuevo)
            VALUES (%s, %s, %s, %s, %s)
            """,
            (prestamo_id, usuario_id, campo, anterior, nuevo),
        )


def listar_auditoria_prestamo(prestamo_id: int, user_id: int, is_admin: bool) -> list[tuple]:
    scope, sparams = _filtro_owner("c", user_id, is_admin)
    q = f"""
        SELECT a.fecha, a.campo_modificado, a.valor_anterior, a.valor_nuevo, u.username
        FROM auditoria_prestamos a
        JOIN prestamos p ON p.id = a.prestamo_id
        JOIN clientes c ON c.id = p.cliente_id
        JOIN usuarios u ON u.id = a.usuario_id
        WHERE a.prestamo_id = %s {scope}
        ORDER BY a.fecha DESC
    """
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute(q, (prestamo_id,) + sparams)
        return cur.fetchall()


def contar_prestamos_activos(user_id: int, is_admin: bool) -> int:
    scope, sparams = _filtro_owner("c", user_id, is_admin)
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute(
            f"""
            SELECT COUNT(*) FROM prestamos p
            JOIN clientes c ON c.id = p.cliente_id
            WHERE p.estado = 'ACTIVO' {scope}
            """,
            sparams,
        )
        return int(cur.fetchone()[0] or 0)


def contar_pagos_en_rango(f_ini: str, f_fin: str, user_id: int, is_admin: bool) -> int:
    scope, sparams = _filtro_owner("c", user_id, is_admin)
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute(
            f"""
            SELECT COUNT(*) FROM pagos
            JOIN prestamos p ON p.id = pagos.prestamo_id
            JOIN clientes c ON c.id = p.cliente_id
            WHERE pagos.fecha BETWEEN %s AND %s {scope}
            """,
            (f_ini, f_fin) + sparams,
        )
        return int(cur.fetchone()[0] or 0)


def listar_prestamos_por_cliente(cliente_id: int, user_id: int, is_admin: bool) -> list[tuple]:
    return listar_prestamos("p.cliente_id = %s", (cliente_id,), user_id, is_admin)


def sum_pagos_por_prestamo(prestamo_id: int, user_id: int, is_admin: bool) -> float:
    scope, sparams = _filtro_owner("c", user_id, is_admin)
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute(
            f"""
            SELECT COALESCE(SUM(pg.valor), 0) FROM pagos pg
            JOIN prestamos p ON p.id = pg.prestamo_id
            JOIN clientes c ON c.id = p.cliente_id
            WHERE pg.prestamo_id = %s {scope}
            """,
            (prestamo_id,) + sparams,
        )
        return float(cur.fetchone()[0] or 0)


def obtener_prestamo(pid: int, user_id: int, is_admin: bool):
    extra, params = _filtro_owner("c", user_id, is_admin)
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute(
            f"""
            SELECT p.id, p.cliente_id, c.nombre, c.identificacion, p.fecha, p.frecuencia,
                   p.cuotas, p.monto, p.tasa, p.interes_total, p.total_pagar,
                   p.valor_cuota, p.vencimiento, p.estado, p.pagadas,
                   p.proximo_pago, p.notas, p.mora_activa, p.tasa_mora_diaria
            FROM prestamos p
            JOIN clientes c ON c.id = p.cliente_id
            WHERE p.id = %s {extra}
            """,
            (pid,) + params,
        )
        return cur.fetchone()


def actualizar_prestamo(
    pid,
    fecha,
    frecuencia,
    cuotas,
    monto,
    tasa,
    vencimiento,
    user_id: int,
    is_admin: bool,
    *,
    mora_activa: bool | None = None,
    tasa_mora_diaria: float | None = None,
) -> bool:
    """
    Actualiza un préstamo ACTIVO y recalcula montos. No borra pagos.
    Lanza ValueError si el nuevo total es menor que lo cobrado o si cuotas < pagadas.
    """
    info = obtener_prestamo(pid, user_id, is_admin)
    if not info:
        return False
    if str(info[13]).upper() != "ACTIVO":
        return False

    pagadas = int(info[14])
    cuotas_i = int(cuotas)
    if cuotas_i < pagadas:
        raise ValueError("Las cuotas no pueden ser menores que las ya registradas como pagadas.")

    total_pagado = sum_pagos_por_prestamo(pid, user_id, is_admin)
    interes_total = float(monto) * (tasa / 100.0)
    total_pagar = float(monto) + interes_total
    if total_pagar + 1e-6 < total_pagado:
        raise ValueError(
            f"El nuevo total a pagar (${total_pagar:,.0f}) no puede ser menor que lo ya cobrado (${total_pagado:,.0f})."
        )

    valor_cuota = round(total_pagar / max(1, cuotas_i), 2)
    prox = proxima_fecha_pago(fecha, frecuencia, pagadas, cuotas_i)
    saldo = max(0.0, round(total_pagar - total_pagado, 2))
    nuevo_estado = "PAGADO" if pagadas >= cuotas_i or saldo <= 1 else "ACTIVO"

    mora_a = bool(info[17]) if mora_activa is None else bool(mora_activa)
    mora_t = float(info[18] or 0) if tasa_mora_diaria is None else float(tasa_mora_diaria or 0)

    extra, params = _filtro_owner("c", user_id, is_admin)
    cambios = []
    if str(info[3]) != fecha:
        cambios.append(("fecha", str(info[3]), fecha))
    if str(info[5]).lower() != frecuencia.lower():
        cambios.append(("frecuencia", str(info[5]), frecuencia))
    if int(info[6]) != cuotas_i:
        cambios.append(("cuotas", str(info[6]), str(cuotas_i)))
    if float(info[7]) != monto:
        cambios.append(("monto", str(info[7]), str(monto)))
    if float(info[8]) != tasa:
        cambios.append(("tasa", str(info[8]), str(tasa)))
    if str(info[9]) != vencimiento:
        cambios.append(("vencimiento", str(info[9]), vencimiento))

    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute(
            f"""
            UPDATE prestamos AS p SET
                fecha=%s, frecuencia=%s, cuotas=%s, monto=%s, tasa=%s,
                interes_total=%s, total_pagar=%s, valor_cuota=%s, vencimiento=%s, proximo_pago=%s,
                estado=%s, mora_activa=%s, tasa_mora_diaria=%s
            FROM clientes c
            WHERE p.cliente_id = c.id AND p.id = %s {extra}
            """,
            (
                fecha,
                frecuencia,
                cuotas_i,
                monto,
                tasa,
                interes_total,
                total_pagar,
                valor_cuota,
                vencimiento,
                prox,
                nuevo_estado,
                mora_a,
                mora_t,
                pid,
            )
            + params,
        )
        if cur.rowcount > 0:
            for campo, ant, nue in cambios:
                guardar_auditoria_prestamo(pid, user_id, campo, ant, nue)
        return cur.rowcount > 0


def editar_prestamo_inteligente(
    pid,
    fecha,
    frecuencia,
    cuotas,
    monto,
    tasa,
    vencimiento,
    user_id: int,
    is_admin: bool,
    *,
    mora_activa: bool | None = None,
    tasa_mora_diaria: float | None = None,
) -> bool:
    """
    Edición inteligente de préstamo: si no hay pagos, edita normal.
    Si hay pagos, ajusta sobre el saldo restante (recalcula como nuevo préstamo sobre saldo).
    No pierde historial, valida user_id.
    """
    info = obtener_prestamo(pid, user_id, is_admin)
    if not info:
        return False
    if str(info[13]).upper() != "ACTIVO":
        return False

    pagadas = int(info[14])
    cuotas_i = int(cuotas)
    if cuotas_i < pagadas:
        raise ValueError("Las cuotas no pueden ser menores que las ya registradas como pagadas.")

    total_pagado = sum_pagos_por_prestamo(pid, user_id, is_admin)

    # Si no hay pagos, editar normal
    if total_pagado <= 0:
        return actualizar_prestamo(
            pid, fecha, frecuencia, cuotas, monto, tasa, vencimiento,
            user_id, is_admin, mora_activa=mora_activa, tasa_mora_diaria=tasa_mora_diaria
        )

    # Si hay pagos, ajustar sobre saldo restante
    saldo_restante = float(info[10]) - total_pagado  # total_pagar original - total_pagado
    if saldo_restante <= 0:
        raise ValueError("El préstamo ya está completamente pagado o no tiene saldo pendiente.")

    # Nuevo monto base es el saldo restante
    nuevo_monto = saldo_restante
    nuevo_interes_total = nuevo_monto * (tasa / 100.0)
    nuevo_total_pagar = nuevo_monto + nuevo_interes_total

    # Validar que el nuevo total sea al menos el saldo restante (no negativo)
    if nuevo_total_pagar < saldo_restante:
        raise ValueError("El nuevo cálculo resulta en un total menor al saldo restante.")

    nuevo_valor_cuota = round(nuevo_total_pagar / max(1, cuotas_i - pagadas), 2)  # cuotas restantes
    prox = proxima_fecha_pago(fecha, frecuencia, pagadas, cuotas_i)
    nuevo_estado = "PAGADO" if pagadas >= cuotas_i else "ACTIVO"

    mora_a = bool(info[17]) if mora_activa is None else bool(mora_activa)
    mora_t = float(info[18] or 0) if tasa_mora_diaria is None else float(tasa_mora_diaria or 0)

    extra, params = _filtro_owner("c", user_id, is_admin)
    cambios = [
        ("fecha", str(info[3]), fecha),
        ("frecuencia", str(info[5]), frecuencia),
        ("cuotas", str(info[6]), str(cuotas_i)),
        ("monto_original", str(info[7]), str(monto)),  # Guardar el monto solicitado
        ("tasa", str(info[8]), str(tasa)),
        ("vencimiento", str(info[9]), vencimiento),
        ("edicion_inteligente", "false", "true"),  # Marcar que fue edición inteligente
    ]

    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute(
            f"""
            UPDATE prestamos AS p SET
                fecha=%s, frecuencia=%s, cuotas=%s, monto=%s, tasa=%s,
                interes_total=%s, total_pagar=%s, valor_cuota=%s, vencimiento=%s, proximo_pago=%s,
                estado=%s, mora_activa=%s, tasa_mora_diaria=%s
            FROM clientes c
            WHERE p.cliente_id = c.id AND p.id = %s {extra}
            """,
            (
                fecha,
                frecuencia,
                cuotas_i,
                nuevo_monto,  # Monto ajustado al saldo
                tasa,
                nuevo_interes_total,
                nuevo_total_pagar,
                nuevo_valor_cuota,
                vencimiento,
                prox,
                nuevo_estado,
                mora_a,
                mora_t,
                pid,
            )
            + params,
        )
        if cur.rowcount > 0:
            for campo, ant, nue in cambios:
                guardar_auditoria_prestamo(pid, user_id, campo, ant, nue)
        return cur.rowcount > 0


def actualizar_nota_prestamo(pid: int, nota: str, user_id: int, is_admin: bool) -> bool:
    extra, params = _filtro_owner("c", user_id, is_admin)
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute(
            f"""UPDATE prestamos p SET notas = %s
                FROM clientes c
                WHERE p.cliente_id = c.id AND p.id = %s {extra}""",
            (nota, pid) + params,
        )
        return cur.rowcount > 0


# ---------- pagos ----------
def registrar_pago(prestamo_id: int, fecha: str, valor: float, user_id: int, is_admin: bool, nota: str = "") -> tuple:
    """
    Registra un pago. Calcula interés por mora si el préstamo lo tiene habilitado.
    Permite pagos parciales y recalcula las cuotas pagadas según el total acumulado.
    """
    with get_conn() as conn:
        cur = conn.cursor()
        extra, params = _filtro_owner("c", user_id, is_admin)
        cur.execute(
            f"""
            SELECT p.total_pagar, p.pagadas, p.cuotas, p.estado, p.valor_cuota, p.fecha, p.frecuencia,
                   p.proximo_pago, p.mora_activa, p.tasa_mora_diaria
            FROM prestamos p
            JOIN clientes c ON c.id = p.cliente_id
            WHERE p.id = %s {extra}
            """,
            (prestamo_id,) + params,
        )
        row = cur.fetchone()
        if not row:
            raise ValueError("Préstamo no encontrado.")
        (
            total_pagar,
            pagadas,
            cuotas,
            estado,
            valor_cuota,
            fecha_ini,
            frecuencia,
            proximo_pago,
            mora_activa,
            tasa_mora_diaria,
        ) = row
        if estado == "PAGADO":
            raise ValueError("El préstamo ya está pagado.")

        interes_mora = calcular_interes_mora(
            float(valor_cuota),
            proximo_pago,
            fecha,
            bool(mora_activa),
            float(tasa_mora_diaria or 0),
        )

        # Cálculo de nuevas cuotas pagadas basado en el total acumulado cobrado
        total_cobrado_previo = sum_pagos_por_prestamo(prestamo_id, user_id, is_admin)
        nuevo_total_cobrado = total_cobrado_previo + float(valor)

        # El número de cuotas pagadas se redondea hacia abajo según el valor de la cuota base
        # Por ejemplo, si la cuota es 100 y ha pagado 250, lleva 2 cuotas pagadas y 50 de saldo a favor de la 3ra.
        nuevas_pagadas = int(nuevo_total_cobrado // float(valor_cuota))
        if nuevas_pagadas > int(cuotas):
            nuevas_pagadas = int(cuotas)

        saldo_restante = max(0.0, round(float(total_pagar) - nuevo_total_cobrado, 2))
        cuota_actual_del_pago = int(pagadas) + 1

        cur.execute(
            """
            INSERT INTO pagos (prestamo_id, fecha, valor, cuota, saldo_restante, interes_mora, nota)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            RETURNING id
            """,
            (prestamo_id, fecha, valor, cuota_actual_del_pago, saldo_restante, interes_mora, nota),
        )
        pid_pago = int(cur.fetchone()[0])

        nuevo_estado = "PAGADO" if saldo_restante <= 1 or nuevas_pagadas >= int(cuotas) else "ACTIVO"
        prox = proxima_fecha_pago(fecha_ini, frecuencia, nuevas_pagadas, int(cuotas))

        cur.execute(
            """
            UPDATE prestamos SET pagadas=%s, estado=%s, proximo_pago=%s WHERE id=%s
            """,
            (nuevas_pagadas, nuevo_estado, prox, prestamo_id),
        )
        return pid_pago, cuota_actual_del_pago, interes_mora, float(valor_cuota)


def listar_pagos(prestamo_id: Optional[int], user_id: int, is_admin: bool):
    scope, sparams = _filtro_owner("clientes", user_id, is_admin)
    base = f"""
        SELECT pagos.id,
               clientes.nombre,
               pagos.prestamo_id,
               pagos.fecha,
               pagos.valor,
               pagos.cuota,
               pagos.saldo_restante,
               prestamos.vencimiento,
               prestamos.estado,
               prestamos.proximo_pago,
               prestamos.notas,
               COALESCE(pagos.interes_mora, 0),
               COALESCE(pagos.nota, '')
        FROM pagos
        JOIN prestamos ON prestamos.id = pagos.prestamo_id
        JOIN clientes ON clientes.id = prestamos.cliente_id
        WHERE 1=1 {scope}
    """
    args = list(sparams)
    if prestamo_id:
        base += " AND pagos.prestamo_id = %s ORDER BY pagos.fecha DESC, pagos.id DESC"
        args.append(prestamo_id)
    else:
        base += " ORDER BY pagos.fecha DESC, pagos.id DESC"
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute(base, tuple(args))
        return cur.fetchall()


def obtener_pago_para_recibo(prestamo_id: int, pago_id: int, user_id: int, is_admin: bool):
    """Devuelve los datos completos de un pago validando alcance por rol/propietario."""
    scope, sparams = _filtro_owner("c", user_id, is_admin)
    with get_conn() as conn:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute(
            f"""
            SELECT pg.id AS pago_id,
                   pg.prestamo_id,
                   pg.fecha,
                   pg.valor,
                   pg.cuota,
                   pg.saldo_restante,
                   COALESCE(pg.interes_mora, 0) AS interes_mora,
                   COALESCE(pg.nota, '') AS nota,
                   p.valor_cuota AS valor_cuota_base,
                   p.cuotas AS total_cuotas,
                   c.nombre AS nombre_cliente
            FROM pagos pg
            JOIN prestamos p ON p.id = pg.prestamo_id
            JOIN clientes c ON c.id = p.cliente_id
            WHERE pg.id = %s
              AND pg.prestamo_id = %s
              {scope}
            """,
            (pago_id, prestamo_id) + sparams,
        )
        return cur.fetchone()


def eliminar_pago_y_actualizar(prestamo_id, pago_id, user_id: int, is_admin: bool) -> bool:
    extra, params = _filtro_owner("c", user_id, is_admin)
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute(
            f"""
            SELECT pagos.id FROM pagos
            JOIN prestamos p ON p.id = pagos.prestamo_id
            JOIN clientes c ON c.id = p.cliente_id
            WHERE pagos.id = %s AND pagos.prestamo_id = %s {extra}
            """,
            (pago_id, prestamo_id) + params,
        )
        if not cur.fetchone():
            return False

        cur.execute("DELETE FROM pagos WHERE id=%s", (pago_id,))

        # Recalcular el estado del préstamo
        total_cobrado = sum_pagos_por_prestamo(prestamo_id, user_id, is_admin)

        cur.execute(
            "SELECT total_pagar, valor_cuota, cuotas, fecha, frecuencia FROM prestamos WHERE id=%s",
            (prestamo_id,),
        )
        p = cur.fetchone()
        if not p: return False

        total_pagar, valor_cuota, cuotas, fecha_ini, frecuencia = p
        nuevas_pagadas = int(total_cobrado // float(valor_cuota))
        saldo_restante = max(0.0, round(float(total_pagar) - total_cobrado, 2))
        nuevo_estado = "PAGADO" if saldo_restante <= 1 or nuevas_pagadas >= int(cuotas) else "ACTIVO"
        prox = proxima_fecha_pago(fecha_ini, frecuencia, nuevas_pagadas, int(cuotas))

        cur.execute(
            "UPDATE prestamos SET pagadas=%s, estado=%s, proximo_pago=%s WHERE id=%s",
            (nuevas_pagadas, nuevo_estado, prox, prestamo_id),
        )
        return True


def eliminar_prestamo(pid: int, user_id: int, is_admin: bool) -> bool:
    """Elimina un préstamo y todos sus pagos asociados. Verifica permisos de owner."""
    extra, params = _filtro_owner("c", user_id, is_admin)
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute(
            f"SELECT p.id FROM prestamos p JOIN clientes c ON c.id = p.cliente_id WHERE p.id = %s {extra}",
            (pid,) + params,
        )
        if not cur.fetchone():
            return False
        cur.execute("DELETE FROM pagos WHERE prestamo_id = %s", (pid,))
        cur.execute("DELETE FROM prestamos WHERE id = %s", (pid,))
        return True


def sum_montos_por_rango(f_ini, f_fin, user_id: int, is_admin: bool) -> float:
    scope, sparams = _filtro_owner("c", user_id, is_admin)
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute(
            f"""
            SELECT COALESCE(SUM(p.monto), 0) FROM prestamos p
            JOIN clientes c ON c.id = p.cliente_id
            WHERE p.fecha BETWEEN %s AND %s {scope}
            """,
            (f_ini, f_fin) + sparams,
        )
        return float(cur.fetchone()[0] or 0)


def sum_pagos_por_rango(f_ini, f_fin, user_id: int, is_admin: bool) -> float:
    scope, sparams = _filtro_owner("c", user_id, is_admin)
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute(
            f"""
            SELECT COALESCE(SUM(pagos.valor), 0) FROM pagos
            JOIN prestamos p ON p.id = pagos.prestamo_id
            JOIN clientes c ON c.id = p.cliente_id
            WHERE pagos.fecha BETWEEN %s AND %s {scope}
            """,
            (f_ini, f_fin) + sparams,
        )
        return float(cur.fetchone()[0] or 0)


def total_prestado_en_rango(f_ini: str, f_fin: str, user_id: int, is_admin: bool) -> float:
    """Suma de montos de préstamos desembolsados (fecha del préstamo) en el rango."""
    return sum_montos_por_rango(f_ini, f_fin, user_id, is_admin)


def total_cobrado_en_rango(f_ini: str, f_fin: str, user_id: int, is_admin: bool) -> float:
    """Suma de valores cobrados (pagos) en el rango."""
    return sum_pagos_por_rango(f_ini, f_fin, user_id, is_admin)


def total_mora_cobrada_en_rango(f_ini: str, f_fin: str, user_id: int, is_admin: bool) -> float:
    scope, sparams = _filtro_owner("c", user_id, is_admin)
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute(
            f"""
            SELECT COALESCE(SUM(COALESCE(pagos.interes_mora, 0)), 0) FROM pagos
            JOIN prestamos p ON p.id = pagos.prestamo_id
            JOIN clientes c ON c.id = p.cliente_id
            WHERE pagos.fecha BETWEEN %s AND %s {scope}
            """,
            (f_ini, f_fin) + sparams,
        )
        return float(cur.fetchone()[0] or 0)


def desglose_capital_interes_cobrado_en_rango(
    f_ini: str, f_fin: str, user_id: int, is_admin: bool
) -> tuple[float, float]:
    """
    Estima capital y el interés del préstamo recuperados en el período,
    prorrateando la parte del pago que no es mora según monto/total_pagar del préstamo.
    """
    scope, sparams = _filtro_owner("c", user_id, is_admin)
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute(
            f"""
            SELECT COALESCE(SUM(
                (COALESCE(pagos.valor, 0) - COALESCE(pagos.interes_mora, 0)) *
                (p.monto / NULLIF(p.total_pagar, 0))
            ), 0),
            COALESCE(SUM(
                (COALESCE(pagos.valor, 0) - COALESCE(pagos.interes_mora, 0)) *
                (p.interes_total / NULLIF(p.total_pagar, 0))
            ), 0)
            FROM pagos
            JOIN prestamos p ON p.id = pagos.prestamo_id
            JOIN clientes c ON c.id = p.cliente_id
            WHERE pagos.fecha BETWEEN %s AND %s {scope}
            """,
            (f_ini, f_fin) + sparams,
        )
        row = cur.fetchone()
        return float(row[0] or 0), float(row[1] or 0)


def pagos_detalle_en_rango(
    f_ini: str, f_fin: str, user_id: int, is_admin: bool
) -> list[tuple]:
    """Filas: fecha (str), cliente, valor, cuota. Más reciente primero."""
    scope, sparams = _filtro_owner("c", user_id, is_admin)
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute(
            f"""
            SELECT pagos.fecha::text, c.nombre, pagos.valor, pagos.cuota
            FROM pagos
            JOIN prestamos p ON p.id = pagos.prestamo_id
            JOIN clientes c ON c.id = p.cliente_id
            WHERE pagos.fecha BETWEEN %s AND %s {scope}
            ORDER BY pagos.fecha DESC, pagos.id DESC
            """,
            (f_ini, f_fin) + sparams,
        )
        return cur.fetchall()


def contar_prestamos_en_mora(user_id: int, is_admin: bool) -> int:
    """Préstamos ACTIVOS con próximo pago vencido (misma lógica que cuotas vencidas)."""
    scope, sparams = _filtro_owner("c", user_id, is_admin)
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute(
            f"""
            SELECT COUNT(*) FROM prestamos p
            JOIN clientes c ON c.id = p.cliente_id
            WHERE p.estado = 'ACTIVO'
              AND p.proximo_pago IS NOT NULL AND TRIM(p.proximo_pago) <> ''
              AND (p.proximo_pago::date) < CURRENT_DATE
            {scope}
            """,
            sparams,
        )
        return int(cur.fetchone()[0] or 0)


def sum_pagos_hoy(user_id: int, is_admin: bool) -> float:
    hoy = datetime.now().strftime("%Y-%m-%d")
    return sum_pagos_por_rango(hoy, hoy, user_id, is_admin)


def export_database_sql() -> str:
    """Volcado simple en SQL (INSERTs) para descarga."""
    lines = [
        "-- Financiera NP backup (restaurar en BD vacía o truncar antes)",
        "BEGIN;",
        "TRUNCATE pagos, prestamos, clientes, usuarios RESTART IDENTITY CASCADE;",
    ]
    tables = [
        ("usuarios", "id, username, password_hash, rol, activo, creado_en"),
        ("clientes", "id, nombre, identificacion, telefono, barrio, direccion, owner_user_id"),
        (
            "prestamos",
            "id, cliente_id, fecha, frecuencia, cuotas, monto, tasa, interes_total, total_pagar, valor_cuota, vencimiento, estado, pagadas, proximo_pago, notas",
        ),
        ("pagos", "id, prestamo_id, fecha, valor, cuota, saldo_restante"),
    ]
    with get_conn() as conn:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        for table, _cols in tables:
            cur.execute(f"SELECT * FROM {table} ORDER BY 1")
            for row in cur.fetchall():
                cols = list(row.keys())
                vals = []
                for v in row.values():
                    if v is None:
                        vals.append("NULL")
                    elif isinstance(v, (int, float)):
                        vals.append(str(v))
                    elif isinstance(v, bool):
                        vals.append("TRUE" if v else "FALSE")
                    elif hasattr(v, "isoformat"):
                        esc = v.isoformat().replace("'", "''")
                        vals.append(f"'{esc}'")
                    else:
                        esc = str(v).replace("'", "''")
                        vals.append(f"'{esc}'")
                lines.append(
                    f"INSERT INTO {table} ({', '.join(cols)}) VALUES ({', '.join(vals)});"
                )
    lines.append("COMMIT;")
    return "\n".join(lines)


def restore_database_sql(sql: str) -> None:
    """
    Ejecuta un volcado .sql generado por export_database_sql() (BEGIN/TRUNCATE/INSERTs/COMMIT).
    Valida que solo contenga sentencias seguras para prevenir SQL injection.
    """
    import re
    chunks: list[str] = []
    buf: list[str] = []
    for line in sql.splitlines():
        s = line.strip()
        if not s or s.startswith("--"):
            continue
        buf.append(line)
        if s.endswith(";"):
            stmt = "\n".join(buf).strip()
            if stmt:
                chunks.append(stmt)
            buf = []
    if buf:
        stmt = "\n".join(buf).strip()
        if stmt:
            chunks.append(stmt)

    allowed_patterns = re.compile(
        r"^(BEGIN|COMMIT|TRUNCATE\s+TABLE\s+\w+|INSERT\s+INTO\s+\w+)",
        re.IGNORECASE,
    )
    
    conn = psycopg2.connect(_dsn())
    try:
        conn.autocommit = False
        cur = conn.cursor()
        for stmt in chunks:
            if not allowed_patterns.match(stmt):
                raise ValueError(f"Sentencia no permitida: {stmt[:50]}...")
            cur.execute(stmt)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


# ---------- WhatsApp / Evolution API ----------
def get_instance_by_name(instance_name):
    with get_conn() as conn:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("SELECT * FROM whatsapp_instances WHERE instance_name = %s", (instance_name,))
        return cur.fetchone()


def get_user_instance(user_id):
    with get_conn() as conn:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("SELECT * FROM whatsapp_instances WHERE user_id = %s ORDER BY created_at DESC LIMIT 1", (user_id,))
        return cur.fetchone()


def create_whatsapp_instance(user_id, instance_name):
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO whatsapp_instances (user_id, instance_name, status) VALUES (%s, %s, 'pending')",
            (user_id, instance_name),
        )


def update_instance_status(instance_name, status, phone_number=None, qr_code=None):
    with get_conn() as conn:
        cur = conn.cursor()
        if status == "connected":
            cur.execute(
                "UPDATE whatsapp_instances SET status = %s, phone_number = %s, connected_at = NOW() WHERE instance_name = %s",
                (status, phone_number, instance_name),
            )
        elif qr_code:
            cur.execute(
                "UPDATE whatsapp_instances SET status = %s, qr_code = %s WHERE instance_name = %s",
                (status, qr_code, instance_name),
            )
        else:
            cur.execute(
                "UPDATE whatsapp_instances SET status = %s WHERE instance_name = %s",
                (status, instance_name),
            )


def save_whatsapp_message(instance_id, from_number, to_number, content, direction, message_type="text", media_url=None):
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute(
            """INSERT INTO whatsapp_messages
               (instance_id, from_number, to_number, content, direction, message_type, media_url)
               VALUES (%s, %s, %s, %s, %s, %s, %s)""",
            (instance_id, from_number, to_number, content, direction, message_type, media_url),
        )


def get_or_create_session(instance_id, client_phone):
    with get_conn() as conn:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute(
            """INSERT INTO whatsapp_sessions (instance_id, client_phone, state, context)
               VALUES (%s, %s, 'idle', '{}')
               ON CONFLICT (instance_id, client_phone)
               DO UPDATE SET last_activity = NOW()
               RETURNING *""",
            (instance_id, client_phone),
        )
        return cur.fetchone()


def update_session_state(instance_id, client_phone, state, context=None):
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute(
            """UPDATE whatsapp_sessions
               SET state = %s, context = %s::jsonb, last_activity = NOW()
               WHERE instance_id = %s AND client_phone = %s""",
            (state, context, instance_id, client_phone),
        )


def get_session(instance_id, client_phone):
    with get_conn() as conn:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute(
            "SELECT * FROM whatsapp_sessions WHERE instance_id = %s AND client_phone = %s",
            (instance_id, client_phone),
        )
        return cur.fetchone()


def get_vencimientos_hoy():
    """Devuelve préstamos con próximo pago hoy para alertas automáticas."""
    with get_conn() as conn:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("""
            SELECT p.id, c.nombre, c.telefono, p.valor_cuota, p.proximo_pago
            FROM prestamos p
            JOIN clientes c ON c.id = p.cliente_id
            WHERE p.estado = 'ACTIVO'
              AND p.proximo_pago::date = CURRENT_DATE
            ORDER BY c.nombre
        """)
        return cur.fetchall()


# ---------- Rate Limiting ----------
def record_login_attempt(ip_address, username=None):
    """Registra un intento fallido de login."""
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO login_attempts (ip_address, username) VALUES (%s, %s)",
            (ip_address, username),
        )


def get_failed_attempts(ip_address, minutes=15):
    """Obtiene cantidad de intentos fallidos recientes desde una IP."""
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute(
            """SELECT COUNT(*) FROM login_attempts
               WHERE ip_address = %s AND failed_at > NOW() - INTERVAL '%s minutes'""",
            (ip_address, minutes),
        )
        return int(cur.fetchone()[0])


def cleanup_old_attempts():
    """Limpia intentos antiguos (más de 24 horas)."""
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute("DELETE FROM login_attempts WHERE failed_at < NOW() - INTERVAL '24 hours'")


# ---------- Backups por Usuario ----------
def export_user_data(user_id):
    """Exporta clientes, préstamos y pagos de un usuario a JSON."""
    with get_conn() as conn:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        
        cur.execute("SELECT * FROM clientes WHERE owner_user_id = %s ORDER BY id", (user_id,))
        clientes = [dict(r) for r in cur.fetchall()]
        
        # Obtener IDs de clientes para filtrar préstamos
        cliente_ids = [c['id'] for c in clientes]
        prestamos = []
        pagos = []
        
        if cliente_ids:
            cur.execute("SELECT * FROM prestamos WHERE cliente_id = ANY(%s) ORDER BY id", (cliente_ids,))
            prestamos = [dict(r) for r in cur.fetchall()]
            
            prestamo_ids = [p['id'] for p in prestamos]
            if prestamo_ids:
                cur.execute("SELECT * FROM pagos WHERE prestamo_id = ANY(%s) ORDER BY id", (prestamo_ids,))
                pagos = [dict(r) for r in cur.fetchall()]
        
        return {
            "version": "1.0",
            "user_id": user_id,
            "clientes": clientes,
            "prestamos": prestamos,
            "pagos": pagos,
        }


def restore_user_data(user_id, data):
    """Restaura datos de un usuario desde JSON. Elimina datos existentes primero."""
    with get_conn() as conn:
        cur = conn.cursor()
        try:
            # 1. Eliminar pagos existentes
            cur.execute("""
                DELETE FROM pagos WHERE prestamo_id IN (
                    SELECT id FROM prestamos WHERE cliente_id IN (
                        SELECT id FROM clientes WHERE owner_user_id = %s
                    )
                )
            """, (user_id,))
            
            # 2. Eliminar préstamos existentes
            cur.execute("""
                DELETE FROM prestamos WHERE cliente_id IN (
                    SELECT id FROM clientes WHERE owner_user_id = %s
                )
            """, (user_id,))
            
            # 3. Eliminar clientes existentes
            cur.execute("DELETE FROM clientes WHERE owner_user_id = %s", (user_id,))
            
            # 4. Insertar clientes
            for c in data.get("clientes", []):
                cols = [k for k in c.keys() if k != 'id']
                vals = [c[k] for k in cols]
                placeholders = ", ".join(["%s"] * len(cols))
                col_names = ", ".join(cols)
                cur.execute(f"INSERT INTO clientes ({col_names}) VALUES ({placeholders})", vals)
            
            # 5. Insertar préstamos
            for p in data.get("prestamos", []):
                cols = [k for k in p.keys() if k != 'id']
                vals = [p[k] for k in cols]
                placeholders = ", ".join(["%s"] * len(cols))
                col_names = ", ".join(cols)
                cur.execute(f"INSERT INTO prestamos ({col_names}) VALUES ({placeholders})", vals)
            
            # 6. Insertar pagos
            for pg in data.get("pagos", []):
                cols = [k for k in pg.keys() if k != 'id']
                vals = [pg[k] for k in cols]
                placeholders = ", ".join(["%s"] * len(cols))
                col_names = ", ".join(cols)
                cur.execute(f"INSERT INTO pagos ({col_names}) VALUES ({placeholders})", vals)
                
        except Exception as e:
            conn.rollback()
            raise e
