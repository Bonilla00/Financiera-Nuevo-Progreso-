import os
import json
import logging
import psycopg2.extras
import db

logger = logging.getLogger("push_service")

try:
    from pywebpush import webpush, WebPushException
    PYWEBPUSH_AVAILABLE = True
except ImportError:
    PYWEBPUSH_AVAILABLE = False

def fmt_money(valor):
    try:
        return f"${float(valor):,.0f}"
    except (TypeError, ValueError):
        return "$0"

def send_push_notification(user_id, data):
    """Envía una notificación Push independiente sin importar app.py."""
    if not PYWEBPUSH_AVAILABLE:
        logger.error("pywebpush no está instalado. No se puede enviar notificación.")
        return False

    subs = db.get_user_push_subscriptions(user_id)
    if not subs:
        return False

    vapid_private = db.obtener_configuracion("vapid_private_key")
    vapid_public = db.obtener_configuracion("vapid_public_key")

    if not vapid_private or not vapid_public:
        logger.error("Llaves VAPID no configuradas en la base de datos.")
        return False

    payload = json.dumps(data)
    success = False

    for sub in subs:
        try:
            webpush(
                subscription_info={
                    "endpoint": sub["endpoint"],
                    "keys": {"p256dh": sub["p256dh"], "auth": sub["auth"]},
                },
                data=payload,
                vapid_private_key=vapid_private,
                vapid_claims={"sub": "mailto:admin@financieranp.com"},
            )
            success = True
        except WebPushException as ex:
            logger.warning(f"Error enviando push a {sub['endpoint']}: {ex}")
            if ex.response and ex.response.status_code in (404, 410):
                db.delete_push_subscription(sub["endpoint"])
        except Exception as e:
            logger.error(f"Error inesperado en push: {e}")

    return success
