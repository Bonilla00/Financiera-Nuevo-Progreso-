"""Genera PDF en memoria para descarga (Flask)."""
import os
from io import BytesIO

from fpdf import FPDF

import db

_LOGO_PATH = os.path.normpath(os.path.join(os.path.dirname(__file__), "static", "logo.png"))


def generar_recibo_pdf(nombre_cliente, prestamo_id, num_cuota_pagada, valor, fecha, user_id: int, is_admin: bool) -> BytesIO:
    info = db.obtener_prestamo(prestamo_id, user_id, is_admin)
    if not info:
        raise ValueError("Préstamo no encontrado")
    total_cuotas = int(info[6])
    cuotas_restantes = max(0, total_cuotas - (num_cuota_pagada - 1))

    pdf = FPDF()
    pdf.add_page()
    pdf.set_auto_page_break(auto=True, margin=18)
    lm = pdf.l_margin
    rm = pdf.r_margin
    epw = pdf.w - lm - rm

    # —— Cabecera: fondo oscuro → logo (si existe) → título → franja cyan ——
    header_top = 10
    logo_w = 36.0
    has_logo = os.path.isfile(_LOGO_PATH)
    header_h = 44.0 if has_logo else 30.0

    pdf.set_fill_color(15, 23, 42)
    pdf.rect(lm, header_top, epw, header_h, "F")

    if has_logo:
        x_logo = lm + (epw - logo_w) / 2
        pdf.image(_LOGO_PATH, x=x_logo, y=header_top + 2, w=logo_w, keep_aspect_ratio=True)
        pdf.set_xy(lm, header_top + 22)
        pdf.set_font("Helvetica", "B", 13)
        pdf.set_text_color(255, 255, 255)
        pdf.cell(epw, 7, "RECIBO DE PAGO", align="C", new_x="LMARGIN", new_y="NEXT")
    else:
        pdf.set_xy(lm, header_top + 8)
        pdf.set_font("Helvetica", "B", 18)
        pdf.set_text_color(255, 255, 255)
        pdf.cell(epw, 10, "RECIBO DE PAGO", align="C", new_x="LMARGIN", new_y="NEXT")
        pdf.set_x(lm)
        pdf.set_font("Helvetica", "", 10)
        pdf.set_text_color(186, 210, 253)
        pdf.cell(epw, 6, "Financiera Nuevo Progreso", align="C", new_x="LMARGIN", new_y="NEXT")

    pdf.set_fill_color(34, 211, 238)
    pdf.rect(lm, header_top + header_h, epw, 1.3, "F")

    # —— Cuerpo ——
    pdf.set_y(header_top + header_h + 10)
    pdf.set_text_color(30, 41, 59)
    pdf.set_draw_color(203, 213, 225)
    pdf.set_line_width(0.2)

    data = [
        ("Cliente", str(nombre_cliente)),
        ("Préstamo", f"#{prestamo_id}"),
        ("Cuota pagada", str(num_cuota_pagada)),
        ("Cuotas restantes", str(cuotas_restantes)),
        ("Valor pagado", f"${float(valor):,.2f}"),
        ("Fecha", str(fecha)),
    ]

    row_h = 9.5
    label_w = 52
    x0 = lm

    for i, (label, val) in enumerate(data):
        if i % 2 == 0:
            pdf.set_fill_color(248, 250, 252)
        else:
            pdf.set_fill_color(255, 255, 255)
        pdf.set_x(x0)
        pdf.set_font("Helvetica", "B", 10)
        pdf.set_text_color(51, 65, 85)
        pdf.cell(label_w, row_h, f"  {label}", border=1, fill=True)
        pdf.set_font("Helvetica", "", 10)
        pdf.set_text_color(15, 23, 42)
        pdf.cell(epw - label_w, row_h, f"  {val}", border=1, new_x="LMARGIN", new_y="NEXT", fill=True)

    pdf.ln(2)
    pdf.set_fill_color(34, 211, 238)
    pdf.rect(lm, pdf.get_y(), epw, 1.0, "F")
    pdf.ln(12)
    pdf.set_font("Helvetica", "I", 10)
    pdf.set_text_color(100, 116, 139)
    pdf.cell(epw, 7, "Gracias por su pago puntual.", align="C", new_x="LMARGIN", new_y="NEXT")

    raw = pdf.output(dest="S")
    if isinstance(raw, str):
        raw = raw.encode("latin-1")
    buf = BytesIO(raw)
    buf.seek(0)
    return buf
