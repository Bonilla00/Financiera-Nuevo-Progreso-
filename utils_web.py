import datetime


def add_days(fecha_str, dias):
    try:
        f = datetime.datetime.strptime(fecha_str, "%Y-%m-%d").date()
        return (f + datetime.timedelta(days=int(dias))).strftime("%Y-%m-%d")
    except (TypeError, ValueError):
        return fecha_str
