"""
Crea tablas en PostgreSQL si no existen.
Uso: DATABASE_URL=... python init_db.py
"""
import os
import sys

import psycopg2

DDL = """
CREATE TABLE IF NOT EXISTS usuarios (
    id SERIAL PRIMARY KEY,
    username VARCHAR(80) UNIQUE NOT NULL,
    password_hash TEXT NOT NULL,
    rol VARCHAR(20) NOT NULL DEFAULT 'usuario' CHECK (rol IN ('admin', 'usuario')),
    activo BOOLEAN NOT NULL DEFAULT TRUE,
    creado_en TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS clientes (
    id SERIAL PRIMARY KEY,
    nombre TEXT,
    identificacion TEXT NOT NULL,
    telefono TEXT,
    barrio TEXT,
    direccion TEXT,
    owner_user_id INTEGER NOT NULL REFERENCES usuarios(id) ON DELETE CASCADE,
    UNIQUE (owner_user_id, identificacion)
);

CREATE TABLE IF NOT EXISTS prestamos (
    id SERIAL PRIMARY KEY,
    cliente_id INTEGER NOT NULL REFERENCES clientes(id) ON DELETE CASCADE,
    fecha TEXT NOT NULL,
    frecuencia TEXT NOT NULL,
    cuotas INTEGER NOT NULL,
    monto DOUBLE PRECISION NOT NULL,
    tasa DOUBLE PRECISION NOT NULL,
    interes_total DOUBLE PRECISION NOT NULL,
    total_pagar DOUBLE PRECISION NOT NULL,
    valor_cuota DOUBLE PRECISION NOT NULL,
    vencimiento TEXT NOT NULL,
    estado TEXT NOT NULL DEFAULT 'ACTIVO',
    pagadas INTEGER NOT NULL DEFAULT 0,
    proximo_pago TEXT,
    notas TEXT,
    mora_activa BOOLEAN NOT NULL DEFAULT FALSE,
    tasa_mora_diaria DOUBLE PRECISION NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS pagos (
    id SERIAL PRIMARY KEY,
    prestamo_id INTEGER NOT NULL REFERENCES prestamos(id) ON DELETE CASCADE,
    fecha TEXT NOT NULL,
    valor DOUBLE PRECISION NOT NULL,
    cuota INTEGER NOT NULL,
    saldo_restante DOUBLE PRECISION NOT NULL,
    interes_mora DOUBLE PRECISION NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_prestamos_estado ON prestamos(estado);
CREATE INDEX IF NOT EXISTS idx_pagos_prestamo ON pagos(prestamo_id);
CREATE INDEX IF NOT EXISTS idx_clientes_owner ON clientes(owner_user_id);
"""


def main():
    url = os.environ.get("DATABASE_URL")
    if not url:
        print("Defina DATABASE_URL", file=sys.stderr)
        sys.exit(1)
    if url.startswith("postgres://"):
        url = url.replace("postgres://", "postgresql://", 1)
    conn = psycopg2.connect(url)
    try:
        cur = conn.cursor()
        cur.execute(DDL)
        conn.commit()
        print("Tablas listas.")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
