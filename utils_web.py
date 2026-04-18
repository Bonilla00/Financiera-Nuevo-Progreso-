import datetime
from urllib.parse import quote


def add_days(fecha_str, dias):
    try:
        f = datetime.datetime.strptime(fecha_str, "%Y-%m-%d").date()
        return (f + datetime.timedelta(days=int(dias))).strftime("%Y-%m-%d")
    except (TypeError, ValueError):
        return fecha_str


_DIAS = (
    "lunes",
    "martes",
    "miércoles",
    "jueves",
    "viernes",
    "sábado",
    "domingo",
)
_MESES = (
    "enero",
    "febrero",
    "marzo",
    "abril",
    "mayo",
    "junio",
    "julio",
    "agosto",
    "septiembre",
    "octubre",
    "noviembre",
    "diciembre",
)

_FREQ_LABEL = {
    "diaria": "Diaria",
    "semanal": "Semanal",
    "quincenal": "Quincenal",
    "mensual": "Mensual",
}


def frecuencia_label(freq):
    if not freq:
        return "—"
    return _FREQ_LABEL.get(str(freq).lower().strip(), str(freq).capitalize())


def fecha_proximo_pago_texto(iso_date):
    """Texto tipo 'miércoles 9 de abril de 2026', o None si no hay fecha válida."""
    if not iso_date or str(iso_date).strip() == "":
        return None
    try:
        d = datetime.datetime.strptime(str(iso_date).strip()[:10], "%Y-%m-%d").date()
    except ValueError:
        return None
    return f"{_DIAS[d.weekday()]} {d.day} de {_MESES[d.month - 1]} de {d.year}"


def solo_digitos_telefono(tel):
    if not tel:
        return ""
    return "".join(c for c in str(tel) if c.isdigit())


def url_tel(tel):
    d = solo_digitos_telefono(tel)
    if not d:
        return ""
    if len(d) == 10 and not d.startswith("57"):
        d = "57" + d
    return f"tel:+{d}"


def url_whatsapp(tel, nombre="", valor=0, num_cuota=0):
    d = solo_digitos_telefono(tel)
    if not d:
        return ""
    if len(d) <= 10 and not d.startswith("57"):
        d = "57" + d
    if nombre or valor or num_cuota:
        msg = f"Hola {nombre}, confirmamos recibo de pago por ${int(valor):,} correspondiente a la cuota {num_cuota} de su préstamo. Gracias por su pago. — Financiera Nuevo Progreso"
        return f"https://wa.me/{d}?text=" + quote(msg)
    return f"https://wa.me/{d}"


def url_maps(direccion, barrio):
    """Solo dirección y barrio (sin nombre del cliente) para que Maps geocodifique la ubicación."""
    parts = []
    for x in (direccion, barrio):
        if x and str(x).strip():
            parts.append(str(x).strip())
    if not parts:
        return ""
    q = ", ".join(parts)
    return "https://www.google.com/maps/search/?api=1&query=" + quote(q)
