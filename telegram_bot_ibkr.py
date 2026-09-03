"""
telegram_bot_ibkr.py - control y consulta del bot de IBKR (bot_completo.py)
desde Telegram (movil).

Proceso APARTE del propio bot de trading. Pensado para Windows (el PC donde
corre IB Gateway/TWS y bot_completo.py), a diferencia de telegram_bot.py
(Alpaca en AWS/Linux con systemd) -aqui no hay systemd, asi que arrancar y
parar el bot se hace de otra forma, ver mas abajo-. Usa "long polling"
contra la API HTTP de Telegram (metodo getUpdates): no hace falta libreria
extra, solo `requests`.

Comandos soportados (solo responde al chat autorizado, TELEGRAM_CHAT_ID):
    /estado      - si bot_completo.py esta corriendo (PID vivo) y si esta
                   respondiendo (latido reciente) o parece congelado
    /arrancar    - lanza run.bot.bat en una ventana de CMD nueva (borra antes
                   cualquier peticion de parada pendiente, para que arranque
                   limpio)
    /parar       - pide una parada LIMPIA (no mata el proceso a la fuerza:
                   deja un archivo de señal que bot_completo.py comprueba en
                   cada vuelta de su bucle -maximo cada 30s- y run.bot.bat
                   respeta para no reiniciarlo). Puede tardar hasta medio
                   minuto en hacer efecto; comprueba con /estado.
    /cartera     - posiciones abiertas en todos los mercados (igual que
                   cartera_ibkr.py), consultando IB Gateway con un clientId
                   propio (no interfiere con el bot si esta corriendo)
    /hoy         - actividad y operaciones cerradas de hoy
    /ayer        - lo mismo, del dia anterior
    /semana      - lo mismo, de la semana laboral actual (lunes a hoy)
    /log         - actividad reciente leida de bot_completo.log (compras,
                   ventas, avisos y errores; se filtran los mensajes
                   rutinarios de cada ciclo para que sea una lista corta)
    /ayuda       - lista de comandos

Variables de entorno necesarias (ver NOTES.md):
    TELEGRAM_BOT_TOKEN   - token del bot, dado por @BotFather
    TELEGRAM_CHAT_ID     - id numerico de tu chat/usuario de Telegram

IMPORTANTE sobre /cartera, /hoy, /ayer y /semana: necesitan que IB
Gateway/TWS este abierto y con sesion iniciada (igual que bot_completo.py);
si no, fallan con un error de conexion, que se muestra tal cual en la
respuesta de Telegram.

Requiere que este script corra en el MISMO ordenador que IB Gateway/TWS y
bot_completo.py (no tiene sentido correrlo en otra maquina: /arrancar,
/parar y /log actuan sobre archivos y procesos locales). Se recomienda
dejarlo como una ventana de CMD mas, o como Tarea Programada de Windows
para que arranque solo al iniciar sesion (ver NOTES.md).
"""
import os
import re
import subprocess
import sys
import time

import requests

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
    raise SystemExit(
        "Faltan las variables de entorno TELEGRAM_BOT_TOKEN y/o TELEGRAM_CHAT_ID. "
        "Ver NOTES.md para como crear el bot y conseguir estos valores."
    )

# Import diferido: bot_completo trae sus propias dependencias (ib_async,
# pandas...), ya instaladas en este mismo entorno porque es el bot real.
import bot_completo as bot  # noqa: E402
import cartera_ibkr as cartera  # noqa: E402

API_URL = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}"
TIMEOUT_LARGO_POLLING_SEGUNDOS = 30
RUTA_BASE = os.path.dirname(os.path.abspath(__file__))
RUTA_RUN_BOT_BAT = os.path.join(RUTA_BASE, "run.bot.bat")


def log(mensaje):
    print(f"[telegram_bot_ibkr] {mensaje}", flush=True)


def enviar_mensaje(texto):
    try:
        requests.post(f"{API_URL}/sendMessage",
                      data={"chat_id": TELEGRAM_CHAT_ID, "text": texto, "parse_mode": "HTML"},
                      timeout=10)
    except Exception as e:
        log(f"No se pudo enviar respuesta a Telegram: {type(e).__name__}: {e}")


def escapar_html(texto):
    return texto.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _pid_registrado():
    """Lee el PID guardado por bot_completo.py en bot.pid, o None si el
    archivo no existe o esta vacio/corrupto."""
    try:
        with open(os.path.join(RUTA_BASE, bot.ARCHIVO_PID), "r", encoding="utf-8") as f:
            return int(f.read().strip())
    except (OSError, ValueError):
        return None


def _proceso_vivo(pid):
    """True si existe un proceso de Windows con ese PID (tasklist)."""
    try:
        resultado = subprocess.run(
            ["tasklist", "/FI", f"PID eq {pid}"],
            capture_output=True, text=True, timeout=10
        )
        return str(pid) in resultado.stdout
    except Exception:
        return False


def _segundos_desde_ultimo_latido():
    """Segundos desde la ultima escritura de latido_bot.txt, o None si el
    archivo no existe todavia."""
    ruta = os.path.join(RUTA_BASE, bot.ARCHIVO_LATIDO)
    if not os.path.exists(ruta):
        return None
    return time.time() - os.path.getmtime(ruta)


def consultar_estado():
    pid = _pid_registrado()
    if pid is None or not _proceso_vivo(pid):
        return "🔴 Bot parado (no hay ningun proceso con el PID registrado)."

    segundos_latido = _segundos_desde_ultimo_latido()
    if segundos_latido is None:
        return f"🟡 Bot arrancando (PID {pid} vivo, aun sin ningun latido registrado)."
    if segundos_latido > bot.UMBRAL_CONGELACION_SEGUNDOS:
        minutos = segundos_latido / 60
        return (f"🟠 Bot posiblemente CONGELADO: el proceso (PID {pid}) sigue vivo pero lleva "
                f"{minutos:.0f} min sin dar señales de vida.")
    return f"🟢 Bot corriendo (PID {pid}, ultima actividad hace {segundos_latido:.0f}s)."


def arrancar_bot():
    pid = _pid_registrado()
    if pid is not None and _proceso_vivo(pid):
        return "⚠️ El bot ya esta corriendo (usa /estado para comprobarlo)."

    ruta_flag = os.path.join(RUTA_BASE, bot.ARCHIVO_DETENER)
    if os.path.exists(ruta_flag):
        try:
            os.remove(ruta_flag)
        except OSError:
            pass

    if not os.path.exists(RUTA_RUN_BOT_BAT):
        return f"⚠️ No se encuentra {RUTA_RUN_BOT_BAT}."

    try:
        # 'start ""' abre una ventana de CMD NUEVA y devuelve el control
        # enseguida (no se queda colgado esperando a que el bot termine,
        # que corre indefinidamente).
        subprocess.Popen(f'start "" "{RUTA_RUN_BOT_BAT}"', shell=True, cwd=RUTA_BASE)
        return "🟢 Bot arrancando (run.bot.bat lanzado en una ventana nueva)."
    except Exception as e:
        return f"⚠️ No se pudo arrancar el bot: {type(e).__name__}: {e}"


def parar_bot():
    pid = _pid_registrado()
    if pid is None or not _proceso_vivo(pid):
        return "⚠️ El bot ya esta parado."

    try:
        with open(os.path.join(RUTA_BASE, bot.ARCHIVO_DETENER), "w", encoding="utf-8") as f:
            f.write(str(time.time()))
        return ("🟡 Parada solicitada. El bot terminara el ciclo actual y se detendra "
                "en los proximos 30-60s (comprueba con /estado). No se le fuerza el cierre, "
                "para no interrumpir una orden a medio colocar.")
    except OSError as e:
        return f"⚠️ No se pudo solicitar la parada: {type(e).__name__}: {e}"


def _conectar_cartera():
    ib = bot.IB()
    ib.connect('127.0.0.1', 4002, clientId=cartera.CLIENT_ID_CARTERA, timeout=15)
    return ib


LIMITE_CARACTERES_LOG_TELEGRAM = 3500

FRAGMENTOS_RUIDO_LOG = [
    "Iniciando nuevo ciclo de revision.",
    "por debajo del umbral -> se mantiene",
    "MACD 5min ALCISTA -> se deja correr",
    "datos insuficientes para MACD",
    "no se pudo obtener precio",
    "fuera de horario operativo",
    "no hay posiciones abiertas",
    "señales de compra, 0 errores",
    "analizados, 0 señales de compra",
    "Ciclo completado en",
    "Esperando ",
    "la posicion NO ha cambiado",
    "pidiendo historial de ejecuciones",
    "sin dato",
    "reintentando en 15s",
]


def _es_linea_separadora(linea):
    linea = linea.strip()
    return not linea or set(linea) <= {"=", "#", "-"} or (linea.startswith("##") and linea.endswith("##"))


def _es_linea_ruido(linea):
    return any(fragmento in linea for fragmento in FRAGMENTOS_RUIDO_LOG)


_PATRON_DECIMAL = re.compile(r"(?<!\d)(\d+)\.(\d+)")


def _numeros_a_formato_es(texto):
    return _PATRON_DECIMAL.sub(r"\1,\2", texto)


def obtener_ultimas_lineas_log(n=200):
    ruta = os.path.join(RUTA_BASE, bot.ARCHIVO_LOG)
    if not os.path.exists(ruta):
        return "📄 <b>ACTIVIDAD RECIENTE</b>\n(el bot aun no ha escrito ningun log en esta maquina)"
    try:
        with open(ruta, "r", encoding="utf-8", errors="replace") as f:
            todas = f.readlines()
    except OSError as e:
        return f"No se pudo leer el log: {type(e).__name__}: {e}"

    lineas = [l.rstrip("\n") for l in todas[-n:]
              if not _es_linea_separadora(l) and not _es_linea_ruido(l)]

    if not lineas:
        return "📄 <b>ACTIVIDAD RECIENTE</b>\n(sin compras, ventas ni avisos en las últimas líneas del log)"

    lista = "\n".join(f"• {escapar_html(_numeros_a_formato_es(l))}" for l in lineas)
    if len(lista) > LIMITE_CARACTERES_LOG_TELEGRAM:
        lista = "(...)\n" + lista[-LIMITE_CARACTERES_LOG_TELEGRAM:]
    return "📄 <b>ACTIVIDAD RECIENTE</b>\n" + lista


AYUDA = (
    "🤖 Comandos disponibles:\n"
    "/estado - si el bot esta corriendo, parado o congelado\n"
    "/arrancar - arranca el bot (run.bot.bat)\n"
    "/parar - pide una parada limpia del bot\n"
    "/cartera - posiciones abiertas en todos los mercados\n"
    "/hoy - actividad y operaciones cerradas hoy\n"
    "/ayer - operaciones cerradas ayer\n"
    "/semana - operaciones cerradas esta semana\n"
    "/log - actividad reciente (compras, ventas, avisos)\n"
    "/ayuda - esta lista"
)


def procesar_comando(texto):
    comando = texto.strip().split()[0].lower().lstrip("/") if texto.strip() else ""

    if comando == "estado":
        return consultar_estado()

    if comando == "arrancar":
        return arrancar_bot()

    if comando == "parar":
        return parar_bot()

    if comando == "cartera":
        ib = _conectar_cartera()
        try:
            return cartera.formatear_posiciones_abiertas(ib, html=True)
        finally:
            ib.disconnect()

    if comando in ("hoy", "ayer", "semana"):
        args_falsos = type("Args", (), {
            "ayer": comando == "ayer", "semana": comando == "semana", "desde": None, "hasta": None,
        })()
        desde, hasta = cartera.calcular_rango(args_falsos)
        return (cartera.formatear_actividad(desde, hasta, html=True) + "\n\n"
                + cartera.formatear_operaciones_cerradas(desde, hasta, html=True))

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
