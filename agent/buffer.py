# agent/buffer.py — Redis-backed message buffering para agrupar múltiples mensajes

import os
import json
import asyncio
import logging
from typing import Optional, Dict, List
from datetime import datetime
import redis.asyncio as redis
from pythonjsonlogger import jsonlogger

# Configuración de logging estructurado
logger = logging.getLogger("agentkit.buffer")
handler = logging.StreamHandler()
formatter = jsonlogger.JsonFormatter()
handler.setFormatter(formatter)
logger.addHandler(handler)
logger.setLevel(logging.INFO)

# Configuración de timeouts
BUFFER_TIMEOUT_MS = int(os.getenv("BUFFER_TIMEOUT_MS", 2500))  # 2.5 segundos de pausa para terminar
MAX_BUFFER_AGE_MS = int(os.getenv("MAX_BUFFER_AGE_MS", 300000))  # 5 minutos
BUFFER_KEY_PREFIX = "whatsapp:buffer:"
BUFFER_PROCESSED_KEY_PREFIX = "whatsapp:processed:"  # Rastrea IDs procesados
MAX_MESSAGES_PER_BUFFER = 15  # Backpressure limit
REDIS_TIMEOUT = 5  # segundos para operaciones Redis

# Storage para timers que están esperando
_timers: Dict[str, asyncio.Task] = {}


class MessageBuffer:
    """Gestor de buffer de mensajes con Redis."""

    def __init__(self):
        self.redis_client: Optional[redis.Redis] = None
        self.connected = False

    async def connect(self):
        """Conecta a Redis."""
        try:
            redis_url = os.getenv("REDIS_URL", "redis://localhost:6379")
            self.redis_client = await redis.from_url(redis_url, decode_responses=True)
            await self.redis_client.ping()
            self.connected = True
            logger.info("Redis conectado exitosamente")
        except Exception as e:
            logger.error("REDIS_CONNECTION_FAILED", extra={"error": str(e)})
            self.connected = False

    async def disconnect(self):
        """Desconecta de Redis."""
        if self.redis_client:
            await self.redis_client.close()

    async def handle_message(
        self, user_id: str, text: str, message_id: str, timestamp: Optional[float] = None
    ) -> Optional[Dict]:
        """
        Procesa un mensaje entrante.
        Devuelve el buffer completado si está listo para procesar, None si aún está buffering.
        """
        if timestamp is None:
            timestamp = datetime.now().timestamp() * 1000

        try:
            # 1. REDIS AVAILABILITY CHECK
            if not self.connected:
                logger.warning("REDIS_UNAVAILABLE", extra={"user_id": user_id})
                return {"messages": [{"text": text, "timestamp": timestamp, "message_id": message_id}]}

            buffer_key = f"{BUFFER_KEY_PREFIX}{user_id}"
            processed_key = f"{BUFFER_PROCESSED_KEY_PREFIX}{user_id}"

            # 2. CHECK IF ALREADY PROCESSED (Global deduplication)
            processed_ids = await self.redis_client.smembers(processed_key)
            if message_id in processed_ids:
                logger.info(
                    "MESSAGE_ALREADY_PROCESSED",
                    extra={"user_id": user_id, "message_id": message_id},
                )
                return None

            # 3. GET EXISTING BUFFER
            buffer_json = await self.redis_client.get(buffer_key)
            buffer = json.loads(buffer_json) if buffer_json else None

            # 4. DEDUPLICATION - Prevent webhook retry duplicates in current buffer
            if buffer and any(m["message_id"] == message_id for m in buffer["messages"]):
                logger.info(
                    "BUFFER_DUPLICATE_SKIPPED",
                    extra={"user_id": user_id, "message_id": message_id},
                )
                return None

            # 5. BACKPRESSURE CHECK
            if buffer and len(buffer["messages"]) >= MAX_MESSAGES_PER_BUFFER:
                logger.error(
                    "BUFFER_OVERFLOW",
                    extra={
                        "user_id": user_id,
                        "msg_count": len(buffer["messages"]),
                        "max": MAX_MESSAGES_PER_BUFFER,
                    },
                )
                return buffer

            # 6. CHECK BUFFER AGE - Prevent merging unrelated topics
            if buffer and (timestamp - buffer["buffer_created_at"]) > MAX_BUFFER_AGE_MS:
                logger.info(
                    "BUFFER_AGE_EXCEEDED",
                    extra={
                        "user_id": user_id,
                        "age_ms": int(timestamp - buffer["buffer_created_at"]),
                    },
                )
                return buffer

            # 7. APPEND OR CREATE
            if buffer:
                buffer["messages"].append(
                    {"text": text, "timestamp": timestamp, "message_id": message_id}
                )
                buffer["last_message_at"] = timestamp
                buffer["status"] = "BUFFERING"

                gap_ms = int(timestamp - buffer["messages"][-2]["timestamp"]) if len(buffer["messages"]) > 1 else 0
                logger.info(
                    "BUFFER_APPEND",
                    extra={
                        "user_id": user_id,
                        "msg_count": len(buffer["messages"]),
                        "gap_ms": gap_ms,
                    },
                )
            else:
                buffer = {
                    "user_id": user_id,
                    "messages": [{"text": text, "timestamp": timestamp, "message_id": message_id}],
                    "buffer_created_at": timestamp,
                    "last_message_at": timestamp,
                    "status": "BUFFERING",
                }
                logger.info("BUFFER_START", extra={"user_id": user_id})

            # 8. CLEAR EXISTING TIMER
            timer_key = f"{buffer_key}:timer"
            if timer_key in _timers:
                _timers[timer_key].cancel()
                del _timers[timer_key]

            # 9. SAVE BUFFER TO REDIS
            try:
                await self.redis_client.setex(buffer_key, 300, json.dumps(buffer))
            except Exception as redis_err:
                logger.error(
                    "REDIS_WRITE_FAILED",
                    extra={"user_id": user_id, "error": str(redis_err)},
                )
                return buffer

            # 10. SET NEW TIMER - Espera pausa de mensajes antes de responder
            async def timeout_handler():
                await asyncio.sleep(BUFFER_TIMEOUT_MS / 1000)
                # Marca el buffer como completado usando un flag en Redis
                await self.redis_client.setex(f"{buffer_key}:ready", 10, "1")
                logger.info(
                    "BUFFER_TIMEOUT_TRIGGERED",
                    extra={"user_id": user_id, "msg_count": len(buffer["messages"])},
                )

            task = asyncio.create_task(timeout_handler())
            _timers[timer_key] = task

            return None  # Sigue buffering, espera a que se complete el timeout

        except Exception as err:
            logger.error(
                "BUFFER_HANDLER_ERROR",
                extra={
                    "user_id": user_id,
                    "error": str(err),
                },
            )
            return {"messages": [{"text": text, "timestamp": timestamp, "message_id": message_id}]}

    async def check_and_get_completed_buffer(self, user_id: str) -> Optional[Dict]:
        """Verifica si el buffer está listo y lo devuelve si es así."""
        try:
            buffer_key = f"{BUFFER_KEY_PREFIX}{user_id}"
            ready_key = f"{buffer_key}:ready"

            # Verifica si el timeout se completó
            is_ready = await self.redis_client.exists(ready_key)
            if not is_ready:
                return None

            # Obtén el buffer
            buffer_json = await self.redis_client.get(buffer_key)
            if not buffer_json:
                return None

            buffer = json.loads(buffer_json)

            # Marca todos los mensajes como procesados
            processed_key = f"{BUFFER_PROCESSED_KEY_PREFIX}{user_id}"
            for msg in buffer["messages"]:
                await self.redis_client.sadd(processed_key, msg["message_id"])

            # TTL para processed_ids (7 días)
            await self.redis_client.expire(processed_key, 7 * 24 * 3600)

            # Limpia el buffer
            await self.redis_client.delete(buffer_key)
            await self.redis_client.delete(ready_key)

            # Limpia el timer
            timer_key = f"{buffer_key}:timer"
            if timer_key in _timers:
                _timers[timer_key].cancel()
                del _timers[timer_key]

            logger.info(
                "BUFFER_COMPLETE",
                extra={"user_id": user_id, "msg_count": len(buffer["messages"])},
            )

            return buffer
        except Exception as e:
            logger.error("CHECK_BUFFER_ERROR", extra={"user_id": user_id, "error": str(e)})
            return None

    def combine_messages(self, messages: List[Dict]) -> str:
        """Combina múltiples mensajes en un contexto unificado."""
        return "\n\n".join(
            f"[Mensaje {idx + 1}]\n{msg['text']}"
            for idx, msg in enumerate(messages)
        )


# Instancia global
buffer_manager = MessageBuffer()
