import os
import tempfile
import unittest

import db


class TestDbCoreFlows(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self._original_db_path = db.db_path
        self.test_db_file = os.path.join(self.temp_dir.name, "test_financiera.db")
        db.db_path = lambda: self.test_db_file
        db.init_db()

    def tearDown(self):
        db.db_path = self._original_db_path
        self.temp_dir.cleanup()

    def _crear_prestamo_base(self):
        cid = db.get_or_create_cliente(
            "Cliente Prueba",
            "CC-123",
            "3000000000",
            "Centro",
            "Calle 1",
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
            )

    def test_registrar_y_eliminar_pago_actualiza_estado(self):
        _, pid = self._crear_prestamo_base()

        db.registrar_pago(pid, "2026-01-10", 550.0)
        prestamo = db.obtener_prestamo(pid)
        self.assertEqual(prestamo[13], "ACTIVO")
        self.assertEqual(prestamo[14], 1)

        pagos = db.listar_pagos(pid)
        self.assertEqual(len(pagos), 1)
        pago_id = pagos[0][0]

        ok = db.eliminar_pago_y_actualizar(pid, pago_id)
        self.assertTrue(ok)

        prestamo = db.obtener_prestamo(pid)
        self.assertEqual(prestamo[13], "ACTIVO")
        self.assertEqual(prestamo[14], 0)

    def test_proxima_fecha_pago_por_frecuencia(self):
        self.assertEqual(db.proxima_fecha_pago("2026-01-01", "diaria", 0, 3), "2026-01-02")
        self.assertEqual(db.proxima_fecha_pago("2026-01-01", "semanal", 0, 3), "2026-01-08")
        self.assertEqual(db.proxima_fecha_pago("2026-01-01", "quincenal", 0, 3), "2026-01-16")
        self.assertEqual(db.proxima_fecha_pago("2026-01-01", "mensual", 0, 3), "2026-01-31")

    def test_prestamo_queda_pagado_al_completar_cuotas(self):
        _, pid = self._crear_prestamo_base()

        db.registrar_pago(pid, "2026-01-10", 550.0)
        db.registrar_pago(pid, "2026-02-10", 550.0)

        prestamo = db.obtener_prestamo(pid)
        self.assertEqual(prestamo[13], "PAGADO")
        self.assertEqual(prestamo[14], 2)


if __name__ == "__main__":
    unittest.main()
