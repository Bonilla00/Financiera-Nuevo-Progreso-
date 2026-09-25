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

    def test_renovar_prestamo_descontando_ultima_cuota(self):
        # Caso B y G: Préstamo con última cuota pendiente y nuevo_monto > deuda
        _, pid = self._crear_prestamo_base()
        # Pagar 1 de 2 cuotas (saldo pendiente = 550.0)
        db.registrar_pago(pid, "2026-01-10", 550.0, self.user_id, False)
        
        nuevo_pid, pid_ant, desembolsado = db.renovar_prestamo(
            pid_anterior=pid,
            nuevo_monto=2000.0,
            nueva_tasa=10.0,
            nuevas_cuotas=2,
            nueva_frecuencia="mensual",
            nuevo_vencimiento="2026-04-01",
            descontar_ultima_cuota=True,
            user_id=self.user_id,
            is_admin=False,
            observaciones="Renovación de prueba con asistente"
        )

        self.assertEqual(pid_ant, pid)
        self.assertEqual(desembolsado, 1450.0) # 2000 - 550

        p_ant = db.obtener_prestamo(pid, self.user_id, False)
        self.assertEqual(p_ant[13], "RENOVADO")
        self.assertEqual(p_ant[14], 2) # Cuotas pagadas completas

        p_nuevo = db.obtener_prestamo(nuevo_pid, self.user_id, False)
        self.assertEqual(p_nuevo[7], 2000.0) # Monto total contratado
        self.assertEqual(p_nuevo[13], "ACTIVO")
        self.assertEqual(p_nuevo[14], 0) # 0 cuotas pagadas
        self.assertEqual(p_nuevo[17], pid) # prestamo_anterior_id

    def test_renovar_prestamo_sin_descontar_ultima_cuota(self):
        # Caso A: Checkbox desmarcado
        _, pid = self._crear_prestamo_base()
        db.registrar_pago(pid, "2026-01-10", 550.0, self.user_id, False)

        nuevo_pid, pid_ant, desembolsado = db.renovar_prestamo(
            pid_anterior=pid,
            nuevo_monto=3000.0,
            nueva_tasa=10.0,
            nuevas_cuotas=2,
            nueva_frecuencia="mensual",
            nuevo_vencimiento="2026-04-01",
            descontar_ultima_cuota=False,
            user_id=self.user_id,
            is_admin=False
        )

        self.assertEqual(pid_ant, pid)
        self.assertEqual(desembolsado, 3000.0) # Sin descuento

        p_ant = db.obtener_prestamo(pid, self.user_id, False)
        self.assertEqual(p_ant[13], "RENOVADO")
        self.assertEqual(p_ant[14], 1) # Mantiene 1 cuota pagada

        p_nuevo = db.obtener_prestamo(nuevo_pid, self.user_id, False)
        self.assertEqual(p_nuevo[7], 3000.0)

    def test_renovar_prestamo_completamente_pagado_lanza_error(self):
        # Caso C y D: Préstamo ya pagado por completo
        _, pid = self._crear_prestamo_base()
        db.registrar_pago(pid, "2026-01-10", 550.0, self.user_id, False)
        db.registrar_pago(pid, "2026-02-10", 550.0, self.user_id, False)

        puede, mensaje, _ = db.puede_renovar_prestamo(pid, self.user_id, False)
        self.assertFalse(puede)
        self.assertIn("completamente pagado", mensaje)

        with self.assertRaises(ValueError):
            db.renovar_prestamo(
                pid_anterior=pid,
                nuevo_monto=2000.0,
                nueva_tasa=10.0,
                nuevas_cuotas=2,
                nueva_frecuencia="mensual",
                nuevo_vencimiento="2026-04-01",
                descontar_ultima_cuota=True,
                user_id=self.user_id,
                is_admin=False
            )

    def test_renovar_prestamo_monto_menor_a_deuda_lanza_error(self):
        # Caso E: Nuevo préstamo menor que deuda a descontar
        _, pid = self._crear_prestamo_base()
        db.registrar_pago(pid, "2026-01-10", 550.0, self.user_id, False)

        with self.assertRaises(ValueError) as ctx:
            db.renovar_prestamo(
                pid_anterior=pid,
                nuevo_monto=500.0, # Deuda pendiente es 550.0
                nueva_tasa=10.0,
                nuevas_cuotas=2,
                nueva_frecuencia="mensual",
                nuevo_vencimiento="2026-04-01",
                descontar_ultima_cuota=True,
                user_id=self.user_id,
                is_admin=False
            )
        self.assertIn("No es posible descontar", str(ctx.exception))

    def test_renovar_prestamo_monto_igual_a_deuda(self):
        # Caso F: Nuevo préstamo igual a deuda
        _, pid = self._crear_prestamo_base()
        db.registrar_pago(pid, "2026-01-10", 550.0, self.user_id, False)

        nuevo_pid, _, desembolsado = db.renovar_prestamo(
            pid_anterior=pid,
            nuevo_monto=550.0, # Igual a deuda pendiente (550.0)
            nueva_tasa=10.0,
            nuevas_cuotas=2,
            nueva_frecuencia="mensual",
            nuevo_vencimiento="2026-04-01",
            descontar_ultima_cuota=True,
            user_id=self.user_id,
            is_admin=False
        )
        self.assertEqual(desembolsado, 0.0)
        p_nuevo = db.obtener_prestamo(nuevo_pid, self.user_id, False)
        self.assertEqual(p_nuevo[7], 550.0)

    def test_buscar_clientes_ajax_admin(self):
        self._crear_prestamo_base()
        res = db.buscar_clientes_ajax("Prueba", 9999, True)
        self.assertGreater(len(res), 0)


if __name__ == "__main__":
    unittest.main()
