# agent/calendar.py — Integración Google Calendar con Service Account

"""
Maneja autenticación y operaciones con Google Calendar API usando Service Account.
"""

import os
import json
import logging
from datetime import datetime, timedelta
from typing import Optional, List, Dict
from pythonjsonlogger import jsonlogger
from google.oauth2 import service_account
from google.auth.transport.requests import Request
from googleapiclient.discovery import build

logger = logging.getLogger("agentkit.calendar")
handler = logging.StreamHandler()
formatter = jsonlogger.JsonFormatter()
handler.setFormatter(formatter)
logger.addHandler(handler)
logger.setLevel(logging.INFO)

SCOPES = ["https://www.googleapis.com/auth/calendar"]


def get_calendar_service():
    """
    Autentica con Google Calendar usando Service Account y devuelve el cliente.
    """
    try:
        credentials_file = os.getenv("GOOGLE_CREDENTIALS_FILE", "credentials/google-service-account.json")

        if not os.path.exists(credentials_file):
            logger.error("GOOGLE_CREDENTIALS_NOT_FOUND", extra={"file": credentials_file})
            return None

        credentials = service_account.Credentials.from_service_account_file(
            credentials_file, scopes=SCOPES
        )

        service = build("calendar", "v3", credentials=credentials)
        logger.info("GOOGLE_CALENDAR_AUTHENTICATED", extra={"email": credentials.service_account_email})
        return service

    except Exception as e:
        logger.error("GOOGLE_CALENDAR_AUTH_ERROR", extra={"error": str(e)})
        return None


async def get_available_slots(
    calendar_service,
    n: int = 3,
    start_date: Optional[datetime] = None
) -> List[Dict]:
    """
    Busca los próximos N slots disponibles en horario laboral.

    Retorna lista de dicts:
    [
        {
            "id": "slot1",
            "start": "2025-03-27T10:00:00-05:00",
            "end": "2025-03-27T11:00:00-05:00",
            "display": "Jueves 27 Mar · 10:00am - 11:00am"
        }
    ]
    """
    try:
        if not calendar_service:
            logger.warning("CALENDAR_SERVICE_UNAVAILABLE")
            return []

        calendar_id = os.getenv("GOOGLE_CALENDAR_ID")
        meeting_duration = int(os.getenv("MEETING_DURATION_MINUTES", 60))
        meeting_buffer = int(os.getenv("MEETING_BUFFER_MINUTES", 15))

        # Horario laboral: 9:00 - 18:00 (UTC-5)
        # Lunes a sábado
        if start_date is None:
            start_date = datetime.now()

        slots = []
        search_date = start_date.date()

        while len(slots) < n:
            # Saltar domingos
            if search_date.weekday() == 6:  # Sunday
                search_date += timedelta(days=1)
                continue

            # Buscar eventos en ese día
            search_start = datetime.combine(search_date, datetime.min.time()).replace(
                hour=9, minute=0, second=0
            )
            search_end = datetime.combine(search_date, datetime.max.time()).replace(
                hour=18, minute=0, second=0
            )

            # Agregar timezone offset UTC-5
            search_start_iso = search_start.isoformat() + "-05:00"
            search_end_iso = search_end.isoformat() + "-05:00"

            try:
                events_result = calendar_service.events().list(
                    calendarId=calendar_id,
                    timeMin=search_start_iso,
                    timeMax=search_end_iso,
                    singleEvents=True,
                    orderBy="startTime"
                ).execute()

                events = events_result.get("items", [])

                # Buscar huecos libres
                current_time = search_start
                occupied_times = []

                for event in events:
                    event_start = datetime.fromisoformat(event["start"].get("dateTime", event["start"].get("date")))
                    event_end = datetime.fromisoformat(event["end"].get("dateTime", event["end"].get("date")))
                    occupied_times.append((event_start, event_end))

                # Generar slots de 1 hora entre ocupados
                while current_time.time().hour < 18:
                    slot_end = current_time + timedelta(minutes=meeting_duration)

                    # Validar que no se solape con eventos
                    is_free = True
                    for occ_start, occ_end in occupied_times:
                        if (current_time < occ_end and slot_end > occ_start):
                            is_free = False
                            # Saltar al final del evento
                            current_time = occ_end
                            break

                    if is_free and slot_end.time().hour <= 18:
                        slot_id = f"slot_{len(slots)+1}"
                        # Formato para usuario (simple y confiable)
                        hour = current_time.hour
                        minute = current_time.minute
                        ampm = "am" if hour < 12 else "pm"
                        hour_12 = hour if hour <= 12 else hour - 12
                        if hour_12 == 0:
                            hour_12 = 12
                        time_str = f"{hour_12}:{minute:02d}{ampm}"

                        day_map = {0: "Lunes", 1: "Martes", 2: "Miércoles", 3: "Jueves", 4: "Viernes", 5: "Sábado", 6: "Domingo"}
                        day_name = day_map.get(search_date.weekday(), "Día")
                        display = f"{day_name} {search_date.day} Mar · {time_str}"

                        slots.append({
                            "id": slot_id,
                            "start": current_time.isoformat() + "-05:00",
                            "end": slot_end.isoformat() + "-05:00",
                            "display": display
                        })

                        if len(slots) >= n:
                            break

                        current_time = slot_end + timedelta(minutes=meeting_buffer)
                    else:
                        current_time += timedelta(minutes=15)

            except Exception as search_err:
                logger.error("CALENDAR_SEARCH_ERROR", extra={"date": search_date, "error": str(search_err)})

            search_date += timedelta(days=1)

        logger.info("SLOTS_FOUND", extra={"count": len(slots)})
        return slots[:n]

    except Exception as e:
        logger.error("GET_AVAILABLE_SLOTS_ERROR", extra={"error": str(e)})
        return []


async def create_event(
    calendar_service,
    datos: Dict,
    slot: Dict,
    virtual: bool = False
) -> Optional[str]:
    """
    Crea evento en Google Calendar.

    Args:
        calendar_service: Google Calendar API client
        datos: {"nombre": "...", "negocio": "...", "necesidad": "..."}
        slot: {"start": "...", "end": "..."}
        virtual: True si es virtual (crea Meet), False si es presencial

    Returns:
        URL del evento o link de Meet (si virtual), None si error
    """
    try:
        if not calendar_service:
            logger.warning("CALENDAR_SERVICE_UNAVAILABLE")
            return None

        calendar_id = os.getenv("GOOGLE_CALENDAR_ID")

        # Armar descripción
        description = f"""Asesoría LanLabs

Cliente: {datos.get('nombre', 'N/A')}
Negocio: {datos.get('negocio', 'N/A')}
Necesidad: {datos.get('necesidad', 'N/A')}

Modalidad: {'Virtual (Videollamada)' if virtual else 'Presencial (Quito)'}
"""

        event = {
            "summary": f"Asesoría LanLabs - {datos.get('nombre', 'Cliente')}",
            "description": description,
            "start": {"dateTime": slot["start"]},
            "end": {"dateTime": slot["end"]},
            "attendees": []  # No agregar al cliente automáticamente
        }

        # Si es virtual, crear Google Meet
        if virtual:
            event["conferenceData"] = {
                "createRequest": {
                    "requestId": f"meet_{int(__import__('time').time())}",
                    "conferenceSolution": {
                        "key": {"type": "hangoutsMeet"}
                    }
                }
            }
            event["conferenceDataVersion"] = 1

        # Crear evento
        created_event = calendar_service.events().insert(
            calendarId=calendar_id,
            body=event,
            conferenceDataVersion=1 if virtual else 0
        ).execute()

        event_link = created_event.get("htmlLink")
        meet_link = None

        if virtual and "conferenceData" in created_event:
            meet_link = created_event["conferenceData"].get("entryPoints", [{}])[0].get("uri")

        result_link = meet_link if virtual else event_link

        logger.info(
            "EVENT_CREATED",
            extra={
                "event_id": created_event.get("id"),
                "cliente": datos.get('nombre'),
                "virtual": virtual,
                "meet_link": meet_link is not None
            }
        )

        return result_link

    except Exception as e:
        logger.error("CREATE_EVENT_ERROR", extra={"error": str(e), "cliente": datos.get('nombre')})
        return None
