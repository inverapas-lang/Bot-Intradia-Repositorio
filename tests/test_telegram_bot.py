"""
Tests manuales (sin pytest) para telegram_bot.py: solo la logica pura de
procesar_comando() y los helpers de systemctl/journalctl, con subprocess y
cartera_alpaca sustituidos por dobles de prueba. No hace ninguna llamada de
red real a Telegram ni al sistema.
"""
import os
import sys
import types

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("ALPACA_API_KEY", "test-key")
os.environ.setdefault("ALPACA_SECRET_KEY", "test-secret")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test-token")
os.environ.setdefault("TELEGRAM_CHAT_ID", "999")

import telegram_bot as tb

fallos = []


def check(nombre, condicion, detalle=""):
    estado = "OK  " if condicion else "FAIL"
    print(f"[{estado}] {nombre}" + (f" -- {detalle}" if detalle and not condicion else ""))
    if not condicion:
        fallos.append(nombre)


# --- Dobles de prueba ---
tb.cartera.formatear_posiciones_abiertas = lambda html=False, client=None, modo_etiqueta=None: (
    f"POSICIONES_FALSAS[{modo_etiqueta}]" if modo_etiqueta else "POSICIONES_FALSAS")
tb.cartera.formatear_operaciones_cerradas = lambda d, h, html=False: f"CERRADAS de {d} a {h}"
tb.cartera.formatear_actividad = lambda d, h, html=False: f"ACTIVIDAD de {d} a {h}"

llamadas_subprocess = []


def _run_falso_ok(cmd, **kwargs):
    llamadas_subprocess.append(cmd)
    if cmd[:2] == ["systemctl", "is-active"]:
        return types.SimpleNamespace(stdout="active\n", returncode=0)
    if cmd[:2] == ["sudo", "systemctl"]:
        return types.SimpleNamespace(returncode=0, stdout="", stderr="")
    if cmd[0] == "journalctl":
        return types.SimpleNamespace(stdout="linea 1\nlinea 2", returncode=0)
    return types.SimpleNamespace(returncode=1, stdout="", stderr="comando no reconocido")


tb.subprocess.run = _run_falso_ok

# --- 1. Consultas de solo lectura ---
check("/estado -> activo", tb.procesar_comando("/estado") == "🟢 Estado del bot: active")
check("/cartera -> reutiliza cartera_alpaca.formatear_posiciones_abiertas",
      tb.procesar_comando("/cartera") == "POSICIONES_FALSAS")

# --- /carterapaper (peticion del usuario, sept. 2026): consulta la cuenta
#     PAPER aparte, incluso con el bot operando en REAL ---
check("/carterapaper sin ALPACA_PAPER_API_KEY/SECRET_KEY -> aviso claro, no revienta",
      "ALPACA_PAPER_API_KEY" in tb.procesar_comando("/carterapaper"))

tb.ALPACA_PAPER_API_KEY = "paper-key-test"
tb.ALPACA_PAPER_SECRET_KEY = "paper-secret-test"
tb.bot.TradingClient = lambda *a, **k: types.SimpleNamespace(_falso_cliente_paper=True)
tb.bot._forzar_timeout_por_defecto = lambda cliente: None
try:
    check("/carterapaper con claves puestas -> reutiliza formatear_posiciones_abiertas con modo_etiqueta=PAPER",
          tb.procesar_comando("/carterapaper") == "POSICIONES_FALSAS[PAPER]",
          f"resultado={tb.procesar_comando('/carterapaper')!r}")
finally:
    tb.ALPACA_PAPER_API_KEY = ""
    tb.ALPACA_PAPER_SECRET_KEY = ""
    tb._cliente_paper = None
check("/log -> reutiliza journalctl, en forma de lista con viñetas",
      "linea 1\nlinea 2".replace("\n", "") not in tb.procesar_comando("/log").replace("\n", "")
      and "• linea 1" in tb.procesar_comando("/log") and "• linea 2" in tb.procesar_comando("/log"))
check("/log -> usa 'journalctl -o cat' (sin prefijo de fecha/host/PID duplicado)",
      any(cmd == ["journalctl", "-u", "bot-alpaca", "-n", "150", "--no-pager", "-o", "cat"]
          for cmd in llamadas_subprocess if cmd and cmd[0] == "journalctl"),
      f"llamadas={[c for c in llamadas_subprocess if c and c[0] == 'journalctl']}")

# --- 1b. /log quita separadores y ruido rutinario, y convierte numeros a formato español ---


def _run_falso_log_con_ruido(cmd, **kwargs):
    if cmd[0] == "journalctl":
        return types.SimpleNamespace(
            stdout="============================================================\n"
                   "[2026-08-31 12:47:22] Iniciando nuevo ciclo de revision.\n"
                   "\n"
                   "########## VENTAS ##########\n"
                   "[2026-08-31 12:47:23] VENTAS: AMD - beneficio -2.42%, por debajo del umbral -> se mantiene.\n"
                   "[2026-08-31 12:47:24] VENTAS: META - beneficio 1.16%, MACD 5min BAJISTA -> VENDIENDO "
                   "(orden limitada al precio exacto).\n"
                   "[2026-08-31 12:47:24] VENTAS: META - orden colocada, estado: filled\n"
                   "[2026-08-31 12:47:25] COMPRAS: 6 analizados, 0 señales de compra, 0 errores.\n"
                   "[2026-08-31 12:47:26] COMPRAS CRIPTO: 6 analizados, 1 señales de compra, 0 errores.",
            returncode=0)
    return types.SimpleNamespace(returncode=1, stdout="", stderr="")


run_original_log = tb.subprocess.run
tb.subprocess.run = _run_falso_log_con_ruido
try:
    resultado_log = tb.procesar_comando("/log")
    check("/log: quita las lineas separadoras puramente decorativas ('===...')",
          "====" not in resultado_log, f"resultado={resultado_log!r}")
    check("/log: quita el ruido rutinario ('se mantiene', 'Iniciando nuevo ciclo', cabecera de seccion)",
          "se mantiene" not in resultado_log and "Iniciando nuevo ciclo" not in resultado_log
          and "##########" not in resultado_log,
          f"resultado={resultado_log!r}")
    check("/log: SI conserva las lineas de accion real (VENDIENDO, orden colocada)",
          "VENDIENDO" in resultado_log and "orden colocada" in resultado_log)
    check("/log: convierte el punto decimal a coma (formato español)",
          "1,16%" in resultado_log and "1.16%" not in resultado_log, f"resultado={resultado_log!r}")
    check("/log: quita el resumen de analisis SIN señales (0 señales de compra, 0 errores)",
          "0 señales de compra" not in resultado_log, f"resultado={resultado_log!r}")
    check("/log: SI conserva el resumen de analisis CON señales de compra (bug corregido sept. 2026: "
          "antes se filtraba por error cualquier resumen con 0 errores, aunque hubiera señales)",
          "1 señales de compra" in resultado_log, f"resultado={resultado_log!r}")
finally:
    tb.subprocess.run = run_original_log
check("/ayuda -> lista de comandos", "/estado" in tb.procesar_comando("/ayuda"))
check("comando desconocido -> mensaje de error amigable, no excepcion",
      "No entiendo" in tb.procesar_comando("/algo_que_no_existe"))

# --- 2. Rangos de fecha por comando (hoy/ayer/semana) ---
llamadas_subprocess.clear()
resultado_hoy = tb.procesar_comando("/hoy")
resultado_ayer = tb.procesar_comando("/ayer")
resultado_semana = tb.procesar_comando("/semana")
check("/hoy y /ayer piden rangos DISTINTOS", resultado_hoy != resultado_ayer,
      f"hoy={resultado_hoy!r} ayer={resultado_ayer!r}")
check("/semana no lanza excepcion y devuelve texto", isinstance(resultado_semana, str) and resultado_semana)
check("/hoy combina el resumen de ACTIVIDAD con el detalle de CERRADAS",
      resultado_hoy.startswith("ACTIVIDAD de") and "CERRADAS de" in resultado_hoy,
      f"resultado_hoy={resultado_hoy!r}")

# --- 3. Arrancar/parar: exito ---
tb.subprocess.run = _run_falso_ok
check("/arrancar -> llama a 'systemctl start' via sudo, sin error",
      tb.procesar_comando("/arrancar") == "🟢 Bot arrancado.")
check("/parar -> llama a 'systemctl stop' via sudo, sin error",
      tb.procesar_comando("/parar") == "🔴 Bot parado.")
check("/arrancar SI ejecuto el comando sudo systemctl start bot-alpaca",
      ["sudo", "systemctl", "start", "bot-alpaca"] in llamadas_subprocess,
      f"llamadas={llamadas_subprocess}")

# --- 4. Arrancar/parar: fallo (p.ej. falta el permiso de sudoers) ---


def _run_falso_falla(cmd, **kwargs):
    if cmd[:2] == ["sudo", "systemctl"]:
        return types.SimpleNamespace(returncode=1, stdout="", stderr="sudo: a password is required")
    return types.SimpleNamespace(returncode=1, stdout="", stderr="")


run_original = tb.subprocess.run
tb.subprocess.run = _run_falso_falla
try:
    resultado_fallo = tb.procesar_comando("/arrancar")
    check("/arrancar con fallo de permisos -> devuelve aviso claro, no revienta",
          "No se pudo" in resultado_fallo and "password" in resultado_fallo,
          f"resultado={resultado_fallo!r}")
finally:
    tb.subprocess.run = run_original


# --- 4b. /actualizar (peticion del usuario, sept. 2026: poder desplegar
#     cambios sin SSH, solo desde Telegram): git pull + reinicio del
#     servicio si trajo cambios. ---
def _run_falso_pull_con_cambios(cmd, **kwargs):
    llamadas_subprocess.append(cmd)
    if cmd[:2] == ["git", "pull"]:
        return types.SimpleNamespace(returncode=0, stdout="Updating abc123..def456\n 3 files changed\n", stderr="")
    if cmd[:2] == ["sudo", "systemctl"]:
        return types.SimpleNamespace(returncode=0, stdout="", stderr="")
    return types.SimpleNamespace(returncode=1, stdout="", stderr="comando no reconocido")


llamadas_subprocess.clear()
tb.subprocess.run = _run_falso_pull_con_cambios
try:
    resultado_actualizar = tb.procesar_comando("/actualizar")
    check("/actualizar: con cambios nuevos, hace git pull Y reinicia bot-alpaca",
          "✅" in resultado_actualizar and "actualizado" in resultado_actualizar.lower()
          and ["sudo", "systemctl", "restart", "bot-alpaca"] in llamadas_subprocess,
          f"resultado={resultado_actualizar!r}, llamadas={llamadas_subprocess}")
    check("/actualizar: el mensaje incluye la salida de git pull",
          "abc123" in resultado_actualizar, f"resultado={resultado_actualizar!r}")
finally:
    tb.subprocess.run = run_original


def _run_falso_pull_sin_cambios(cmd, **kwargs):
    llamadas_subprocess.append(cmd)
    if cmd[:2] == ["git", "pull"]:
        return types.SimpleNamespace(returncode=0, stdout="Already up to date.\n", stderr="")
    return types.SimpleNamespace(returncode=1, stdout="", stderr="no deberia llamarse")


llamadas_subprocess.clear()
tb.subprocess.run = _run_falso_pull_sin_cambios
try:
    resultado_sin_cambios = tb.procesar_comando("/actualizar")
    check("/actualizar: sin cambios nuevos (Already up to date), NO reinicia el bot",
          "Ya estaba actualizado" in resultado_sin_cambios
          and not any(cmd[:2] == ["sudo", "systemctl"] for cmd in llamadas_subprocess),
          f"resultado={resultado_sin_cambios!r}, llamadas={llamadas_subprocess}")
finally:
    tb.subprocess.run = run_original


def _run_falso_pull_conflicto(cmd, **kwargs):
    llamadas_subprocess.append(cmd)
    if cmd[:2] == ["git", "pull"]:
        return types.SimpleNamespace(returncode=1, stdout="",
                                      stderr="error: your local changes would be overwritten by merge")
    return types.SimpleNamespace(returncode=1, stdout="", stderr="no deberia llamarse")


llamadas_subprocess.clear()
tb.subprocess.run = _run_falso_pull_conflicto
try:
    resultado_conflicto = tb.procesar_comando("/actualizar")
    check("/actualizar: si git pull falla (p.ej. cambios locales sin commitear), avisa "
          "claramente y NO reinicia el bot",
          "⚠️" in resultado_conflicto and "local changes" in resultado_conflicto
          and not any(cmd[:2] == ["sudo", "systemctl"] for cmd in llamadas_subprocess),
          f"resultado={resultado_conflicto!r}, llamadas={llamadas_subprocess}")
finally:
    tb.subprocess.run = run_original


# --- 5. Comando que lanza una excepcion inesperada no rompe el bucle principal ---
def _formatear_que_falla(html=False):
    raise RuntimeError("fallo simulado en cartera")


formatear_original = tb.cartera.formatear_posiciones_abiertas
tb.cartera.formatear_posiciones_abiertas = _formatear_que_falla
try:
    # procesar_comando() propaga la excepcion (se captura en bucle_principal,
    # no aqui); comprobamos que al menos no corrompe el estado del modulo.
    exception_capturada = None
    try:
        tb.procesar_comando("/cartera")
    except RuntimeError as e:
        exception_capturada = e
    check("/cartera con fallo interno: la excepcion se propaga limpia (la captura bucle_principal)",
          exception_capturada is not None and "fallo simulado" in str(exception_capturada))
finally:
    tb.cartera.formatear_posiciones_abiertas = formatear_original


if fallos:
    print(f"\n{len(fallos)} test(s) FALLARON: {fallos}")
    sys.exit(1)
else:
    print("\nTodos los tests pasaron correctamente.")
