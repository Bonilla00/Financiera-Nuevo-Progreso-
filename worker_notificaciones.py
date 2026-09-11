import os
import logging
from datetime import datetime, date
import pytz
import db
from push_service import send_push_notification, fmt_money

# Configuración de logs con zona horaria America/Bogota
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
)
logger = logging.getLogger("worker_vencimientos")

def procesar_vencimientos():
    """Revisa préstamos activos y envía notificaciones push de vencimientos/mora."""
    tz_colombia = pytz.timezone("America/Bogota")
    hoy = datetime.now(tz_colombia).date()
    hoy_str = hoy.isoformat()

    logger.info(f"Iniciando revisión de vencimientos para la fecha: {hoy_str} (Bogotá)...")

    # 1. Obtener configuraciones globales
    recordatorio_on = db.obtener_configuracion("push_recordatorio_cuotas")
    cuotas_vencidas_on = db.obtener_configuracion("push_cuotas_vencidas")
    mora_on = db.obtener_configuracion("push_mora")
    dias_antes = db.obtener_configuracion("push_dias_antes") or 0

    if not any([recordatorio_on, cuotas_vencidas_on, mora_on]):
        logger.info("Todas las notificaciones automáticas están desactivadas en la configuración.")
        return

    # 2. Listar préstamos activos (is_admin=True para consultar toda la cartera)
    try:
        prestamos = db.listar_prestamos(where="p.estado = 'ACTIVO'", is_admin=True)
    except Exception as e:
        logger.error(f"Error al consultar préstamos en la base de datos: {e}")
        return

    logger.info(f"Total de préstamos activos a evaluar: {len(prestamos)}")

    for p in prestamos:
        pid = p['id']
        owner_id = p.get('owner_user_id') or 1
        nombre_cliente = p['nombre']
        prox_pago_str = p['proximo_pago']

        if not prox_pago_str:
            continue

        try:
            prox_pago = datetime.strptime(prox_pago_str[:10], "%Y-%m-%d").date()
        except Exception:
            continue

        dias_atraso = (hoy - prox_pago).days
        users_to_notify = list(set([owner_id, 1])) # Asesor dueño + Administrador

        # CASO A: Vence hoy o en X días (Recordatorio)
        if recordatorio_on and (dias_atraso == 0 or dias_atraso == -dias_antes):
            clave = f"{pid}_recordatorio_{hoy_str}"
            if dias_atraso == 0:
                titulo = "📅 Cuota para HOY"
                mensaje = f"Cliente {nombre_cliente}: cuota de {fmt_money(p['valor_cuota'])} vence hoy."
            else:
                titulo = f"📅 Cuota en {dias_antes} días"
                mensaje = f"Cliente {nombre_cliente}: cuota de {fmt_money(p['valor_cuota'])} vence el {prox_pago_str}."

            url = f"/clientes/{p['cid']}/perfil"
            for uid in users_to_notify:
                try:
                    if db.registrar_notificacion(uid, "recordatorio", titulo, mensaje, url, f"{clave}_{uid}"):
                        send_push_notification(uid, {"title": titulo, "body": mensaje, "url": url})
                        logger.info(f"Recordatorio enviado a usuario #{uid} para préstamo #{pid}")
                except Exception as ex:
                    logger.error(f"Error enviando recordatorio a usuario #{uid}: {ex}")

        # CASO B: Cuota vencida / Mora
        elif dias_atraso > 0:
            mora_calculada = db.calcular_interes_mora(
                p['valor_cuota'],
                prox_pago_str,
                hoy_str,
                p.get('mora_activa', False),
                p.get('tasa_mora_diaria', 0),
                p.get('valor_mora_fijo_diario', 0),
                p.get('dias_gracia_mora', 0)
            )

            # Aviso de primer día de atraso
            if dias_atraso == 1 and cuotas_vencidas_on:
                clave = f"{pid}_vencida_{hoy_str}"
                titulo = "⚠️ Cuota VENCIDA"
                total_v = p['valor_cuota'] + mora_calculada
                mensaje = f"{nombre_cliente} no pagó ayer. Pendiente: {fmt_money(total_v)}"

                url = f"/clientes/{p['cid']}/perfil"
                for uid in users_to_notify:
                    try:
                        if db.registrar_notificacion(uid, "vencida", titulo, mensaje, url, f"{clave}_{uid}is"):
                            send_push_notification(uid, {"title": titulo, "body": mensaje, "url": url})
                            logger.info(f"Aviso de vencimiento enviado a usuario #{uid} para préstamo #{pid}")
                    except Exception as ex:
                        logger.error(f"Error enviando vencimiento a usuario #{uid}: {ex}")

            # Aviso periódico de mora (cada 3 días de atraso)
            elif mora_calculada > 0 and mora_on:
                if dias_atraso % 3 == 0:
                    clave = f"{pid}_mora_{hoy_str}"
                    titulo = "💰 Mora Acumulada"
                    mensaje = f"{nombre_cliente} lleva {dias_atraso} días de atraso. Mora: {fmt_money(mora_calculada)}"

                    url = f"/clientes/{p['cid']}/perfil"
                    for uid in users_to_notify:
                        try:
                            if db.registrar_notificacion(uid, "mora", titulo, mensaje, url, f"{clave}_{uid}"):
                                send_push_notification(uid, {"title": titulo, "body": mensaje, "url": url})
                                logger.info(f"Aviso de mora enviado a usuario #{uid} para préstamo #{pid}")
                        except Exception as ex:
                            logger.error(f"Error enviando mora a usuario #{uid}: {ex}")

    logger.info("Proceso de revisión de vencimientos finalizado con éxito.")

if __name__ == "__main__":
    import sys
    # Permite ejecución manual: python worker_notificaciones.py
    logger.info("Ejecutando worker de forma manual...")
    procesar_vencimientos()
