import datetime
import os

def fmt_money(valor):
    try:
        return f"${valor:,.0f}"
    except:
        return "$0"

def today_str():
    return datetime.date.today().strftime("%Y-%m-%d")

def add_days(fecha_str, dias):
    try:
        f = datetime.datetime.strptime(fecha_str, "%Y-%m-%d").date()
        return (f + datetime.timedelta(days=dias)).strftime("%Y-%m-%d")
    except:
        return fecha_str

def db_path():
    return os.path.join(os.path.dirname(__file__), "financiera.db")
