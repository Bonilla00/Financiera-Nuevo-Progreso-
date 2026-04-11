"""Genera PDF en memoria para descarga (Flask)."""
import os
from datetime import datetime
from io import BytesIO

from fpdf import FPDF

import db

_LOGO_PATH = os.path.normpath(os.path.join(os.path.dirname(__file__), "static", "logo.png"))


def generar_recibo_pdf(
    nombre_cliente,
    prestamo_id,
    num_cuota_pagada,
    valor_total,
    fecha,
    user_id: int,
    is_admin: bool,
    valor_cuota_base: float | None = None,
    interes_mora: float = 0.0,
) -> BytesIO:
    info = db.obtener_prestamo(prestamo_id, user_id, is_admin)
    if not info:
        raise ValueError("Préstamo no encontrado")
    total_cuotas = int(info[6])
    cuotas_restantes = max(0, total_cuotas - (num_cuota_pagada - 1))

    if valor_cuota_base is None:
        valor_cuota_base = float(info[11])

    pdf = FPDF()
    pdf.add_page()
    pdf.set_auto_page_break(auto=True, margin=16)
    lm = pdf.l_margin
    epw = pdf.w - pdf.l_margin - pdf.r_margin
    cyan = (34, 211, 238)

    # —— Encabezado marca ——
    pdf.set_font("Helvetica", "B", 16)
    pdf.set_text_color(15, 23, 42)
    pdf.cell(epw, 10, "FINANCIERA NUEVO PROGRESO", align="C", new_x="LMARGIN", new_y="NEXT")
    pdf.ln(1)
    pdf.set_draw_color(*cyan)
    pdf.set_line_width(0.6)
    pdf.line(lm, pdf.get_y(), lm + epw, pdf.get_y())
    pdf.ln(4)

    pdf.set_font("Helvetica", "B", 13)
    pdf.set_text_color(51, 65, 85)
    pdf.cell(epw, 8, "RECIBO DE PAGO", align="C", new_x="LMARGIN", new_y="NEXT")
    pdf.ln(2)

    if os.path.isfile(_LOGO_PATH):
        lw = 28.0
        pdf.image(_LOGO_PATH, x=lm + (epw - lw) / 2, y=pdf.get_y(), w=lw, keep_aspect_ratio=True)
        pdf.ln(16)

    # —— Detalle ——
    pdf.set_y(pdf.get_y() + 2)
    pdf.set_draw_color(203, 213, 225)
    pdf.set_line_width(0.2)

    mora_f = float(interes_mora or 0)
    data = [
        ("Cliente", str(nombre_cliente)),
        ("Préstamo", f"#{prestamo_id}"),
        ("Cuota pagada", str(num_cuota_pagada)),
        ("Cuotas restantes", str(cuotas_restantes)),
        ("Valor cuota", f"${float(valor_cuota_base):,.2f}"),
    ]
    if mora_f > 0.001:
        data.append(("Interés por mora", f"${mora_f:,.2f}"))
    data.append(("Total pagado", f"${float(valor_total):,.2f}"))
    data.append(("Fecha del pago", str(fecha)))

    row_h = 9.0
    label_w = 54
    x0 = lm
    for i, (label, val) in enumerate(data):
        if i % 2 == 0:
            pdf.set_fill_color(248, 250, 252)
        else:
            pdf.set_fill_color(255, 255, 255)
        pdf.set_x(x0)
        pdf.set_font("Helvetica", "B", 10)
        pdf.set_text_color(71, 85, 105)
        pdf.cell(label_w, row_h, f"  {label}", border=1, fill=True)
        pdf.set_font("Helvetica", "", 10)
        pdf.set_text_color(15, 23, 42)
        pdf.cell(epw - label_w, row_h, f"  {val}", border=1, new_x="LMARGIN", new_y="NEXT", fill=True)

    pdf.ln(10)
    pdf.set_font("Helvetica", "I", 9)
    pdf.set_text_color(100, 116, 139)
    gen = datetime.now().strftime("%Y-%m-%d %H:%M")
    pdf.cell(epw, 6, f"Documento generado el {gen}", align="C", new_x="LMARGIN", new_y="NEXT")
    pdf.ln(2)
    pdf.set_font("Helvetica", "I", 10)
    pdf.cell(epw, 6, "Gracias por su pago puntual.", align="C", new_x="LMARGIN", new_y="NEXT")

    raw = pdf.output(dest="S")
    if isinstance(raw, str):
        raw = raw.encode("latin-1")
    buf = BytesIO(raw)
    buf.seek(0)
    return buf
