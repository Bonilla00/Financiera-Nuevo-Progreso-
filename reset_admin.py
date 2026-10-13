"""
Script de Emergencia: Restablece la contraseña del administrador desde la consola.
Uso: python reset_admin.py
"""
import os
import secrets
import getpass
import psycopg2
from werkzeug.security import generate_password_hash

def reset_password():
    db_url = os.environ.get("DATABASE_URL")
    if not db_url:
        print("❌ Error: La variable de entorno DATABASE_URL no está configurada.")
        return

    if db_url.startswith("postgres://"):
        db_url = db_url.replace("postgres://", "postgresql://", 1)

    username = "admin"
    env_pass = os.environ.get("NEW_ADMIN_PASS", "").strip()

    if env_pass:
        new_pass = env_pass
    else:
        try:
            prompt_pass = getpass.getpass(prompt="Ingresa la nueva clave segura para el admin (deja vacío para generar una aleatoria): ").strip()
            if prompt_pass:
                new_pass = prompt_pass
            else:
                new_pass = secrets.token_urlsafe(12)
        except Exception:
            new_pass = secrets.token_urlsafe(12)

    password_hash = generate_password_hash(new_pass)

    try:
        conn = psycopg2.connect(db_url)
        cur = conn.cursor()

        cur.execute("SELECT id FROM usuarios WHERE username = %s", (username,))
        user = cur.fetchone()

        if user:
            cur.execute(
                "UPDATE usuarios SET password_hash = %s, activo = TRUE, debe_cambiar_password = TRUE WHERE id = %s",
                (password_hash, user[0])
            )
            conn.commit()
            print("✅ Éxito: Contraseña cambiada correctamente.")
            print(f"🔑 Usuario: {username}")
            print(f"🔑 Clave temporal asignada: {new_pass}")
            print("⚠️ Debe cambiar esta clave inmediatamente al iniciar sesión.")
        else:
            print(f"❌ Error: El usuario '{username}' no existe.")

        cur.close()
        conn.close()
    except Exception as e:
        print(f"❌ Error de conexión o SQL: {e}")

if __name__ == "__main__":
    reset_password()
