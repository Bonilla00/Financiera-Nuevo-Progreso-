"""Genera PDF en memoria para descarga (Flask)."""
from io import BytesIO

from fpdf import FPDF

import db


def generar_recibo_pdf(nombre_cliente, prestamo_id, num_cuota_pagada, valor, fecha, user_id: int, is_admin: bool) -> BytesIO:
    info = db.obtener_prestamo(prestamo_id, user_id, is_admin)
    if not info:
        raise ValueError("Préstamo no encontrado")
    total_cuotas = int(info[6])
    cuotas_restantes = max(0, total_cuotas - (num_cuota_pagada - 1))

    pdf = FPDF()
    pdf.add_page()
    pdf.set_auto_page_break(auto=True, margin=15)
    pdf.set_font("Helvetica", "B", 18)
    pdf.cell(0, 12, "RECIBO DE PAGO", new_x="LMARGIN", new_y="NEXT")
    pdf.ln(4)
    pdf.set_font("Helvetica", "", 12)
    data = [
        ("Cliente", nombre_cliente),
        ("Cuotas restantes", str(cuotas_restantes)),
        ("Valor pagado", f"${float(valor):,.2f}"),
        ("Fecha", fecha),
    ]
    for label, val in data:
        pdf.set_font("Helvetica", "B", 12)
        pdf.cell(50, 9, f"{label}:")
        pdf.set_font("Helvetica", "", 12)
        pdf.cell(0, 9, str(val), new_x="LMARGIN", new_y="NEXT")
    pdf.ln(8)
    pdf.set_font("Helvetica", "", 12)
    pdf.cell(0, 10, "Firma del cobrador: ____________________________", new_x="LMARGIN", new_y="NEXT")
    pdf.ln(4)
    pdf.cell(0, 10, "Firma del cliente:  ____________________________", new_x="LMARGIN", new_y="NEXT")
    pdf.ln(8)
    pdf.set_font("Helvetica", "I", 11)
    pdf.cell(0, 8, "Gracias por su pago puntual.", new_x="LMARGIN", new_y="NEXT")

    raw = pdf.output(dest="S")
    if isinstance(raw, str):
        raw = raw.encode("latin-1")
    buf = BytesIO(raw)
    buf.seek(0)
    return buf
