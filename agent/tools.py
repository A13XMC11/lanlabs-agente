# agent/tools.py — Herramientas del agente Labi (LanLabs)
# Generado por AgentKit

"""
Herramientas específicas para LanLabs.
Incluye funciones para calificar leads, agendar reuniones,
gestionar solicitudes de proyectos y soporte post-venta.
"""

import os
import yaml
import logging
from datetime import datetime

logger = logging.getLogger("agentkit")


def cargar_info_negocio() -> dict:
    """Carga la información del negocio desde business.yaml."""
    try:
        with open("config/business.yaml", "r", encoding="utf-8") as f:
            return yaml.safe_load(f)
    except FileNotFoundError:
        logger.error("config/business.yaml no encontrado")
        return {}


def obtener_horario() -> dict:
    """Retorna el horario de atención de LanLabs."""
    info = cargar_info_negocio()
    horario = info.get("negocio", {}).get("horario", "Todos los días de 9am a 10pm")
    hora_actual = datetime.now().hour
    esta_abierto = 9 <= hora_actual < 22  # 9am a 10pm
    return {
        "horario": horario,
        "esta_abierto": esta_abierto,
    }


def buscar_en_knowledge(consulta: str) -> str:
    """
    Busca información relevante en los archivos de /knowledge.
    Retorna el contenido más relevante encontrado.
    """
    resultados = []
    knowledge_dir = "knowledge"

    if not os.path.exists(knowledge_dir):
        return "No hay archivos de conocimiento disponibles."

    for archivo in os.listdir(knowledge_dir):
        ruta = os.path.join(knowledge_dir, archivo)
        if archivo.startswith(".") or not os.path.isfile(ruta):
            continue
        try:
            with open(ruta, "r", encoding="utf-8") as f:
                contenido = f.read()
                if consulta.lower() in contenido.lower():
                    resultados.append(f"[{archivo}]: {contenido[:500]}")
        except (UnicodeDecodeError, IOError):
            continue

    if resultados:
        return "\n---\n".join(resultados)
    return "No encontré información específica sobre eso en mis archivos."


# ── Calificación de Leads ────────────────────────────────────────────────────

def registrar_lead(telefono: str, nombre: str, interes: str, presupuesto: str = "") -> dict:
    """
    Registra un lead interesado en los servicios de LanLabs.

    Args:
        telefono: Número de teléfono del prospecto
        nombre: Nombre del prospecto
        interes: En qué está interesado (automatización, app, etc.)
        presupuesto: Presupuesto aproximado (opcional)

    Returns:
        Confirmación del registro con ID generado
    """
    lead_id = f"LEAD-{datetime.now().strftime('%Y%m%d%H%M%S')}"
    logger.info(f"Nuevo lead registrado: {lead_id} | {nombre} | {interes} | Tel: {telefono}")
    # TODO: Integrar con CRM o base de datos cuando se requiera
    return {
        "lead_id": lead_id,
        "nombre": nombre,
        "interes": interes,
        "presupuesto": presupuesto,
        "estado": "nuevo",
    }


def calificar_lead(presupuesto: str, urgencia: str, descripcion: str) -> str:
    """
    Califica un lead según su presupuesto, urgencia y descripción del proyecto.

    Returns:
        "alto", "medio" o "bajo" según la calificación
    """
    descripcion_lower = descripcion.lower()
    urgencia_lower = urgencia.lower()

    # Señales de lead de alta calidad
    señales_alto = ["empresa", "negocio", "facturación", "urgente", "esta semana", "hoy"]
    señales_bajo = ["ver", "curiosidad", "investigando", "sin presupuesto", "sin costo"]

    if any(s in descripcion_lower or s in urgencia_lower for s in señales_alto):
        return "alto"
    if any(s in descripcion_lower or s in urgencia_lower for s in señales_bajo):
        return "bajo"
    return "medio"


# ── Agendamiento de Reuniones ────────────────────────────────────────────────

def registrar_solicitud_reunion(
    telefono: str,
    nombre: str,
    horario_preferido: str,
    tema: str
) -> dict:
    """
    Registra una solicitud de reunión con el equipo de LanLabs.

    Returns:
        Confirmación con ID de la solicitud
    """
    solicitud_id = f"REU-{datetime.now().strftime('%Y%m%d%H%M%S')}"
    logger.info(f"Solicitud de reunión: {solicitud_id} | {nombre} | {horario_preferido} | {tema}")
    # TODO: Integrar con Google Calendar o Calendly cuando se requiera
    return {
        "solicitud_id": solicitud_id,
        "nombre": nombre,
        "telefono": telefono,
        "horario_preferido": horario_preferido,
        "tema": tema,
        "estado": "pendiente_confirmacion",
    }


# ── Solicitudes de Proyectos ─────────────────────────────────────────────────

def registrar_solicitud_proyecto(
    telefono: str,
    nombre: str,
    tipo_proyecto: str,
    descripcion: str,
    presupuesto: str = "a definir",
    fecha_inicio: str = "a definir"
) -> dict:
    """
    Registra una solicitud formal de proyecto.

    Args:
        tipo_proyecto: "automatizacion" | "app_web" | "app_movil" | "sistema" | "otro"

    Returns:
        Confirmación con número de solicitud
    """
    solicitud_id = f"PROJ-{datetime.now().strftime('%Y%m%d%H%M%S')}"
    logger.info(f"Nueva solicitud de proyecto: {solicitud_id} | {tipo_proyecto} | {nombre}")
    return {
        "solicitud_id": solicitud_id,
        "nombre": nombre,
        "telefono": telefono,
        "tipo_proyecto": tipo_proyecto,
        "descripcion": descripcion,
        "presupuesto": presupuesto,
        "fecha_inicio": fecha_inicio,
        "estado": "en_revision",
    }


# ── Soporte Post-venta ───────────────────────────────────────────────────────

def crear_ticket_soporte(telefono: str, cliente: str, problema: str) -> dict:
    """
    Crea un ticket de soporte para un cliente existente.

    Returns:
        Número de ticket y tiempo estimado de respuesta
    """
    ticket_id = f"TKT-{datetime.now().strftime('%Y%m%d%H%M%S')}"
    logger.info(f"Ticket de soporte: {ticket_id} | {cliente} | {problema[:50]}...")
    return {
        "ticket_id": ticket_id,
        "cliente": cliente,
        "telefono": telefono,
        "problema": problema,
        "estado": "abierto",
        "tiempo_respuesta": "2-4 horas en horario de atención",
    }
