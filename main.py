import os
import math
from datetime import datetime, timezone
from fastapi import FastAPI, Request, Response
import httpx
from supabase import create_client, Client

app = FastAPI()

VERIFY_TOKEN = os.getenv("VERIFY_TOKEN", "mi_token_secreto_123")
WHATSAPP_TOKEN = os.getenv("WHATSAPP_TOKEN")
PHONE_NUMBER_ID = os.getenv("PHONE_NUMBER_ID")
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")

supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

esperando_gps = {}

TRANSICIONES_ESTADO = {
    "entrada": "trabajando",
    "inicio_comida": "comiendo",
    "fin_comida": "trabajando",
    "inicio_pausa": "en_pausa",
    "fin_pausa": "trabajando",
    "salida": "fuera"
}

def calcular_distancia_metros(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    R = 6371000
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2)**2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2)**2
    return 2 * R * math.atan2(math.sqrt(a), math.sqrt(1 - a))

@app.get("/")
async def health_check():
    return {"status": "ChecaBot RH activo y operando"}

@app.get("/webhook")
async def verify_webhook(request: Request):
    params = request.query_params
    if params.get("hub.mode") == "subscribe" and params.get("hub.verify_token") == VERIFY_TOKEN:
        return Response(content=params.get("hub.challenge"), media_type="text/plain")
    return Response(content="Error de verificación", status_code=403)

@app.post("/webhook")
async def receive_message(request: Request):
    data = await request.json()
    try:
        # 1. Extraer el mensaje usando el índice [0] obligatorio para las listas de Meta
        if "entry" in data and len(data["entry"]) > 0:
            entry = data["entry"][0]
            if "changes" in entry and len(entry["changes"]) > 0:
                changes_value = entry["changes"][0].get("value", {})
                
                if "messages" in changes_value and len(changes_value["messages"]) > 0:
                    message = changes_value["messages"][0]
                    sender = message.get("from")
                    msg_type = message.get("type")

                    print(f"[MENSAJE RECIBIDO] De: {sender} | Tipo: {msg_type}")

                    # 2. Buscar empleado en Supabase (res.data es una lista, usamos)
                    res = supabase.table("empleados").select("*, empresas(*)").eq("telefono", sender).eq("activo", True).execute()
                    
                    if not res.data or len(res.data) == 0:
                        print(f"[RECHAZADO] Número {sender} no encontrado en tabla empleados.")
                        await enviar_texto(sender, f"⛔ Tu número ({sender}) no está registrado en ninguna empresa activa.")
                        return {"status": "ok"}

                    empleado = res.data[0]
                    empresa = empleado["empresas"]

                    if not empresa["activa"]:
                        await enviar_texto(sender, "⚠️ El servicio de tu empresa está suspendido.")
                        return {"status": "ok"}

                    # 3. Enviar botonera al recibir texto
                    if msg_type == "text":
                        await enviar_menu_inteligente(sender, empleado, empresa)

                    # 4. Procesar botón o lista elegida
                    elif msg_type == "interactive":
                        interactive = message["interactive"]
                        accion_id = interactive.get("button_reply", {}).get("id") or interactive.get("list_reply", {}).get("id")
                        accion_titulo = interactive.get("button_reply", {}).get("title") or interactive.get("list_reply", {}).get("title")

                        if not es_movimiento_valido(empleado["estado_actual"], accion_id):
                            await enviar_texto(sender, "⚠️ Movimiento no válido para tu estado actual. Usa el menú:")
                            await enviar_menu_inteligente(sender, empleado, empresa)
                            return {"status": "ok"}

                        if empresa["pedir_ubicacion"]:
                            esperando_gps[sender] = {"accion_id": accion_id, "titulo": accion_titulo}
                            await enviar_texto(sender, f"Seleccionaste: *{accion_titulo}*.\n\n📍 Envía tu *ubicación actual* (clip 📎 -> Ubicación) para guardar el registro.")
                        else:
                            await registrar_en_db(sender, empleado, empresa, accion_id, accion_titulo)

                    # 5. Procesar ubicación GPS
                    elif msg_type == "location":
                        if sender in esperando_gps:
                            datos = esperando_gps.pop(sender)
                            lat = message["location"]["latitude"]
                            lon = message["location"]["longitude"]

                            if empresa.get("validar_geocerca"):
                                distancia = calcular_distancia_metros(
                                    lat, lon,
                                    empresa["latitud_sucursal"],
                                    empresa["longitud_sucursal"]
                                )
                                radio = empresa.get("radio_metros", 100)
                                if distancia > radio:
                                    await enviar_texto(
                                        sender,
                                        f"❌ *Registro rechazado por ubicación*\n"
                                        f"Estás a *{int(distancia)}m* de la sucursal (máximo: {radio}m)."
                                    )
                                    return {"status": "ok"}

                            await registrar_en_db(sender, empleado, empresa, datos["accion_id"], datos["titulo"], lat, lon)
                        else:
                            await enviar_texto(sender, "⚠️ Primero selecciona una opción del menú:")
                            await enviar_menu_inteligente(sender, empleado, empresa)

    except Exception as e:
        print(f"[ERROR CRÍTICO] {e}")

    return {"status": "ok"}

def es_movimiento_valido(estado_actual: str, accion_id: str) -> bool:
    reglas = {
        "fuera": ["entrada"],
        "trabajando": ["inicio_comida", "inicio_pausa", "salida"],
        "comiendo": ["fin_comida"],
        "en_pausa": ["fin_pausa"]
    }
    return accion_id in reglas.get(estado_actual, [])

async def registrar_en_db(sender: str, empleado: dict, empresa: dict, accion_id: str, titulo: str, lat=None, lon=None):
    nuevo_estado = TRANSICIONES_ESTADO[accion_id]
    ahora_iso = datetime.now(timezone.utc).isoformat()
    ahora_legible = datetime.now().strftime("%d/%m/%Y %H:%M:%S")

    supabase.table("asistencias").insert({
        "empresa_id": empresa["id"],
        "empleado_id": empleado["id"],
        "tipo_movimiento": accion_id,
        "latitud": lat,
        "longitud": lon,
        "fecha_hora": ahora_iso
    }).execute()

    supabase.table("empleados").update({"estado_actual": nuevo_estado}).eq("id", empleado["id"]).execute()

    msg = f"✅ *Registro exitoso*\n👤 {empleado['nombre_completo']}\n🏢 {empresa['nombre']}\n📌 Movimiento: *{titulo}*\n🕒 Hora: {ahora_legible}"
    if lat and lon:
        msg += "\n📍 GPS guardado correctamente"
    await enviar_texto(sender, msg)

async def enviar_menu_inteligente(to: str, empleado: dict, empresa: dict):
    estado = empleado["estado_actual"]
    opciones = []

    if estado == "fuera":
        opciones.append({"id": "entrada", "title": "🟢 Entrada", "description": "Iniciar jornada laboral"})
    elif estado == "comiendo":
        opciones.append({"id": "fin_comida", "title": "💼 Fin de comida", "description": "Regresar de alimentos"})
    elif estado == "en_pausa":
        opciones.append({"id": "fin_pausa", "title": "▶️ Fin de pausa", "description": "Reanudar labores"})
    elif estado == "trabajando":
        if empresa["registrar_comida"]:
            opciones.append({"id": "inicio_comida", "title": "🍽️ Inicio de comida", "description": "Salir a tomar alimentos"})
        if empresa["registrar_pausas"]:
            opciones.append({"id": "inicio_pausa", "title": "🪑 Iniciar pausa", "description": "Descanso breve"})
        opciones.append({"id": "salida", "title": "🔴 Salida", "description": "Finalizar jornada laboral"})

    saludo = f"👋 Hola *{empleado['nombre_completo']}* ({empresa['nombre']}).\nEstado actual: *{estado.upper()}*"

    if len(opciones) <= 3:
        botones = [{"type": "reply", "reply": {"id": op["id"], "title": op["title"]}} for op in opciones]
        payload = {
            "messaging_product": "whatsapp",
            "to": to,
            "type": "interactive",
            "interactive": {
                "type": "button",
                "body": {"text": saludo},
                "action": {"buttons": botones}
            }
        }
    else:
        payload = {
            "messaging_product": "whatsapp",
            "to": to,
            "type": "interactive",
            "interactive": {
                "type": "list",
                "body": {"text": saludo},
                "action": {
                    "button": "Seleccionar acción",
                    "sections": [{"title": "Opciones disponibles", "rows": opciones}]
                }
            }
        }
    await post_whatsapp(payload)

async def enviar_texto(to: str, text: str):
    await post_whatsapp({"messaging_product": "whatsapp", "to": to, "type": "text", "text": {"body": text}})

async def post_whatsapp(payload: dict):
    url = f"https://facebook.com{PHONE_NUMBER_ID}/messages"
    headers = {"Authorization": f"Bearer {WHATSAPP_TOKEN}", "Content-Type": "application/json"}
    async with httpx.AsyncClient() as client:
         resp = await client.post(url, json=payload, headers=headers)
         print(f"[RESPUESTA META API] Status: {resp.status_code} | Body: {resp.text}")
