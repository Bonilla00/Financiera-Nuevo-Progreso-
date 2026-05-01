"""
Script de Emergencia: Restablece la contraseña del administrador desde la consola.
Uso: python reset_admin.py
"""
import os
import psycopg2
from werkzeug.security import generate_password_hash

def reset_password():
    # 1. Obtener la URL de la base de datos
    db_url = os.environ.get("DATABASE_URL")
    if not db_url:
        print("❌ Error: La variable de entorno DATABASE_URL no está configurada.")
        return

    # Ajustar para psycopg2 si es necesario
    if db_url.startswith("postgres://"):
        db_url = db_url.replace("postgres://", "postgresql://", 1)

    username = "admin"  # Usuario a restablecer
    new_pass = "admin123" # Contraseña temporal

    print(f"--- Iniciando restablecimiento de contraseña para '{username}' ---")

    # 2. Generar Hash seguro
    password_hash = generate_password_hash(new_pass)

    # 3. Actualizar la Base de Datos
    try:
        conn = psycopg2.connect(db_url)
        cur = conn.cursor()

        # Verificar si el usuario existe
        cur.execute("SELECT id FROM usuarios WHERE username = %s", (username,))
        user = cur.fetchone()

        if user:
            cur.execute(
                "UPDATE usuarios SET password_hash = %s, activo = TRUE WHERE id = %s",
                (password_hash, user[0])
            )
            conn.commit()
            print(f"✅ Éxito: Contraseña cambiada correctamente.")
            print(f"🔑 Usuario: {username}")
            print(f"🔑 Nueva clave: {new_pass}")
            print("⚠️ RECOMENDACIÓN: Cambia esta clave inmediatamente después de iniciar sesión.")
        else:
            print(f"❌ Error: El usuario '{username}' no existe.")

        cur.close()
        conn.close()
    except Exception as e:
        print(f"❌ Error de conexión o SQL: {e}")

if __name__ == "__main__":
    reset_password()
