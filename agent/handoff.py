# agent/handoff.py — Lógica de pausa y transferencia a operador humano

import os
import logging
import re
from typing import Optional, Tuple
import redis.asyncio as redis
from pythonjsonlogger import jsonlogger

logger = logging.getLogger("agentkit.handoff")
handler = logging.StreamHandler()
formatter = jsonlogger.JsonFormatter()
handler.setFormatter(formatter)
logger.addHandler(handler)
logger.setLevel(logging.INFO)

# Configuración
HANDOFF_KEY_PREFIX = "whatsapp:paused:"
OPERATOR_PHONE = os.getenv("OPERATOR_PHONE", "593996979291")
HANDOFF_TTL_SECONDS = int(os.getenv("HANDOFF_TTL_HOURS", 2)) * 3600

# Keywords que el cliente puede usar para solicitar atención humana
HANDOFF_KEYWORDS = [
    "humano",
    "agente",
    "operador",
    "persona",
    "hablar con alguien",
    "con alguien",
    "atiéndeme",
    "ayuda humana",
    "quiero hablar",
    "necesito ayuda",
    "atención",
]

# Comandos que el operador puede usar (desde su número)
OPERATOR_COMMANDS = {
    "pausar": r"^!pausar\s+(\d+)$",  # !pausar 593111111111
    "reanudar": r"^!reanudar\s+(\d+)$",  # !reanudar 593111111111
}


async def is_paused(redis_client: redis.Redis, user_id: str) -> bool:
    """Verifica si una conversación está pausada."""
    if not redis_client:
        return False
    try:
        key = f"{HANDOFF_KEY_PREFIX}{user_id}"
        paused = await redis_client.exists(key)
        return paused == 1
    except Exception as e:
        logger.error("IS_PAUSED_ERROR", extra={"user_id": user_id, "error": str(e)})
        return False


async def pause_conversation(redis_client: redis.Redis, user_id: str) -> None:
    """Pausa una conversación por HANDOFF_TTL_SECONDS."""
    if not redis_client:
        return
    try:
        key = f"{HANDOFF_KEY_PREFIX}{user_id}"
        await redis_client.setex(key, HANDOFF_TTL_SECONDS, "1")
        logger.info("CONVERSATION_PAUSED", extra={"user_id": user_id, "ttl_hours": HANDOFF_TTL_SECONDS // 3600})
    except Exception as e:
        logger.error("PAUSE_CONVERSATION_ERROR", extra={"user_id": user_id, "error": str(e)})


async def resume_conversation(redis_client: redis.Redis, user_id: str) -> None:
    """Reanuda una conversación pausada."""
    if not redis_client:
        return
    try:
        key = f"{HANDOFF_KEY_PREFIX}{user_id}"
        await redis_client.delete(key)
        logger.info("CONVERSATION_RESUMED", extra={"user_id": user_id})
    except Exception as e:
        logger.error("RESUME_CONVERSATION_ERROR", extra={"user_id": user_id, "error": str(e)})


def is_handoff_request(text: str) -> bool:
    """Detecta si el cliente está pidiendo atención humana."""
    if not text:
        return False

    # Convertir a minúsculas para búsqueda case-insensitive
    text_lower = text.lower().strip()

    # Buscar cualquier keyword
    for keyword in HANDOFF_KEYWORDS:
        if keyword in text_lower:
            logger.info("HANDOFF_REQUEST_DETECTED", extra={"keyword": keyword, "text": text[:50]})
            return True

    return False


def is_operator_command(text: str, from_number: str) -> Tuple[bool, Optional[str], Optional[str]]:
    """
    Detecta si es un comando del operador.

    Returns:
        (es_comando: bool, accion: str, target_user_id: str)
        Ejemplo: (True, "reanudar", "593111111111")
    """
    if not text or from_number != OPERATOR_PHONE:
        return (False, None, None)

    text_stripped = text.strip()

    # Buscar comando !reanudar
    match = re.match(OPERATOR_COMMANDS["reanudar"], text_stripped)
    if match:
        target_user = match.group(1)
        logger.info("OPERATOR_COMMAND_DETECTED", extra={"command": "reanudar", "target": target_user})
        return (True, "reanudar", target_user)

    # Buscar comando !pausar
    match = re.match(OPERATOR_COMMANDS["pausar"], text_stripped)
    if match:
        target_user = match.group(1)
        logger.info("OPERATOR_COMMAND_DETECTED", extra={"command": "pausar", "target": target_user})
        return (True, "pausar", target_user)

    return (False, None, None)


async def notify_operator(proveedor, user_id: str, last_message: str) -> bool:
    """
    Notifica al operador que un cliente necesita atención humana.

    Returns:
        True si la notificación se envió exitosamente, False si falló.
    """
    try:
        notification = (
            f"🔔 *Solicitud de atención humana*\n\n"
            f"Cliente: +{user_id}\n"
            f"Mensaje: \"{last_message[:100]}\"\n\n"
            f"Ya le avisé que le atiendes. Cuando termines escribe:\n"
            f"!reanudar {user_id}"
        )

        success = await proveedor.enviar_mensaje(OPERATOR_PHONE, notification)
        logger.info(
            "OPERATOR_NOTIFIED",
            extra={
                "operator": OPERATOR_PHONE,
                "client": user_id,
                "success": success,
            },
        )
        return success

    except Exception as e:
        logger.error(
            "OPERATOR_NOTIFICATION_FAILED",
            extra={"user_id": user_id, "error": str(e)},
        )
        return False


def get_resume_confirmation_message(user_id: str) -> str:
    """Mensaje de confirmación cuando se reanuda una conversación."""
    return f"✅ Conversación con {user_id} reabierta. El bot volverá a responder."


def get_pause_confirmation_message(user_id: str) -> str:
    """Mensaje de confirmación cuando se pausa una conversación."""
    return f"⏸️ Conversación con {user_id} pausada. Responde directamente desde WhatsApp."
