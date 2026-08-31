"""
telegram_bot.py - control y consulta del bot de Alpaca desde Telegram (movil).

Proceso APARTE del propio bot de trading (bot_alpaca.py) y de su servicio
systemd (bot-alpaca) -pensado para correr como su propio servicio systemd
(telegram-bot), en paralelo, sin que un fallo aqui pueda afectar al trading-.
Usa "long polling" contra la API HTTP de Telegram (metodo getUpdates): no
hace falta librería extra (`python-telegram-bot`, etc.), solo `requests`,
que ya es una dependencia transitiva de alpaca-py.

Comandos soportados (solo responde al chat autorizado, TELEGRAM_CHAT_ID):
    /estado      - si el servicio bot-alpaca esta activo o parado
    /arrancar    - arranca el servicio (systemctl start bot-alpaca)
    /parar       - para el servicio (systemctl stop bot-alpaca)
    /cartera     - posiciones abiertas (igual que cartera_alpaca.py)
    /hoy         - operaciones cerradas hoy
    /ayer        - operaciones cerradas ayer
    /semana      - operaciones cerradas esta semana laboral (lunes a hoy)
    /log         - ultimas lineas del log del bot (journalctl)
    /ayuda       - lista de comandos

Variables de entorno necesarias (ver ALPACA_NOTES.md):
    TELEGRAM_BOT_TOKEN   - token del bot, dado por @BotFather
    TELEGRAM_CHAT_ID     - id numerico de tu chat/usuario de Telegram
                           (unico chat al que responde; cualquier otro se ignora)

Ademas necesita que el usuario que ejecuta este script tenga permiso para
arrancar/parar el servicio bot-alpaca SIN contraseña (ver ALPACA_NOTES.md,
seccion "sudoers" para la regla exacta) -si no, /arrancar y /parar fallaran
con un error de permisos, pero el resto de comandos (solo lectura) funcionan
igual.
"""
import os
import subprocess
import sys
import time

import requests

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
    raise SystemExit(
        "Faltan las variables de entorno TELEGRAM_BOT_TOKEN y/o TELEGRAM_CHAT_ID. "
        "Ver ALPACA_NOTES.md para como crear el bot y conseguir estos valores."
    )

# Import diferido a bot_alpaca / cartera_alpaca: requieren ALPACA_API_KEY y
# ALPACA_SECRET_KEY ya puestas en el entorno (las mismas que usa el bot).
import bot_alpaca as bot  # noqa: E402
import cartera_alpaca as cartera  # noqa: E402

API_URL = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}"
TIMEOUT_LARGO_POLLING_SEGUNDOS = 30  # long polling: la peticion se queda esperando hasta que hay un mensaje nuevo, o hasta este limite
NOMBRE_SERVICIO_BOT = "bot-alpaca"


def log(mensaje):
    print(f"[telegram_bot] {mensaje}", flush=True)


def enviar_mensaje(texto):
    try:
        requests.post(f"{API_URL}/sendMessage",
                      data={"chat_id": TELEGRAM_CHAT_ID, "text": texto, "parse_mode": "HTML"},
                      timeout=10)
    except Exception as e:
        log(f"No se pudo enviar respuesta a Telegram: {type(e).__name__}: {e}")


def escapar_html(texto):
    return texto.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def ejecutar_systemctl(accion):
    """Ejecuta 'sudo systemctl <accion> bot-alpaca'. Requiere que el sudoers
    del usuario permita este comando exacto sin contraseña (ver ALPACA_NOTES.md)."""
    try:
        resultado = subprocess.run(
            ["sudo", "systemctl", accion, NOMBRE_SERVICIO_BOT],
            capture_output=True, text=True, timeout=20
        )
        if resultado.returncode != 0:
            return f"⚠️ No se pudo {accion} el bot: {resultado.stderr.strip() or resultado.stdout.strip()}"
        return None
    except Exception as e:
        return f"⚠️ Error al intentar {accion} el bot: {type(e).__name__}: {e}"


def consultar_estado_servicio():
    try:
        resultado = subprocess.run(
            ["systemctl", "is-active", NOMBRE_SERVICIO_BOT],
            capture_output=True, text=True, timeout=10
        )
        return resultado.stdout.strip()
    except Exception as e:
        return f"desconocido ({type(e).__name__})"


LIMITE_CARACTERES_LOG_TELEGRAM = 3500  # margen bajo el limite de 4096 de un mensaje de Telegram


def obtener_ultimas_lineas_log(n=15):
    try:
        resultado = subprocess.run(
            ["journalctl", "-u", NOMBRE_SERVICIO_BOT, "-n", str(n), "--no-pager"],
            capture_output=True, text=True, timeout=15
        )
        texto = resultado.stdout.strip() or "(sin lineas de log)"
    except Exception as e:
        return f"No se pudo leer el log: {type(e).__name__}: {e}"
    if len(texto) > LIMITE_CARACTERES_LOG_TELEGRAM:
        texto = "(...)\n" + texto[-LIMITE_CARACTERES_LOG_TELEGRAM:]
    return "📄 <b>ULTIMAS LINEAS DEL LOG</b>\n<pre>" + escapar_html(texto) + "</pre>"


AYUDA = (
    "🤖 Comandos disponibles:\n"
    "/estado - si el bot esta corriendo o parado\n"
    "/arrancar - arranca el bot\n"
    "/parar - para el bot\n"
    "/cartera - posiciones abiertas\n"
    "/hoy - operaciones cerradas hoy\n"
    "/ayer - operaciones cerradas ayer\n"
    "/semana - operaciones cerradas esta semana\n"
    "/log - ultimas lineas del log\n"
    "/ayuda - esta lista"
)


def procesar_comando(texto):
    comando = texto.strip().split()[0].lower().lstrip("/") if texto.strip() else ""

    if comando == "estado":
        estado = consultar_estado_servicio()
        emoji = "🟢" if estado == "active" else "🔴"
        return f"{emoji} Estado del bot: {estado}"

    if comando == "arrancar":
        error = ejecutar_systemctl("start")
        return error or "🟢 Bot arrancado."

    if comando == "parar":
        error = ejecutar_systemctl("stop")
        return error or "🔴 Bot parado."

    if comando == "cartera":
        return cartera.formatear_posiciones_abiertas(html=True)

    if comando in ("hoy", "ayer", "semana"):
        args_falsos = type("Args", (), {
            "ayer": comando == "ayer", "semana": comando == "semana", "desde": None, "hasta": None,
        })()
        desde, hasta = cartera.calcular_rango(args_falsos)
        return cartera.formatear_operaciones_cerradas(desde, hasta, html=True)

    if comando == "log":
        return obtener_ultimas_lineas_log()

    if comando in ("ayuda", "start", "help"):
        return AYUDA

    return "No entiendo ese comando. Escribe /ayuda para ver la lista."


def bucle_principal():
    log("Arrancado. Escuchando mensajes de Telegram (long polling)...")
    offset = None
    while True:
        try:
            params = {"timeout": TIMEOUT_LARGO_POLLING_SEGUNDOS}
            if offset is not None:
                params["offset"] = offset
            respuesta = requests.get(f"{API_URL}/getUpdates", params=params,
                                      timeout=TIMEOUT_LARGO_POLLING_SEGUNDOS + 10)
            respuesta.raise_for_status()
            datos = respuesta.json()

            for actualizacion in datos.get("result", []):
                offset = actualizacion["update_id"] + 1
                mensaje = actualizacion.get("message") or actualizacion.get("edited_message")
                if not mensaje:
                    continue
                chat_id = str(mensaje.get("chat", {}).get("id", ""))
                texto = mensaje.get("text", "")
                if chat_id != str(TELEGRAM_CHAT_ID):
                    log(f"Mensaje ignorado de chat no autorizado ({chat_id}).")
                    continue
                if not texto:
                    continue
                log(f"Comando recibido: {texto}")
                try:
                    respuesta_texto = procesar_comando(texto)
                except Exception as e:
                    respuesta_texto = f"⚠️ Error al procesar el comando: {type(e).__name__}: {e}"
                enviar_mensaje(respuesta_texto)
        except requests.exceptions.RequestException as e:
            log(f"Error de red al consultar Telegram: {type(e).__name__}: {e}. Reintentando en 10s...")
            time.sleep(10)
        except Exception as e:
            log(f"ERROR inesperado: {type(e).__name__}: {e}. Reintentando en 10s...")
            time.sleep(10)


if __name__ == "__main__":
    bucle_principal()
