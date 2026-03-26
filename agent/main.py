# agent/main.py — Servidor FastAPI + Webhook de WhatsApp
# Generado por AgentKit

"""
Servidor principal del agente Labi de LanLabs.
Funciona con cualquier proveedor (Whapi, Meta, Twilio) gracias a la capa de providers.
"""

import os
import asyncio
import logging
import time
from contextlib import asynccontextmanager
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import PlainTextResponse
from dotenv import load_dotenv
from pythonjsonlogger import jsonlogger

from agent.brain import generar_respuesta
from agent.memory import inicializar_db, guardar_mensaje, obtener_historial
from agent.providers import obtener_proveedor
from agent.buffer import buffer_manager
from agent.handoff import (
    is_paused,
    pause_conversation,
    resume_conversation,
    is_handoff_request,
    is_operator_command,
    notify_operator,
    get_resume_confirmation_message,
    get_pause_confirmation_message,
)
from agent.scheduler import (
    is_scheduling,
    is_scheduling_request,
    process_scheduling_step,
)
from agent.calendar import get_calendar_service

load_dotenv()

# Configuración de logging estructurado según entorno
ENVIRONMENT = os.getenv("ENVIRONMENT", "development")
log_level = logging.DEBUG if ENVIRONMENT == "development" else logging.INFO

# Setup structured logging
logger = logging.getLogger("agentkit")
handler = logging.StreamHandler()
formatter = jsonlogger.JsonFormatter()
handler.setFormatter(formatter)
logger.addHandler(handler)
logger.setLevel(log_level)

# Proveedor de WhatsApp (se configura en .env con WHATSAPP_PROVIDER)
proveedor = obtener_proveedor()
PORT = int(os.getenv("PORT", 8000))

# Google Calendar service (se inicializa en lifespan)
calendar_service = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Inicializa la base de datos y Redis al arrancar el servidor."""
    global calendar_service

    try:
        await inicializar_db()
        logger.info("DATABASE_INITIALIZED", extra={"status": "ok"})

        # Conectar a Redis para buffering
        await buffer_manager.connect()
        if buffer_manager.connected:
            logger.info("REDIS_CONNECTED", extra={"status": "ok"})
        else:
            logger.warning("REDIS_UNAVAILABLE", extra={"degradation": "will_respond_per_message"})

        # Inicializar Google Calendar service
        calendar_service = get_calendar_service()
        if calendar_service:
            logger.info("GOOGLE_CALENDAR_SERVICE_INITIALIZED", extra={"status": "ok"})
        else:
            logger.warning("GOOGLE_CALENDAR_SERVICE_UNAVAILABLE", extra={"degradation": "scheduling_disabled"})

        logger.info(
            "SERVER_STARTED",
            extra={
                "port": PORT,
                "provider": proveedor.__class__.__name__,
                "environment": ENVIRONMENT,
            },
        )
        yield
    finally:
        await buffer_manager.disconnect()


app = FastAPI(
    title="Labi — Agente de WhatsApp de LanLabs",
    version="1.0.0",
    lifespan=lifespan
)


@app.get("/")
async def health_check():
    """Endpoint de salud para Railway/monitoreo."""
    return {"status": "ok", "agente": "Labi", "negocio": "LanLabs"}


@app.get("/webhook")
async def webhook_verificacion(request: Request):
    """Verificación GET del webhook (requerido por Meta Cloud API, no-op para otros)."""
    resultado = await proveedor.validar_webhook(request)
    if resultado is not None:
        return PlainTextResponse(str(resultado))
    return {"status": "ok"}


@app.post("/webhook")
async def webhook_handler(request: Request):
    """
    Recibe mensajes de WhatsApp via el proveedor configurado.
    Usa buffering para agrupar múltiples mensajes → respuesta unificada.
    Maneja pausa/reanudación para transferencia a operador humano.
    """
    try:
        # Parsear webhook — el proveedor normaliza el formato
        mensajes = await proveedor.parsear_webhook(request)
        user_ids_seen = set()

        # 1. PROCESAR TODOS LOS MENSAJES (agregar al buffer, detectar handoff/commands)
        for msg in mensajes:
            # Ignorar mensajes vacíos
            if not msg.texto:
                continue

            logger.info(
                "WEBHOOK_MESSAGE_RECEIVED",
                extra={"user_id": msg.telefono, "message_id": msg.mensaje_id, "es_propio": msg.es_propio},
            )

            # ===== COMANDOS DEL OPERADOR =====
            if msg.es_propio:
                es_cmd, accion, target_user = is_operator_command(msg.texto, msg.telefono)
                if es_cmd:
                    if accion == "reanudar":
                        await resume_conversation(buffer_manager.redis_client, target_user)
                        await proveedor.enviar_mensaje(
                            msg.telefono, get_resume_confirmation_message(target_user)
                        )
                    elif accion == "pausar":
                        await pause_conversation(buffer_manager.redis_client, target_user)
                        await proveedor.enviar_mensaje(
                            msg.telefono, get_pause_confirmation_message(target_user)
                        )
                    logger.info("OPERATOR_COMMAND_EXECUTED", extra={"command": accion, "target": target_user})
                # Ignorar otros mensajes propios (respuestas del operador al cliente)
                continue

            # ===== IGNORAR SI CONVERSACIÓN ESTÁ PAUSADA =====
            paused = await is_paused(buffer_manager.redis_client, msg.telefono)
            if paused:
                logger.info(
                    "MESSAGE_IGNORED_PAUSED",
                    extra={"user_id": msg.telefono, "reason": "conversation_paused"},
                )
                # No respondemos, el operador está atendiendo
                continue

            # ===== DETECTAR SOLICITUD DE ATENCIÓN HUMANA =====
            if is_handoff_request(msg.texto):
                await pause_conversation(buffer_manager.redis_client, msg.telefono)
                await proveedor.enviar_mensaje(
                    msg.telefono, "Un momento, te atiendo enseguida 🙏"
                )
                await notify_operator(proveedor, msg.telefono, msg.texto)
                logger.info(
                    "HANDOFF_INITIATED",
                    extra={"user_id": msg.telefono, "message": msg.texto[:50]},
                )
                continue

            # ===== FLUJO DE AGENDAMIENTO DE CITAS =====
            scheduling = await is_scheduling(buffer_manager.redis_client, msg.telefono)
            if scheduling or is_scheduling_request(msg.texto):
                scheduling_response = await process_scheduling_step(
                    buffer_manager.redis_client,
                    proveedor,
                    calendar_service,
                    msg.telefono,
                    msg.texto
                )
                if scheduling_response:
                    await guardar_mensaje(msg.telefono, "user", msg.texto)
                    await guardar_mensaje(msg.telefono, "assistant", scheduling_response)
                    await proveedor.enviar_mensaje(msg.telefono, scheduling_response)
                    logger.info(
                        "SCHEDULING_RESPONSE_SENT",
                        extra={"user_id": msg.telefono, "in_process": scheduling},
                    )
                    continue

            user_ids_seen.add(msg.telefono)

            # BUFFERING: Agrupa múltiples mensajes
            await buffer_manager.handle_message(
                user_id=msg.telefono,
                text=msg.texto,
                message_id=msg.mensaje_id,
            )

        # 2. ESPERAR A QUE LOS BUFFERS SE COMPLETEN
        # Espera a que se cumpla el timeout de pausa de mensajes
        await asyncio.sleep((int(os.getenv("BUFFER_TIMEOUT_MS", 2500)) / 1000) + 0.5)

        # 3. VERIFICAR Y PROCESAR BUFFERS COMPLETADOS
        for user_id in user_ids_seen:
            completed_buffer = await buffer_manager.check_and_get_completed_buffer(user_id)

            if completed_buffer is None:
                # Buffer aún no completado (puede haber más mensajes)
                continue

            # ===== BUFFER COMPLETADO: PROCESAR =====
            process_start = time.time()

            try:
                # Combina todos los mensajes buffered en un contexto unificado
                combined_context = buffer_manager.combine_messages(completed_buffer["messages"])

                logger.info(
                    "BUFFER_READY_TO_PROCESS",
                    extra={
                        "user_id": user_id,
                        "msg_count": len(completed_buffer["messages"]),
                        "combined_length": len(combined_context),
                    },
                )

                # Obtener historial ANTES de guardar el mensaje actual
                historial = await obtener_historial(user_id)

                # Generar respuesta con Claude (usando contexto combinado)
                respuesta = await generar_respuesta(combined_context, historial)

                # Guardar TODOS los mensajes del usuario + respuesta única
                for buffered_msg in completed_buffer["messages"]:
                    await guardar_mensaje(user_id, "user", buffered_msg["text"])

                await guardar_mensaje(user_id, "assistant", respuesta)

                # Enviar respuesta UNA SOLA VEZ (no una por cada mensaje)
                send_success = await proveedor.enviar_mensaje(user_id, respuesta)

                duration_ms = int((time.time() - process_start) * 1000)
                logger.info(
                    "RESPONSE_SENT",
                    extra={
                        "user_id": user_id,
                        "duration_ms": duration_ms,
                        "response_length": len(respuesta),
                        "success": send_success,
                    },
                )

            except Exception as process_err:
                logger.error(
                    "BUFFER_PROCESS_FAILED",
                    extra={
                        "user_id": user_id,
                        "msg_count": len(completed_buffer["messages"]),
                        "error": str(process_err),
                    },
                )
                # Intenta enviar un mensaje de error al usuario
                try:
                    await proveedor.enviar_mensaje(
                        user_id,
                        "Disculpa, ocurrió un error procesando tu solicitud. Por favor intenta de nuevo.",
                    )
                except:
                    pass

        return {"status": "ok"}

    except Exception as e:
        logger.error("WEBHOOK_HANDLER_ERROR", extra={"error": str(e)})
        raise HTTPException(status_code=500, detail=str(e))
