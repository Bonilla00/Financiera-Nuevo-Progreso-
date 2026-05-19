"""
Tests para la capa de datos PostgreSQL.
Requiere DATABASE_URL configurada en el entorno.

Uso:
    DATABASE_URL=postgresql://... python -m pytest tests/test_db.py
    DATABASE_URL=postgresql://... python -m unittest tests.test_db
"""
import os
import unittest

import db


@unittest.skipIf(not os.environ.get("DATABASE_URL"), "DATABASE_URL no configurada")
class TestDbPostgreSQL(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        db.ensure_schema_migrations()

    def setUp(self):
        with db.get_conn() as conn:
            cur = conn.cursor()
            cur.execute("DELETE FROM pagos")
            cur.execute("DELETE FROM prestamos")
            cur.execute("DELETE FROM clientes")
            cur.execute("DELETE FROM usuarios WHERE username LIKE 'test_%'")
            cur.execute(
                "INSERT INTO usuarios (username, password_hash, rol, activo, debe_cambiar_password) "
                "VALUES (%s, %s, %s, %s, %s) ON CONFLICT (username) DO NOTHING",
                ("test_user", "hash", "cobrador", True, False),
            )
            cur.execute("SELECT id FROM usuarios WHERE username = 'test_user'")
            self.user_id = cur.fetchone()[0]

    def _crear_prestamo_base(self, user_id=None, is_admin=False):
        uid = user_id or self.user_id
        cid = db.get_or_create_cliente(
            "Cliente Prueba",
            "CC-TEST-001",
            "3000000000",
            "Centro",
            "Calle 1",
            uid,
        )
        pid = db.nuevo_prestamo(
            cid,
            "2026-01-01",
            "mensual",
            2,
            1000.0,
            10.0,
            100.0,
            1100.0,
            550.0,
            "2026-03-01",
            uid,
            is_admin,
        )
        return cid, pid

    def test_nuevo_prestamo_duplicado_lanza_error(self):
        cid, _ = self._crear_prestamo_base()
        with self.assertRaises(ValueError):
            db.nuevo_prestamo(
                cid,
                "2026-01-01",
                "mensual",
                2,
                1000.0,
                10.0,
                100.0,
                1100.0,
                550.0,
                "2026-03-01",
                self.user_id,
                False,
            )

    def test_registrar_y_eliminar_pago_actualiza_estado(self):
        _, pid = self._crear_prestamo_base()

        db.registrar_pago(pid, "2026-01-10", 550.0, self.user_id, False)
        prestamo = db.obtener_prestamo(pid, self.user_id, False)
        self.assertEqual(prestamo[13], "ACTIVO")
        self.assertEqual(prestamo[14], 1)

        pagos = db.listar_pagos(pid, self.user_id, False)
        self.assertEqual(len(pagos), 1)
        pago_id = pagos[0][0]

        ok = db.eliminar_pago_y_actualizar(pid, pago_id, self.user_id, False)
        self.assertTrue(ok)

        prestamo = db.obtener_prestamo(pid, self.user_id, False)
        self.assertEqual(prestamo[13], "ACTIVO")
        self.assertEqual(prestamo[14], 0)

    def test_proxima_fecha_pago_por_frecuencia(self):
        self.assertEqual(db.proxima_fecha_pago("2026-01-01", "diaria", 0, 3), "2026-01-02")
        self.assertEqual(db.proxima_fecha_pago("2026-01-01", "semanal", 0, 3), "2026-01-08")
        self.assertEqual(db.proxima_fecha_pago("2026-01-01", "quincenal", 0, 3), "2026-01-16")
        self.assertEqual(db.proxima_fecha_pago("2026-01-01", "mensual", 0, 3), "2026-01-31")

    def test_prestamo_queda_pagado_al_completar_cuotas(self):
        _, pid = self._crear_prestamo_base()

        db.registrar_pago(pid, "2026-01-10", 550.0, self.user_id, False)
        db.registrar_pago(pid, "2026-02-10", 550.0, self.user_id, False)

        prestamo = db.obtener_prestamo(pid, self.user_id, False)
        self.assertEqual(prestamo[13], "PAGADO")
        self.assertEqual(prestamo[14], 2)

    def test_calcular_interes_mora(self):
        mora = db.calcular_interes_mora(100.0, "2026-01-01", "2026-01-11", True, 1.0)
        self.assertEqual(mora, 10.0)

    def test_calcular_interes_mora_sin_mora(self):
        mora = db.calcular_interes_mora(100.0, "2026-01-01", "2026-01-11", False, 1.0)
        self.assertEqual(mora, 0.0)

    def test_calcular_interes_mora_puntual(self):
        mora = db.calcular_interes_mora(100.0, "2026-01-10", "2026-01-10", True, 1.0)
        self.assertEqual(mora, 0.0)

    def test_scope_owner_admin_ve_todo(self):
        self._crear_prestamo_base()
        todos = db.listar_prestamos("", (), 0, True)
        self.assertGreater(len(todos), 0)

    def test_scope_owner_no_admin_solo_sus_datos(self):
        self._crear_prestamo_base()
        with db.get_conn() as conn:
            cur = conn.cursor()
            cur.execute(
                "INSERT INTO usuarios (username, password_hash, rol, activo, debe_cambiar_password) "
                "VALUES (%s, %s, %s, %s, %s) ON CONFLICT (username) DO NOTHING",
                ("test_other", "hash", "cobrador", True, False),
            )
            cur.execute("SELECT id FROM usuarios WHERE username = 'test_other'")
            other_id = cur.fetchone()[0]

        otros = db.listar_prestamos("", (), other_id, False)
        self.assertEqual(len(otros), 0)

    def test_get_or_create_cliente_reutiliza(self):
        uid = self.user_id
        c1 = db.get_or_create_cliente("Reutilizable", "ID-REUSE", "111", "", "", uid)
        c2 = db.get_or_create_cliente("Reutilizable", "ID-REUSE", "111", "", "", uid)
        self.assertEqual(c1, c2)

    def test_listar_clientes_filtrado(self):
        self._crear_prestamo_base()
        todos = db.listar_clientes_filtrado("todo", self.user_id, False)
        self.assertGreater(len(todos), 0)

        activos = db.listar_clientes_filtrado("activo", self.user_id, False)
        self.assertGreater(len(activos), 0)


if __name__ == "__main__":
    unittest.main()
