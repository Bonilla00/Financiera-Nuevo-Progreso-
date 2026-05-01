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
        "ALTER TABLE usuarios DROP CONSTRAINT IF EXISTS usuarios_rol_check",
        "ALTER TABLE usuarios ADD CONSTRAINT usuarios_rol_check CHECK (rol IN ('admin', 'cobrador', 'solo_lectura', 'usuario'))",
    ]
    with get_conn() as conn:
        cur = conn.cursor()
        for s in stmts:
            try:
                cur.execute(s)
            except:
                pass
    ensure_auditoria_table()
    ensure_gastos_table()


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
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) FROM usuarios")
        return int(cur.fetchone()[0])


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
            SELECT id, username, password_hash, rol, activo
            FROM usuarios WHERE LOWER(username) = LOWER(%s)
            """,
            (username.strip(),),
        )
        return cur.fetchone()


def obtener_usuario_por_id(uid: int) -> Optional[dict]:
    with get_conn() as conn:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute(
            "SELECT id, username, password_hash, rol, activo FROM usuarios WHERE id = %s",
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


def admin_actualizar_usuario(uid: int, rol: str, activo: bool) -> None:
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute(
            "UPDATE usuarios SET rol = %s, activo = %s WHERE id = %s",
            (rol, activo, uid),
        )


def admin_cambiar_password(uid: int, password_hash: str) -> None:
    actualizar_password_usuario(uid, password_hash)


def admin_toggle_activo(uid: int):
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute("UPDATE usuarios SET activo = NOT activo WHERE id = %s RETURNING activo", (uid,))
        return cur.fetchone()[0]


def admin_eliminar_usuario(uid: int):
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute("DELETE FROM usuarios WHERE id = %s", (uid,))
        return cur.rowcount > 0


def admin_reset_password(uid: int, password_hash: str) -> None:
    actualizar_password_usuario(uid, password_hash)


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
    extra, params = _filtro_owner("c", user_id, is_admin)
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
    extra, sparams = _filtro_owner("c", user_id, is_admin)
    params = [f"%{q}%", f"%{q}%", f"%{q}%"] + list(sparams)
    query = f"""
        SELECT id, nombre, identificacion, telefono, barrio
        FROM clientes c
        WHERE (nombre ILIKE %s OR identificacion ILIKE %s OR telefono ILIKE %s)
        {extra}
        ORDER BY nombre ASC LIMIT 20
    """
    with get_conn() as conn:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute(query, params)
        return cur.fetchall()


def listar_clientes_filtrado(filtro: str, user_id: int, is_admin: bool) -> list[tuple]:
    """filtro: todo | activo | pago_hoy | pendiente_hoy | sin_activo"""
    extra, sparams = _filtro_owner("c", user_id, is_admin)
    hoy = datetime.now().strftime("%Y-%m-%d")
    q = f"""
        SELECT c.id, c.nombre, c.identificacion, c.telefono, c.barrio, c.direccion
        FROM clientes c
        WHERE 1=1 {extra}
    """
    args: list = list(sparams)
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


def ensure_gastos_table() -> None:
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute("""
            CREATE TABLE IF NOT EXISTS gastos (
                id SERIAL PRIMARY KEY,
                user_id INTEGER NOT NULL,
                fecha DATE NOT NULL DEFAULT CURRENT_DATE,
                descripcion TEXT NOT NULL,
                valor DOUBLE PRECISION NOT NULL,
                categoria VARCHAR(50) NOT NULL,
                creado_en TIMESTAMP NOT NULL DEFAULT NOW()
            )
        """)


def registrar_gasto(user_id: int, fecha: str, descripcion: str, valor: float, categoria: str) -> int:
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO gastos (user_id, fecha, descripcion, valor, categoria)
            VALUES (%s, %s, %s, %s, %s)
            RETURNING id
            """,
            (user_id, fecha, descripcion, valor, categoria),
        )
        return int(cur.fetchone()[0])


def listar_gastos_mes(user_id: int, is_admin: bool, año: int = None, mes: int = None) -> list[tuple]:
    from datetime import datetime
    if año is None:
        año = datetime.now().year
    if mes is None:
        mes = datetime.now().month
    fecha_ini = f"{año}-{mes:02d}-01"
    if mes == 12:
        fecha_fin = f"{año + 1}-01-01"
    else:
        fecha_fin = f"{año}-{mes + 1:02d}-01"
    scope, sparams = _filtro_owner("g", user_id, is_admin)
    q = f"""
        SELECT g.id, g.fecha, g.descripcion, g.valor, g.categoria
        FROM gastos g
        WHERE g.fecha >= %s AND g.fecha < %s {scope}
        ORDER BY g.fecha DESC, g.id DESC
    """
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute(q, (fecha_ini, fecha_fin) + sparams)
        return cur.fetchall()


def total_gastos_mes(user_id: int, is_admin: bool, año: int = None, mes: int = None) -> float:
    rows = listar_gastos_mes(user_id, is_admin, año, mes)
    return sum(float(r[3] or 0) for r in rows)


def eliminar_gasto(gasto_id: int, user_id: int, is_admin: bool) -> bool:
    scope, sparams = _filtro_owner("g", user_id, is_admin)
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute(f"DELETE FROM gastos WHERE id = %s {scope}", (gasto_id,) + sparams)
        return cur.rowcount > 0


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


def actualizar_nota_prestamo(pid, nota, user_id: int, is_admin: bool) -> bool:
    extra, params = _filtro_owner("c", user_id, is_admin)
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute(
            f"""
            UPDATE prestamos p SET notas = %s
            FROM clientes c
            WHERE p.cliente_id = c.id AND p.id = %s {extra}
            """,
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
    scope, sparams = _filtro_owner("c", user_id, is_admin)
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
    Advertencia: borra y repuebla datos según el script.
    """
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

    conn = psycopg2.connect(_dsn())
    try:
        conn.autocommit = False
        cur = conn.cursor()
        for stmt in chunks:
            cur.execute(stmt)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
