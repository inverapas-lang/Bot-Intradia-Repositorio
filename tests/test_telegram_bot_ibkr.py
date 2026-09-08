"""
Tests manuales (sin pytest) para telegram_bot_ibkr.py: solo la logica pura
de procesar_comando() y los helpers de estado/arranque/parada/log, con
subprocess, os.path y cartera_ibkr sustituidos por dobles de prueba cuando
hace falta. No hace ninguna llamada de red real a Telegram, ni toca
procesos ni archivos reales del bot.
"""
import os
import sys
import tempfile
import types

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test-token")
os.environ.setdefault("TELEGRAM_CHAT_ID", "999")

import telegram_bot_ibkr as tib

fallos = []


def check(nombre, condicion, detalle=""):
    estado = "OK  " if condicion else "FAIL"
    print(f"[{estado}] {nombre}" + (f" -- {detalle}" if detalle and not condicion else ""))
    if not condicion:
        fallos.append(nombre)


# Todas las pruebas de /estado, /arrancar y /parar corren dentro de un
# directorio temporal (RUTA_BASE apuntando ahi), para no tocar nunca los
# archivos reales (bot.pid, latido_bot.txt, detener_bot.flag) de una sesion
# real del bot que pudiera estar corriendo en esta misma maquina.
directorio_temporal = tempfile.TemporaryDirectory()
tib.RUTA_BASE = directorio_temporal.name
tib.RUTA_RUN_BOT_BAT = os.path.join(tib.RUTA_BASE, "run.bot.bat")


def _escribir(nombre_archivo, contenido):
    with open(os.path.join(tib.RUTA_BASE, nombre_archivo), "w", encoding="utf-8") as f:
        f.write(contenido)


def _borrar_si_existe(nombre_archivo):
    ruta = os.path.join(tib.RUTA_BASE, nombre_archivo)
    if os.path.exists(ruta):
        os.remove(ruta)


# _proceso_vivo() llama a "tasklist", un comando SOLO de Windows: en esta
# maquina de pruebas (Linux) no existe, asi que se sustituye por un doble
# controlable en vez de depender del sistema operativo real.
_vivo_simulado = {"valor": False}
tib._proceso_vivo = lambda pid: _vivo_simulado["valor"]

# --- 1. /estado ---
_borrar_si_existe(tib.bot.ARCHIVO_PID)
_borrar_si_existe(tib.bot.ARCHIVO_LATIDO)
check("/estado sin bot.pid -> parado", tib.consultar_estado().startswith("🔴"))

_vivo_simulado["valor"] = False
_escribir(tib.bot.ARCHIVO_PID, "99999999")  # PID que casi seguro no existe
check("/estado con PID que no existe -> parado", tib.consultar_estado().startswith("🔴"))

pid_actual = os.getpid()
_vivo_simulado["valor"] = True
_escribir(tib.bot.ARCHIVO_PID, str(pid_actual))
check("/estado con PID vivo pero sin latido -> arrancando",
      tib.consultar_estado().startswith("🟡"), f"resultado={tib.consultar_estado()!r}")

_escribir(tib.bot.ARCHIVO_LATIDO, "0")  # solo importa la fecha de modificacion del archivo
check("/estado con PID vivo y latido reciente -> corriendo",
      tib.consultar_estado().startswith("🟢"), f"resultado={tib.consultar_estado()!r}")

# Simula un latido MUY antiguo (mas del umbral de congelacion) tocando la
# fecha de modificacion del archivo hacia el pasado.
ruta_latido = os.path.join(tib.RUTA_BASE, tib.bot.ARCHIVO_LATIDO)
antiguo = os.path.getmtime(ruta_latido) - (tib.bot.UMBRAL_CONGELACION_SEGUNDOS + 120)
os.utime(ruta_latido, (antiguo, antiguo))
check("/estado con latido viejo (> umbral de congelacion) -> posible congelacion",
      tib.consultar_estado().startswith("🟠"), f"resultado={tib.consultar_estado()!r}")

_borrar_si_existe(tib.bot.ARCHIVO_PID)
_borrar_si_existe(tib.bot.ARCHIVO_LATIDO)


# --- 2. /arrancar ---
llamadas_popen = []
tib.subprocess.Popen = lambda *a, **k: llamadas_popen.append((a, k))

_borrar_si_existe(tib.bot.ARCHIVO_PID)
_escribir("run.bot.bat", "echo dummy")
_escribir(tib.bot.ARCHIVO_DETENER, "restos de una parada anterior")
resultado_arrancar = tib.arrancar_bot()
check("/arrancar (bot parado) -> lanza run.bot.bat y confirma",
      resultado_arrancar.startswith("🟢"), f"resultado={resultado_arrancar!r}")
check("/arrancar -> SI llamo a subprocess.Popen", len(llamadas_popen) == 1, f"llamadas={llamadas_popen}")
check("/arrancar -> borra cualquier flag de parada pendiente antes de lanzar",
      not os.path.exists(os.path.join(tib.RUTA_BASE, tib.bot.ARCHIVO_DETENER)))

llamadas_popen.clear()
_escribir(tib.bot.ARCHIVO_PID, str(os.getpid()))  # simula que YA esta corriendo
resultado_arrancar_ya_activo = tib.arrancar_bot()
check("/arrancar con el bot YA corriendo -> aviso, no relanza",
      "ya esta corriendo" in resultado_arrancar_ya_activo, f"resultado={resultado_arrancar_ya_activo!r}")
check("/arrancar con el bot YA corriendo -> NO llama a Popen", len(llamadas_popen) == 0)

_borrar_si_existe(tib.bot.ARCHIVO_PID)
os.remove(os.path.join(tib.RUTA_BASE, "run.bot.bat"))
resultado_sin_bat = tib.arrancar_bot()
check("/arrancar sin run.bot.bat en la carpeta -> aviso claro, no revienta",
      "No se encuentra" in resultado_sin_bat, f"resultado={resultado_sin_bat!r}")


# --- 3. /parar ---
_borrar_si_existe(tib.bot.ARCHIVO_PID)
resultado_parar_ya_parado = tib.parar_bot()
check("/parar con el bot YA parado -> aviso, no crea el flag",
      "ya esta parado" in resultado_parar_ya_parado, f"resultado={resultado_parar_ya_parado!r}")
check("/parar con el bot YA parado -> no crea detener_bot.flag",
      not os.path.exists(os.path.join(tib.RUTA_BASE, tib.bot.ARCHIVO_DETENER)))

_escribir(tib.bot.ARCHIVO_PID, str(os.getpid()))
resultado_parar = tib.parar_bot()
check("/parar con el bot corriendo -> confirma parada solicitada",
      resultado_parar.startswith("🟡"), f"resultado={resultado_parar!r}")
check("/parar con el bot corriendo -> SI crea detener_bot.flag (parada cooperativa, no kill)",
      os.path.exists(os.path.join(tib.RUTA_BASE, tib.bot.ARCHIVO_DETENER)))

_borrar_si_existe(tib.bot.ARCHIVO_PID)
_borrar_si_existe(tib.bot.ARCHIVO_DETENER)


# --- 4. /cartera, /hoy, /ayer, /semana: dobles de prueba de cartera_ibkr ---
tib.cartera.formatear_posiciones_abiertas = lambda ib, html=False: "POSICIONES_FALSAS"
tib.cartera.formatear_operaciones_cerradas = lambda d, h, html=False: f"CERRADAS de {d} a {h}"
tib.cartera.formatear_actividad = lambda d, h, html=False: f"ACTIVIDAD de {d} a {h}"


class _IBFalsaCartera:
    def disconnect(self):
        pass


tib._conectar_cartera = lambda: _IBFalsaCartera()

check("/cartera -> reutiliza cartera_ibkr.formatear_posiciones_abiertas",
      tib.procesar_comando("/cartera") == "POSICIONES_FALSAS")

resultado_hoy = tib.procesar_comando("/hoy")
resultado_ayer = tib.procesar_comando("/ayer")
resultado_semana = tib.procesar_comando("/semana")
check("/hoy y /ayer piden rangos DISTINTOS", resultado_hoy != resultado_ayer,
      f"hoy={resultado_hoy!r} ayer={resultado_ayer!r}")
check("/semana no lanza excepcion y devuelve texto", isinstance(resultado_semana, str) and resultado_semana)
check("/hoy combina el resumen de ACTIVIDAD con el detalle de CERRADAS",
      resultado_hoy.startswith("ACTIVIDAD de") and "CERRADAS de" in resultado_hoy,
      f"resultado_hoy={resultado_hoy!r}")

check("/ayuda -> lista de comandos", "/estado" in tib.procesar_comando("/ayuda"))
check("comando desconocido -> mensaje de error amigable, no excepcion",
      "No entiendo" in tib.procesar_comando("/algo_que_no_existe"))


# --- 5. /log: sin archivo, y con ruido/numeros a formatear ---
_borrar_si_existe(tib.bot.ARCHIVO_LOG)
check("/log sin bot_completo.log todavia -> aviso claro, no revienta",
      "aun no ha escrito" in tib.obtener_ultimas_lineas_log())

_escribir(tib.bot.ARCHIVO_LOG,
          "============================================================\n"
          "[2026-09-03 12:47:22] Iniciando nuevo ciclo de revision.\n"
          "\n"
          "########## VENTAS - MERCADO US ##########\n"
          "[2026-09-03 12:47:23] VENTAS: AMD - beneficio -2.42%, por debajo del umbral -> se mantiene.\n"
          "[2026-09-03 12:47:24] VENTAS: META - beneficio 1.16%, MACD 5min BAJISTA -> VENDIENDO "
          "(orden limitada al precio exacto).\n"
          "[2026-09-03 12:47:24] VENTAS: META - orden limitada, estado: Filled\n")
resultado_log = tib.obtener_ultimas_lineas_log()
check("/log: quita las lineas separadoras puramente decorativas ('===...')",
      "====" not in resultado_log, f"resultado={resultado_log!r}")
check("/log: quita el ruido rutinario ('se mantiene', 'Iniciando nuevo ciclo', cabecera de seccion)",
      "se mantiene" not in resultado_log and "Iniciando nuevo ciclo" not in resultado_log
      and "##########" not in resultado_log,
      f"resultado={resultado_log!r}")
check("/log: SI conserva las lineas de accion real (VENDIENDO, orden colocada)",
      "VENDIENDO" in resultado_log and "estado: Filled" in resultado_log)
check("/log: convierte el punto decimal a coma (formato español)",
      "1,16%" in resultado_log and "1.16%" not in resultado_log, f"resultado={resultado_log!r}")

# --- 5b. /log: fecha agrupada (una sola vez), solo horas, y sin huecos en
# blanco ni banners "==== texto ====" sin procesar (sept. 2026, petición
# del usuario: el log salia dificil de leer en Telegram) ---
_borrar_si_existe(tib.bot.ARCHIVO_LOG)
_escribir(tib.bot.ARCHIVO_LOG,
          "[2026-09-07 22:52:38] \n"
          "[2026-09-07 22:52:48] Ciclo completado.\n"
          "[2026-09-07 22:53:36] VENTAS: BTC - beneficio -2.48% -> se mantiene.\n"
          "[2026-09-08 07:46:12] Reinicio del bot.\n"
          "========== RESUMEN DE CIERRE - MERCADO CRYPTO ==========\n"
          "[2026-09-08 07:46:13] IBKR API - error 321 [BTC]: Please enter exchange\n")
resultado_log = tib.obtener_ultimas_lineas_log()
check("/log: la fecha aparece como cabecera, no repetida en cada linea",
      resultado_log.count("2026-09-07") == 1 and resultado_log.count("2026-09-08") == 1,
      f"resultado={resultado_log!r}")
check("/log: cada linea de contenido muestra solo la hora (HH:MM:SS), sin repetir la fecha",
      "• 22:52:48 Ciclo completado." in resultado_log and "07:46:12 Reinicio del bot." in resultado_log,
      f"resultado={resultado_log!r}")
check("/log: se saltan los huecos en blanco (linea con marca de tiempo pero sin mensaje)",
      "22:52:38" not in resultado_log, f"resultado={resultado_log!r}")
check("/log: una cabecera '==== texto ====' se muestra sin el relleno de '='",
      "▸" in resultado_log and "RESUMEN DE CIERRE - MERCADO CRYPTO" in resultado_log
      and "====" not in resultado_log,
      f"resultado={resultado_log!r}")

_borrar_si_existe(tib.bot.ARCHIVO_LOG)
directorio_temporal.cleanup()


if fallos:
    print(f"\n{len(fallos)} test(s) FALLARON: {fallos}")
    sys.exit(1)
else:
    print("\nTodos los tests pasaron correctamente.")
