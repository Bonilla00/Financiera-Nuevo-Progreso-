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
    ]
    with get_conn() as conn:
        cur = conn.cursor()
        for s in stmts:
            cur.execute(s)


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


def obtener_usuario_por_username(username: str) -> Optional[tuple]:
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT id, username, password_hash, rol, activo
            FROM usuarios WHERE LOWER(username) = LOWER(%s)
            """,
            (username.strip(),),
        )
        return cur.fetchone()


def obtener_usuario_por_id(uid: int) -> Optional[tuple]:
    with get_conn() as conn:
        cur = conn.cursor()
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
            SELECT c.id, c.nombre, c.identificacion, c.telefono, c.barrio, c.direccion
            FROM clientes c WHERE c.id = %s {extra}
            """,
            (cid,) + params,
        )
        return cur.fetchone()


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
    scope, sparams = _filtro_owner("c", user_id, is_admin)
    q = f"""
        SELECT p.id, c.nombre, c.identificacion, p.monto, p.tasa, p.cuotas,
               p.valor_cuota, p.fecha, p.vencimiento, p.estado, p.pagadas,
               p.total_pagar, p.frecuencia, p.proximo_pago, p.notas,
               c.id, c.telefono, c.direccion, c.barrio,
               p.mora_activa, p.tasa_mora_diaria
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
        cur = conn.cursor()
        cur.execute(q, tuple(args))
        return cur.fetchall()


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
    pid, fecha, frecuencia, cuotas, monto, tasa, vencimiento, user_id: int, is_admin: bool
) -> bool:
    info = obtener_prestamo(pid, user_id, is_admin)
    if not info:
        return False
    interes_total = float(monto) * (float(tasa) / 100.0)
    total_pagar = float(monto) + interes_total
    valor_cuota = round(total_pagar / max(1, int(cuotas)), 2)
    pagadas = int(info[14])
    prox = proxima_fecha_pago(fecha, frecuencia, pagadas, int(cuotas))
    extra, params = _filtro_owner("c", user_id, is_admin)
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute(
            f"""
            UPDATE prestamos AS p SET
                fecha=%s, frecuencia=%s, cuotas=%s, monto=%s, tasa=%s,
                interes_total=%s, total_pagar=%s, valor_cuota=%s, vencimiento=%s, proximo_pago=%s
            FROM clientes c
            WHERE p.cliente_id = c.id AND p.id = %s {extra}
            """,
            (
                fecha,
                frecuencia,
                cuotas,
                monto,
                tasa,
                interes_total,
                total_pagar,
                valor_cuota,
                vencimiento,
                prox,
                pid,
            )
            + params,
        )
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
def registrar_pago(prestamo_id: int, fecha: str, valor: float, user_id: int, is_admin: bool) -> tuple:
    """
    Registra un pago. Calcula interés por mora si el préstamo lo tiene habilitado.
    Retorna (pago_id, num_cuota, interes_mora, valor_cuota_base).
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
        min_cuota_mora = float(valor_cuota) + interes_mora

        cuota_num = int(pagadas) + 1
        total_pagado = (int(pagadas) * float(valor_cuota)) + float(valor)
        saldo_restante = max(0.0, round(float(total_pagar) - total_pagado, 2))

        cur.execute(
            """
            INSERT INTO pagos (prestamo_id, fecha, valor, cuota, saldo_restante, interes_mora)
            VALUES (%s, %s, %s, %s, %s, %s)
            RETURNING id
            """,
            (prestamo_id, fecha, valor, cuota_num, saldo_restante, interes_mora),
        )
        pid_pago = int(cur.fetchone()[0])

        if interes_mora > 0:
            ok_cuota = float(valor) + 1e-6 >= min_cuota_mora * 0.999
        else:
            ok_cuota = float(valor) + 1e-6 >= float(valor_cuota) * 0.999
        nuevas_pagadas = int(pagadas) + 1 if ok_cuota else int(pagadas)
        nuevo_estado = "PAGADO" if nuevas_pagadas >= int(cuotas) or saldo_restante <= 1 else "ACTIVO"
        prox = proxima_fecha_pago(fecha_ini, frecuencia, nuevas_pagadas, int(cuotas))

        cur.execute(
            """
            UPDATE prestamos SET pagadas=%s, estado=%s, proximo_pago=%s WHERE id=%s
            """,
            (nuevas_pagadas, nuevo_estado, prox, prestamo_id),
        )
        return pid_pago, cuota_num, interes_mora, float(valor_cuota)


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
               COALESCE(pagos.interes_mora, 0)
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

        cur.execute("SELECT valor, cuota FROM pagos WHERE id=%s", (pago_id,))
        row = cur.fetchone()
        if not row:
            return False

        cur.execute("DELETE FROM pagos WHERE id=%s", (pago_id,))

        cur.execute(
            "SELECT pagadas, cuotas, total_pagar, valor_cuota, fecha, frecuencia FROM prestamos WHERE id=%s",
            (prestamo_id,),
        )
        p = cur.fetchone()
        if not p:
            return False

        pagadas, cuotas, total_pagar, valor_cuota, fecha_ini, frecuencia = p
        nuevas_pagadas = max(0, int(pagadas) - 1)
        total_pagado = nuevas_pagadas * float(valor_cuota)
        saldo_restante = max(0.0, round(float(total_pagar) - total_pagado, 2))
        prox = proxima_fecha_pago(fecha_ini, frecuencia, nuevas_pagadas, int(cuotas))
        estado = "PAGADO" if nuevas_pagadas >= int(cuotas) or saldo_restante <= 1 else "ACTIVO"

        cur.execute(
            "UPDATE prestamos SET pagadas=%s, estado=%s, proximo_pago=%s WHERE id=%s",
            (nuevas_pagadas, estado, prox, prestamo_id),
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
