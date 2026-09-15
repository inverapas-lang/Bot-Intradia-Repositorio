"""
telegram_bot_ibkr.py - control y consulta del bot de IBKR (bot_completo.py)
desde Telegram (movil).

Proceso APARTE del propio bot de trading. Funciona en DOS entornos
distintos, detectados automaticamente (ver EN_LINUX):
  - Windows (el PC donde tradicionalmente corre IB Gateway/TWS y
    bot_completo.py): sin systemd, arrancar/parar/actualizar usan
    run.bot.bat + un archivo de PID + un archivo de señal de parada.
  - Linux (sept. 2026, petición del usuario: migrar bot_completo.py +
    telegram_bot_ibkr.py a un servidor en la nube, igual que Alpaca, mientras
    IB Gateway sigue de momento en el PC -ver IBKR_HOST en bot_completo.py-):
    con systemd, arrancar/parar/actualizar son iguales que en
    telegram_bot.py/Alpaca (systemctl, atomico).
Usa "long polling" contra la API HTTP de Telegram (metodo getUpdates): no
hace falta libreria extra, solo `requests`.

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
    /actualizar  - git pull, para desplegar sin necesitar SSH. En DOS PASOS:
                   si trae cambios y el bot esta corriendo, pide la parada
                   (igual que /parar) y NO arranca nada por su cuenta -para
                   no arriesgarse a tener DOS instancias corriendo a la vez
                   con dinero real-; hay que confirmar con /estado que ya
                   paro y mandar /arrancar a mano. Si el bot ya estaba
                   parado, arranca directamente con el codigo nuevo.
    /version     - hash + fecha + mensaje del commit REALMENTE en marcha
                   ahora mismo (para confirmar si un fix concreto ya esta
                   desplegado, sin fiarse de la memoria)
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
import platform
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

# En Linux (sept. 2026, petición del usuario: mover bot_completo.py +
# telegram_bot_ibkr.py a un servidor en la nube, igual que ya se hizo con
# Alpaca) SI hay systemd, así que arrancar/parar/reiniciar es atómico -a
# diferencia de Windows, donde el proceso viejo tarda hasta 30-60s en morir
# de verdad tras la señal de parada, y lanzar uno nuevo antes de eso podría
# dejar DOS instancias corriendo con dinero real-. EN_LINUX decide qué
# mecanismo usar en cada función; el resto del código (PID, latido, flag de
# parada) sigue funcionando igual en los dos sistemas operativos, solo
# cambia CÓMO se arranca/para/reinicia el proceso.
EN_LINUX = platform.system() != "Windows"
NOMBRE_SERVICIO_BOT = "bot-ibkr"
NOMBRE_SERVICIO_TELEGRAM = "telegram-bot-ibkr"


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


def _ejecutar_systemctl(accion, servicio):
    """Igual que ejecutar_systemctl() de telegram_bot.py/Alpaca: requiere
    que el sudoers del usuario permita este comando exacto sin contraseña
    (ver NOTES.md)."""
    try:
        resultado = subprocess.run(
            ["sudo", "systemctl", accion, servicio],
            capture_output=True, text=True, timeout=20
        )
        if resultado.returncode != 0:
            return f"⚠️ No se pudo {accion} {servicio}: {resultado.stderr.strip() or resultado.stdout.strip()}"
        return None
    except Exception as e:
        return f"⚠️ Error al intentar {accion} {servicio}: {type(e).__name__}: {e}"


def _consultar_estado_servicio(servicio):
    try:
        resultado = subprocess.run(
            ["systemctl", "is-active", servicio],
            capture_output=True, text=True, timeout=10
        )
        return resultado.stdout.strip()
    except Exception as e:
        return f"desconocido ({type(e).__name__})"


def consultar_estado():
    if EN_LINUX:
        estado = _consultar_estado_servicio(NOMBRE_SERVICIO_BOT)
        emoji = "🟢" if estado == "active" else "🔴"
        return f"{emoji} Estado del bot ({NOMBRE_SERVICIO_BOT}): {estado}"

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
    if EN_LINUX:
        error = _ejecutar_systemctl("start", NOMBRE_SERVICIO_BOT)
        return error or f"🟢 Bot arrancado ({NOMBRE_SERVICIO_BOT})."

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
    if EN_LINUX:
        error = _ejecutar_systemctl("stop", NOMBRE_SERVICIO_BOT)
        return error or f"🔴 Bot parado ({NOMBRE_SERVICIO_BOT})."

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


def _reiniciar_telegram_bot_ibkr_diferido():
    """Igual que _reiniciar_telegram_bot_diferido() de telegram_bot.py/Alpaca:
    reinicia el propio servicio (telegram-bot-ibkr) con un pequeño retraso en
    un proceso hijo desatendido, para dar tiempo a que este mensaje de
    /actualizar termine de enviarse antes de que el proceso muera. Solo en
    Linux -en Windows no hay systemd, este script sigue sin poder
    reiniciarse a si mismo-."""
    try:
        subprocess.Popen(
            ["bash", "-c", f"sleep 3 && sudo systemctl restart {NOMBRE_SERVICIO_TELEGRAM}"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True,
        )
        return None
    except Exception as e:
        return f"⚠️ Error al programar el reinicio de {NOMBRE_SERVICIO_TELEGRAM}: {type(e).__name__}: {e}"


def actualizar_bot():
    """/actualizar (peticion del usuario, sept. 2026: poder desplegar
    cambios sin necesitar un cliente SSH, solo desde Telegram).

    En LINUX (desplegado con systemd, ver NOTES.md): igual que /actualizar
    de telegram_bot.py/Alpaca -hace git pull y, si trajo cambios, reinicia
    bot-ibkr de forma ATOMICA ('systemctl restart', sin la espera de 30-60s
    de Windows) y programa el reinicio de este propio proceso
    (telegram-bot-ibkr) con un pequeño retraso.

    En WINDOWS: version en DOS PASOS (opcion elegida explicitamente por el
    usuario en su momento, en vez de la bloqueante con espera activa). Aqui
    parar NO es atomico: es cooperativo y asincrono (run.bot.bat, sin
    systemd) - el bot tarda hasta 30-60s en detenerse de verdad tras la
    señal. Lanzar run.bot.bat de nuevo ANTES de que el proceso viejo muera
    del todo dejaria DOS instancias corriendo a la vez con dinero real -el
    riesgo mas serio a evitar-, asi que en Windows esta funcion NUNCA
    arranca nada automaticamente tras pedir la parada: hace git pull y, si
    el bot estaba corriendo, pide la parada (igual que /parar) y devuelve
    el mensaje diciendo que hay que confirmar con /estado y mandar
    /arrancar a mano cuando se vea parado. Si el bot ya estaba parado de
    antemano, arranca directamente (no hay nada que esperar)."""
    try:
        resultado_pull = subprocess.run(
            ["git", "pull"], cwd=RUTA_BASE, capture_output=True, text=True, timeout=30
        )
    except Exception as e:
        return f"⚠️ Error al ejecutar git pull: {type(e).__name__}: {e}"

    salida_pull = (resultado_pull.stdout + resultado_pull.stderr).strip()
    if resultado_pull.returncode != 0:
        return f"⚠️ git pull falló, no se ha tocado el bot:\n<pre>{escapar_html(salida_pull)}</pre>"

    if "Already up to date" in salida_pull or "ya está actualizado" in salida_pull.lower():
        return f"✅ Ya estaba actualizado, no había cambios nuevos.\n<pre>{escapar_html(salida_pull)}</pre>"

    if EN_LINUX:
        error_reinicio = _ejecutar_systemctl("restart", NOMBRE_SERVICIO_BOT)
        error_reinicio_telegram = _reiniciar_telegram_bot_ibkr_diferido()
        if error_reinicio:
            aviso = f"\n\n{error_reinicio_telegram}" if error_reinicio_telegram else ""
            return (f"✅ Código actualizado, pero falló el reinicio de {NOMBRE_SERVICIO_BOT}:\n"
                    f"<pre>{escapar_html(salida_pull)}</pre>\n\n{error_reinicio}{aviso}")
        if error_reinicio_telegram:
            return (f"✅ Código actualizado y {NOMBRE_SERVICIO_BOT} reiniciado:\n"
                    f"<pre>{escapar_html(salida_pull)}</pre>\n\n{error_reinicio_telegram}")
        return (f"✅ Código actualizado, {NOMBRE_SERVICIO_BOT} reiniciado ya:\n"
                f"<pre>{escapar_html(salida_pull)}</pre>\n\n"
                f"🔄 Este bot de Telegram se reiniciará solo en unos segundos para aplicar el cambio.")

    pid = _pid_registrado()
    bot_estaba_corriendo = pid is not None and _proceso_vivo(pid)

    if not bot_estaba_corriendo:
        resultado_arranque = arrancar_bot()
        return (f"✅ Código actualizado (el bot ya estaba parado):\n<pre>{escapar_html(salida_pull)}</pre>\n\n"
                f"{resultado_arranque}")

    resultado_parada = parar_bot()
    return (f"✅ Código actualizado:\n<pre>{escapar_html(salida_pull)}</pre>\n\n{resultado_parada}\n\n"
            f"👉 Cuando /estado confirme que está parado, manda /arrancar para que arranque ya con "
            f"el código nuevo.")


def consultar_version():
    """/version (peticion del usuario, sept. 2026, mismo comando ya añadido
    a telegram_bot.py/Alpaca: poder confirmar en cualquier momento que
    commit esta REALMENTE en marcha, sin depender de la memoria -esto
    importa mas aun en IBKR, donde /actualizar no reinicia solo el bot
    tras el 'git pull', hay que confirmarlo a mano con /estado y
    /arrancar-)."""
    try:
        resultado = subprocess.run(
            ["git", "log", "-1", "--format=%h %ad %s", "--date=iso"],
            cwd=RUTA_BASE, capture_output=True, text=True, timeout=10
        )
        if resultado.returncode != 0:
            return f"⚠️ No se pudo leer el commit actual: {resultado.stderr.strip()}"
        return f"📌 Commit en marcha ahora mismo:\n<pre>{escapar_html(resultado.stdout.strip())}</pre>"
    except Exception as e:
        return f"⚠️ Error al leer el commit actual: {type(e).__name__}: {e}"


def _conectar_cartera():
    ib = bot.IB()
    ib.connect(bot.IBKR_HOST, 4002, clientId=cartera.CLIENT_ID_CARTERA, timeout=15)
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


_PATRON_LINEA_LOG = re.compile(r"^\[(\d{4}-\d{2}-\d{2}) (\d{2}:\d{2}:\d{2})\]\s?(.*)$")
_PATRON_BANNER = re.compile(r"^=+\s*(.*[a-zA-Z0-9].*?)\s*=+$")


def _formatear_lineas_log(lineas):
    """Agrupa las lineas del log por fecha (una sola cabecera de fecha, no
    repetida en cada linea) y muestra solo la hora en cada una -mucho mas
    facil de leer en el movil que repetir 'AAAA-MM-DD HH:MM:SS' entero en
    cada linea-. Se saltan las lineas cuyo mensaje queda vacio tras quitar
    la marca de tiempo (los huecos en blanco que separan tramos del log,
    p.ej. entre "Ciclo completado." y el inicio del siguiente ciclo), y las
    cabeceras decorativas tipo '========== texto ==========' se muestran
    sin el relleno de '=', a modo de sub-titulo de seccion."""
    bloques = []
    fecha_actual = None
    for linea in lineas:
        m = _PATRON_LINEA_LOG.match(linea)
        fecha, hora, resto = m.groups() if m else (None, None, linea)
        resto = resto.strip()
        if not resto:
            continue
        resto = escapar_html(_numeros_a_formato_es(resto))
        if fecha and fecha != fecha_actual:
            bloques.append(f"📅 <b>{fecha}</b>")
            fecha_actual = fecha
        m_banner = _PATRON_BANNER.match(resto)
        if m_banner:
            bloques.append(f"▸ <b>{m_banner.group(1)}</b>")
        elif hora:
            bloques.append(f"• {hora} {resto}")
        else:
            bloques.append(f"• {resto}")
    return "\n".join(bloques)


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

    lista = _formatear_lineas_log(lineas)
    if not lista:
        return "📄 <b>ACTIVIDAD RECIENTE</b>\n(sin compras, ventas ni avisos en las últimas líneas del log)"

    if len(lista) > LIMITE_CARACTERES_LOG_TELEGRAM:
        lista = "(...)\n" + lista[-LIMITE_CARACTERES_LOG_TELEGRAM:]
    return "📄 <b>ACTIVIDAD RECIENTE</b>\n" + lista


AYUDA = (
    "🤖 Comandos disponibles:\n"
    "/estado - si el bot esta corriendo, parado o congelado\n"
    "/arrancar - arranca el bot (run.bot.bat)\n"
    "/parar - pide una parada limpia del bot\n"
    "/actualizar - descarga el codigo mas reciente (git pull) y pide la parada "
    "del bot si hace falta; confirma con /estado y manda /arrancar cuando pare\n"
    "/version - que commit de codigo esta REALMENTE en marcha ahora mismo\n"
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

    if comando == "actualizar":
        return actualizar_bot()

    if comando == "version":
        return consultar_version()

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
