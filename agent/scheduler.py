# agent/scheduler.py — Flujo multi-paso para agendar citas

"""
Maneja el flujo de agendamiento de reuniones paso a paso.
Almacena estado en Redis con TTL de 30 minutos.
"""

import os
import json
import logging
import re
from typing import Optional, Dict, Tuple
from datetime import datetime
import redis.asyncio as redis
from pythonjsonlogger import jsonlogger

logger = logging.getLogger("agentkit.scheduler")
handler = logging.StreamHandler()
formatter = jsonlogger.JsonFormatter()
handler.setFormatter(formatter)
logger.addHandler(handler)
logger.setLevel(logging.INFO)

# Redis configuration
SCHEDULING_KEY_PREFIX = "whatsapp:scheduling:"
SCHEDULING_TTL = 30 * 60  # 30 minutes

# Keywords that trigger scheduling
SCHEDULING_KEYWORDS = [
    "agendar", "reunión", "cita", "asesoría", "llamada",
    "hablar", "consulta", "quiero una reunión", "quiero agendar"
]

# Steps in the scheduling flow
STEPS = ["nombre", "negocio", "necesidad", "modalidad", "slot"]

STEP_MESSAGES = {
    "nombre": "¿Me puedes decir tu nombre?",
    "negocio": "¿A qué tipo de negocio perteneces o qué rubro trabajas?",
    "necesidad": "¿Qué necesitas mejorar o automatizar en tu negocio?",
    "modalidad": "¿Prefieres una asesoría presencial en Quito o virtual por videollamada?",
    "slot": "Estos son los horarios disponibles:\n{slots}\n¿Cuál prefieres? (ej: 1, 2 ó 3)"
}


async def is_scheduling(redis_client: Optional[redis.Redis], user_id: str) -> bool:
    """Verifica si el usuario está en flujo de agendamiento."""
    if not redis_client:
        return False

    try:
        key = f"{SCHEDULING_KEY_PREFIX}{user_id}"
        state_json = await redis_client.get(key)
        return state_json is not None
    except Exception as e:
        logger.error("IS_SCHEDULING_ERROR", extra={"user_id": user_id, "error": str(e)})
        return False


async def get_scheduling_state(redis_client: Optional[redis.Redis], user_id: str) -> Optional[Dict]:
    """Obtiene el estado actual del flujo de agendamiento."""
    if not redis_client:
        return None

    try:
        key = f"{SCHEDULING_KEY_PREFIX}{user_id}"
        state_json = await redis_client.get(key)
        if state_json:
            return json.loads(state_json)
        return None
    except Exception as e:
        logger.error("GET_STATE_ERROR", extra={"user_id": user_id, "error": str(e)})
        return None


async def save_scheduling_state(redis_client: Optional[redis.Redis], user_id: str, state: Dict) -> bool:
    """Guarda el estado del flujo de agendamiento."""
    if not redis_client:
        return False

    try:
        key = f"{SCHEDULING_KEY_PREFIX}{user_id}"
        state_json = json.dumps(state)
        await redis_client.setex(key, SCHEDULING_TTL, state_json)
        return True
    except Exception as e:
        logger.error("SAVE_STATE_ERROR", extra={"user_id": user_id, "error": str(e)})
        return False


async def clear_scheduling_state(redis_client: Optional[redis.Redis], user_id: str) -> bool:
    """Limpia el estado del flujo de agendamiento."""
    if not redis_client:
        return False

    try:
        key = f"{SCHEDULING_KEY_PREFIX}{user_id}"
        await redis_client.delete(key)
        return True
    except Exception as e:
        logger.error("CLEAR_STATE_ERROR", extra={"user_id": user_id, "error": str(e)})
        return False


def is_scheduling_request(text: str) -> bool:
    """Detecta si el texto solicita agendamiento."""
    text_lower = text.lower()
    return any(keyword in text_lower for keyword in SCHEDULING_KEYWORDS)


def _init_scheduling_state() -> Dict:
    """Inicializa el estado del flujo."""
    return {
        "step": "nombre",
        "nombre": None,
        "negocio": None,
        "necesidad": None,
        "modalidad": None,
        "slots_offered": []
    }


async def _process_nombre_step(redis_client, user_id: str, text: str, calendar_service) -> Tuple[str, bool]:
    """Procesa el paso de nombre."""
    if not text or len(text.strip()) < 2:
        return "Por favor, dame un nombre válido. ¿Cómo te llamas?", False

    state = await get_scheduling_state(redis_client, user_id)
    state["nombre"] = text.strip()
    state["step"] = "negocio"

    await save_scheduling_state(redis_client, user_id, state)

    response = f"Mucho gusto {state['nombre']}! {STEP_MESSAGES['negocio']}"
    return response, False


async def _process_negocio_step(redis_client, user_id: str, text: str, calendar_service) -> Tuple[str, bool]:
    """Procesa el paso de tipo de negocio."""
    if not text or len(text.strip()) < 3:
        return "Necesito saber más sobre tu negocio. ¿Qué rubro es?", False

    state = await get_scheduling_state(redis_client, user_id)
    state["negocio"] = text.strip()
    state["step"] = "necesidad"

    await save_scheduling_state(redis_client, user_id, state)

    response = STEP_MESSAGES['necesidad']
    return response, False


async def _process_necesidad_step(redis_client, user_id: str, text: str, calendar_service) -> Tuple[str, bool]:
    """Procesa el paso de necesidad."""
    if not text or len(text.strip()) < 5:
        return "Cuéntame más sobre qué necesitas automatizar o mejorar.", False

    state = await get_scheduling_state(redis_client, user_id)
    state["necesidad"] = text.strip()
    state["step"] = "modalidad"

    await save_scheduling_state(redis_client, user_id, state)

    response = STEP_MESSAGES['modalidad']
    return response, False


async def _process_modalidad_step(redis_client, user_id: str, text: str, calendar_service) -> Tuple[str, bool]:
    """Procesa el paso de modalidad (presencial/virtual)."""
    text_lower = text.lower()

    is_virtual = any(word in text_lower for word in ["virtual", "videollamada", "video", "online"])
    is_presencial = any(word in text_lower for word in ["presencial", "quito"])

    if not is_virtual and not is_presencial:
        return "¿Prefieres virtual o presencial? Responde: virtual o presencial.", False

    state = await get_scheduling_state(redis_client, user_id)
    state["modalidad"] = "virtual" if is_virtual else "presencial"
    state["step"] = "slot"

    await save_scheduling_state(redis_client, user_id, state)

    # Obtener slots disponibles
    if not calendar_service:
        return "Disculpa, no puedo acceder al calendario en este momento. Por favor intenta más tarde.", False

    from agent.calendar import get_available_slots
    slots = await get_available_slots(calendar_service, n=3)

    if not slots:
        return "No hay horarios disponibles en este momento. Por favor intenta más tarde.", False

    state["slots_offered"] = slots
    await save_scheduling_state(redis_client, user_id, state)

    # Formatear slots para presentar al usuario
    slots_text = "\n".join([f"{i+1}. {slot['display']}" for i, slot in enumerate(slots)])
    response = STEP_MESSAGES['slot'].format(slots=slots_text)

    logger.info("SLOTS_OFFERED", extra={"user_id": user_id, "count": len(slots)})
    return response, False


async def _process_slot_step(redis_client, user_id: str, text: str, calendar_service) -> Tuple[str, bool]:
    """Procesa la selección de slot."""
    try:
        # Extraer número (1, 2, o 3)
        match = re.search(r'\d', text)
        if not match:
            return "Por favor, responde con el número del horario: 1, 2 ó 3.", False

        slot_num = int(match.group())
        state = await get_scheduling_state(redis_client, user_id)

        if slot_num < 1 or slot_num > len(state["slots_offered"]):
            return f"Por favor, elige un número válido (1-{len(state['slots_offered'])}).", False

        selected_slot = state["slots_offered"][slot_num - 1]

        # Crear evento en Google Calendar
        if not calendar_service:
            return "Disculpa, no puedo crear la cita en este momento. Por favor intenta más tarde.", False

        from agent.calendar import create_event
        meet_or_event_link = await create_event(
            calendar_service,
            {
                "nombre": state["nombre"],
                "negocio": state["negocio"],
                "necesidad": state["necesidad"]
            },
            selected_slot,
            virtual=(state["modalidad"] == "virtual")
        )

        if not meet_or_event_link:
            return "Hubo un error creando la cita. Por favor intenta de nuevo.", False

        # Formatear confirmación
        confirmation = f"""✅ ¡Cita agendada con éxito!

📅 {selected_slot['display']}
👤 {state['nombre']} · {state['negocio']}
🎯 {state['necesidad']}
💻 {'Virtual · Link: ' + meet_or_event_link if state['modalidad'] == 'virtual' else 'Presencial · Quito'}

El equipo de LanLabs te contactará antes de la reunión.
¡Hasta entonces! 🚀"""

        # Limpiar estado
        await clear_scheduling_state(redis_client, user_id)

        logger.info(
            "APPOINTMENT_CREATED",
            extra={
                "user_id": user_id,
                "nombre": state["nombre"],
                "modalidad": state["modalidad"]
            }
        )

        return confirmation, True

    except Exception as e:
        logger.error("SLOT_SELECTION_ERROR", extra={"user_id": user_id, "error": str(e)})
        return "Hubo un error procesando tu selección. Por favor intenta de nuevo.", False


async def process_scheduling_step(
    redis_client: Optional[redis.Redis],
    proveedor,
    calendar_service,
    user_id: str,
    text: str
) -> Optional[str]:
    """
    Procesa un mensaje dentro del flujo de agendamiento.

    Retorna:
    - Mensaje de respuesta si el paso fue procesado
    - None si el flujo terminó o hay error
    """
    try:
        # Obtener o inicializar estado
        state = await get_scheduling_state(redis_client, user_id)

        if state is None:
            # Nuevo flujo
            state = _init_scheduling_state()
            await save_scheduling_state(redis_client, user_id, state)

        current_step = state.get("step")

        logger.info(
            "SCHEDULING_STEP",
            extra={
                "user_id": user_id,
                "step": current_step,
                "text_length": len(text)
            }
        )

        # Procesar según el paso actual
        if current_step == "nombre":
            response, completed = await _process_nombre_step(redis_client, user_id, text, calendar_service)
        elif current_step == "negocio":
            response, completed = await _process_negocio_step(redis_client, user_id, text, calendar_service)
        elif current_step == "necesidad":
            response, completed = await _process_necesidad_step(redis_client, user_id, text, calendar_service)
        elif current_step == "modalidad":
            response, completed = await _process_modalidad_step(redis_client, user_id, text, calendar_service)
        elif current_step == "slot":
            response, completed = await _process_slot_step(redis_client, user_id, text, calendar_service)
        else:
            logger.warning("UNKNOWN_STEP", extra={"user_id": user_id, "step": current_step})
            return None

        return response

    except Exception as e:
        logger.error("SCHEDULING_PROCESS_ERROR", extra={"user_id": user_id, "error": str(e)})
        return "Disculpa, ocurrió un error. Por favor intenta de nuevo."
