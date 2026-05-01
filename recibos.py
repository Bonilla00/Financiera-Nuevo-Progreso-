"""Genera PDF o Imagen en memoria para descarga (Flask)."""
import os
import unicodedata
from datetime import datetime
from io import BytesIO

from fpdf import FPDF
from PIL import Image, ImageDraw, ImageFont

import db

_LOGO_PATH = os.path.normpath(os.path.join(os.path.dirname(__file__), "static", "logo.png"))


_PRESTAMO_INDEX = {
    "id": 0,
    "cliente_id": 1,
    "nombre": 2,
    "identificacion": 3,
    "fecha": 4,
    "frecuencia": 5,
    "cuotas": 6,
    "monto": 7,
    "tasa": 8,
    "interes_total": 9,
    "total_pagar": 10,
    "valor_cuota": 11,
    "vencimiento": 12,
    "estado": 13,
    "pagadas": 14,
    "proximo_pago": 15,
    "notas": 16,
    "mora_activa": 17,
    "tasa_mora_diaria": 18,
}


def _prestamo_get(info, key, default=None):
    if info is None:
        return default
    if isinstance(info, dict):
        return info.get(key, default)
    idx = _PRESTAMO_INDEX.get(key)
    if idx is None or idx >= len(info):
        return default
    return info[idx]


def generar_recibo_imagen(
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

    total_cuotas = int(_prestamo_get(info, "cuotas", 0) or 0)
    cuotas_restantes = max(0, total_cuotas - (num_cuota_pagada - 1))

    if valor_cuota_base is None:
        valor_cuota_base = float(_prestamo_get(info, "valor_cuota", 0) or 0)

    # Configuración de imagen
    width, height = 600, 800
    bg_color = (255, 255, 255)
    cyan = (34, 211, 238)
    dark_blue = (15, 23, 42)
    text_color = (51, 65, 85)

    img = Image.new("RGB", (width, height), bg_color)
    draw = ImageDraw.Draw(img)

    # Intentar cargar fuentes (usar default si falla)
    # Nota: En entornos Linux/Server es mejor pasar rutas absolutas a .ttf
    font_path = "arial.ttf"
    try:
        font_title = ImageFont.truetype(font_path, 32)
        font_subtitle = ImageFont.truetype(font_path, 24)
        font_label = ImageFont.truetype(font_path, 20)
        font_bold = ImageFont.truetype(font_path, 20, index=0) # Index for bold if available
        font_footer = ImageFont.truetype(font_path, 16)
    except:
        font_title = ImageFont.load_default()
        font_subtitle = ImageFont.load_default()
        font_label = ImageFont.load_default()
        font_bold = ImageFont.load_default()
        font_footer = ImageFont.load_default()

    # Título
    draw.text((width/2, 50), "FINANCIERA NUEVO PROGRESO", font=font_title, fill=dark_blue, anchor="mm")
    draw.line((50, 80, 550, 80), fill=cyan, width=4)

    # Subtítulo
    draw.text((width/2, 110), "RECIBO DE PAGO", font=font_subtitle, fill=text_color, anchor="mm")

    # Logo
    y_offset = 140
    if os.path.isfile(_LOGO_PATH):
        try:
            logo = Image.open(_LOGO_PATH)
            logo.thumbnail((120, 120))
            img.paste(logo, (int((width - logo.width) / 2), y_offset), logo if logo.mode == 'RGBA' else None)
            y_offset += logo.height + 40
        except:
            y_offset += 40
    else:
        y_offset += 40

    # Datos
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

    label_x = 70
    value_x = 280
    row_h = 45

    for i, (label, val) in enumerate(data):
        if i % 2 == 0:
            draw.rectangle((60, y_offset, 540, y_offset + row_h), fill=(248, 250, 252))

        draw.text((label_x, y_offset + 10), label, font=font_label, fill=text_color)
        draw.text((value_x, y_offset + 10), val, font=font_bold, fill=dark_blue)
        draw.rectangle((60, y_offset, 540, y_offset + row_h), outline=(203, 213, 225), width=1)
        y_offset += row_h

    # Footer
    y_offset += 40
    gen = datetime.now().strftime("%Y-%m-%d %H:%M")
    draw.text((width/2, y_offset), f"Documento generado el {gen}", font=font_footer, fill=(100, 116, 139), anchor="mm")
    draw.text((width/2, y_offset + 30), "Gracias por su pago puntual.", font=font_footer, fill=(100, 116, 139), anchor="mm")

    # Guardar en BytesIO
    buf = BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)
    return buf


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

    total_cuotas = int(_prestamo_get(info, "cuotas", 0) or 0)
    cuotas_restantes = max(0, total_cuotas - (num_cuota_pagada - 1))

    if valor_cuota_base is None:
        valor_cuota_base = float(_prestamo_get(info, "valor_cuota", 0) or 0)

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


def _pdf_safe(s: str) -> str:
    if not s:
        return ""
    return (
        unicodedata.normalize("NFKD", str(s)).encode("ascii", "ignore").decode("ascii") or "?"
    )


def generar_reporte_vision_pdf(
    periodo_etiqueta: str,
    f_ini: str,
    f_fin: str,
    total_prestado: float,
    capital_cobrado: float,
    interes_cobrado: float,
    mora_cobrada: float,
    ganancia_neta: float,
    total_cobrado: float,
    activos: int,
    en_mora: int,
    pagos: list[tuple],
) -> BytesIO:
    """Reporte Visión general / período (fpdf2)."""
    pdf = FPDF()
    pdf.set_auto_page_break(auto=True, margin=14)
    pdf.add_page()
    lm = pdf.l_margin
    epw = pdf.w - pdf.l_margin - pdf.r_margin
    cyan = (34, 211, 238)
    dark = (15, 23, 42)

    pdf.set_font("Helvetica", "B", 15)
    pdf.set_text_color(*dark)
    pdf.cell(epw, 9, "FINANCIERA NUEVO PROGRESO", align="C", new_x="LMARGIN", new_y="NEXT")
    pdf.set_font("Helvetica", "B", 12)
    pdf.set_text_color(51, 65, 85)
    pdf.cell(epw, 8, "Vision general / Reporte", align="C", new_x="LMARGIN", new_y="NEXT")
    pdf.set_font("Helvetica", "", 10)
    pdf.set_text_color(100, 116, 139)
    pdf.cell(epw, 6, f"{_pdf_safe(periodo_etiqueta)}  |  {f_ini}  al  {f_fin}", align="C", new_x="LMARGIN", new_y="NEXT")
    pdf.ln(4)
    pdf.set_draw_color(*cyan)
    pdf.set_line_width(0.5)
    pdf.line(lm, pdf.get_y(), lm + epw, pdf.get_y())
    pdf.ln(6)

    def money(v: float) -> str:
        return f"${float(v):,.0f}"

    resumen = [
        ("Total prestado (periodo)", money(total_prestado)),
        ("Capital cobrado (estim.)", money(capital_cobrado)),
        ("Interes cobrado (estim.)", money(interes_cobrado)),
        ("Mora cobrada", money(mora_cobrada)),
        ("Ganancia neta (int.+mora)", money(ganancia_neta)),
        ("Total cobrado (pagos)", money(total_cobrado)),
        ("Prestamos activos", str(activos)),
        ("Prestamos en mora", str(en_mora)),
    ]
    pdf.set_font("Helvetica", "B", 11)
    pdf.set_text_color(*dark)
    pdf.cell(epw, 7, "Resumen", new_x="LMARGIN", new_y="NEXT")
    pdf.ln(2)
    pdf.set_font("Helvetica", "", 9)
    label_w = 78
    row_h = 7.0
    for label, val in resumen:
        pdf.set_x(lm)
        pdf.set_text_color(71, 85, 105)
        pdf.cell(label_w, row_h, f"  {_pdf_safe(label)}")
        pdf.set_text_color(15, 23, 42)
        pdf.cell(epw - label_w, row_h, f"  {val}", new_x="LMARGIN", new_y="NEXT")
    pdf.ln(6)

    pdf.set_font("Helvetica", "B", 11)
    pdf.set_text_color(*dark)
    pdf.cell(epw, 7, f"Pagos del periodo ({len(pagos)})", new_x="LMARGIN", new_y="NEXT")
    pdf.ln(2)

    col_w = [26, 72, 32, 18]
    pdf.set_font("Helvetica", "B", 8)
    pdf.set_fill_color(241, 245, 249)
    pdf.set_text_color(51, 65, 85)
    headers = ("Fecha", "Cliente", "Valor", "Cuota")
    for i, h in enumerate(headers):
        pdf.cell(col_w[i], 7, f"  {h}", border=1, fill=True)
    pdf.ln()
    pdf.set_font("Helvetica", "", 8)
    for row in pagos:
        fecha, nombre, valor, cuota = row[0], row[1], row[2], row[3]
        if pdf.get_y() > 270:
            pdf.add_page()
            pdf.set_font("Helvetica", "B", 8)
            for i, h in enumerate(headers):
                pdf.cell(col_w[i], 7, f"  {h}", border=1, fill=True)
            pdf.ln()
            pdf.set_font("Helvetica", "", 8)
        pdf.set_x(lm)
        pdf.set_text_color(15, 23, 42)
        pdf.cell(col_w[0], 6, f"  {str(fecha)[:10]}", border=1)
        nm = _pdf_safe(str(nombre))[:36]
        pdf.cell(col_w[1], 6, f"  {nm}", border=1)
        pdf.cell(col_w[2], 6, f"  {money(float(valor))}", border=1)
        pdf.cell(col_w[3], 6, f"  {cuota}", border=1)
        pdf.ln()

    pdf.ln(8)
    pdf.set_font("Helvetica", "I", 8)
    pdf.set_text_color(100, 116, 139)
    gen = datetime.now().strftime("%Y-%m-%d %H:%M")
    pdf.cell(epw, 5, f"Generado {gen}", align="C", new_x="LMARGIN", new_y="NEXT")

    raw = pdf.output(dest="S")
    if isinstance(raw, str):
        raw = raw.encode("latin-1")
    buf = BytesIO(raw)
    buf.seek(0)
    return buf
