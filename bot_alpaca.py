"""
BOT ALPACA - hermano de bot_completo.py (IBKR), pensado para correr EN
PARALELO a el. Misma logica de señales (MACD multi-temporalidad) y las
mismas reglas de compra/venta, pero conectado a Alpaca en vez de a IBKR,
y limitado al mercado de EE.UU. (Alpaca no cubre HK ni KR).

Por que Alpaca: API oficial pensada para trading algoritmico (no hace falta
tener una app de escritorio abierta como con IB Gateway -esto es una API
REST/HTTPS normal-, lo que lo hace mucho mas facil de mover a un servidor
en la nube mas adelante), comision 0 EUR en acciones/ETFs de US, y
fracciones de accion soportadas de forma nativa via API. Se asume 0 EUR de
comision tanto en compras como en ventas (a peticion expresa del usuario;
en la realidad Alpaca repercute tasas regulatorias minimas de SEC/FINRA en
ventas, insignificantes para el tamaño de cartera actual, ver ALPACA_NOTES.md).

Diferencias clave frente al mercado en fracciones, respecto a IBKR:
  - Alpaca SI admite fracciones de verdad por API (a diferencia de los
    problemas que dimos con cashQty en IBKR). Las ordenes A MERCADO admiten
    `qty` fraccionario o `notional`, siempre con time_in_force DAY.
  - Las ordenes fuera de la sesion regular (pre/postmercado, extended_hours)
    exigen tipo LIMITADO con time_in_force DAY (o GTC) -Alpaca rechaza
    ordenes a mercado fuera de la sesion regular directamente-. Desde que
    Alpaca amplio el soporte de fracciones (marzo 2024), estas ordenes
    LIMITADAS con extended_hours=True SI admiten `qty` fraccionario -ya no
    exigen cantidad entera-, asi que una posicion fraccionaria SI se puede
    comprar o vender en pre/postmercado con una orden limitada normal.

Requiere las variables de entorno ALPACA_API_KEY y ALPACA_SECRET_KEY (ver
ALPACA_NOTES.md). Por defecto conecta al entorno PAPER (simulado); hace
falta poner ALPACA_PAPER=false explicitamente para operar con dinero real.

Se detiene con Ctrl+C. Todo el log se imprime en pantalla con fecha y hora.

IMPORTANTE: este script envia ordenes REALES en cuanto ALPACA_PAPER=false.
Revisa bien la configuracion (IMPORTE_EUROS, LIMITE_EXPOSICION_PCT, lista
de activos) antes de dejarlo corriendo desatendido.
"""

import json
import os
import threading
import time
from datetime import date, datetime, time as dt_time, timedelta, timezone
from zoneinfo import ZoneInfo

import pandas as pd
import requests

try:
    from alpaca.trading.client import TradingClient
    from alpaca.trading.requests import MarketOrderRequest, LimitOrderRequest, GetOrdersRequest
    from alpaca.trading.enums import OrderSide, TimeInForce, QueryOrderStatus
    from alpaca.data.historical import StockHistoricalDataClient
    from alpaca.data.historical.crypto import CryptoHistoricalDataClient
    from alpaca.data.requests import StockBarsRequest, CryptoBarsRequest
    from alpaca.data.timeframe import TimeFrame, TimeFrameUnit
except ImportError as e:
    raise SystemExit(
        "Falta instalar el SDK de Alpaca. Ejecuta: pip install alpaca-py\n"
        f"(error original: {e})"
    )

# --- Credenciales (variables de entorno, nunca hardcodeadas en el codigo) ---
ALPACA_API_KEY = os.environ.get("ALPACA_API_KEY", "")
ALPACA_SECRET_KEY = os.environ.get("ALPACA_SECRET_KEY", "")
# Por defecto PAPER (simulado). Hay que poner la variable de entorno
# ALPACA_PAPER=false explicitamente para operar con dinero real -asi no hay
# manera de acabar en real "por accidente" con un valor por defecto mal puesto.
ALPACA_PAPER = os.environ.get("ALPACA_PAPER", "true").strip().lower() != "false"

if not ALPACA_API_KEY or not ALPACA_SECRET_KEY:
    raise SystemExit(
        "Faltan las variables de entorno ALPACA_API_KEY y/o ALPACA_SECRET_KEY. "
        "Ver ALPACA_NOTES.md para como obtenerlas y configurarlas."
    )

# --- Notificaciones a Telegram (opcional) ---
# Si no se configuran estas dos variables, notificar_telegram() simplemente no
# hace nada -el bot funciona igual sin Telegram, esto es un extra opcional-.
# Ver ALPACA_NOTES.md para como crear el bot de Telegram y conseguir estos valores.
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
TELEGRAM_TIMEOUT_SEGUNDOS = 10


def formato_es(numero, decimales=2, signo=False):
    """Formatea un numero al estilo español (punto para miles, coma para
    decimales: 1234.5 -> '1.234,50'), para los mensajes de Telegram y la
    consulta de cartera -pensados para un usuario en España, no en el
    formato anglosajon por defecto de Python (1,234.50)."""
    negativo = numero < 0
    texto = f"{abs(numero):,.{decimales}f}".replace(",", "\x00").replace(".", ",").replace("\x00", ".")
    if negativo:
        return f"-{texto}"
    return f"+{texto}" if signo else texto


def notificar_telegram(mensaje):
    """Envia un mensaje a Telegram (compra/venta ejecutada, resumen diario).
    No lanza excepcion nunca hacia el llamador: un fallo de red o de
    configuracion aqui no debe interrumpir ni un ciclo de trading ni el
    guardado del historial, solo se registra en el log normal."""
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            data={"chat_id": TELEGRAM_CHAT_ID, "text": mensaje, "parse_mode": "HTML"},
            timeout=TELEGRAM_TIMEOUT_SEGUNDOS,
        )
    except Exception as e:
        log(f"No se pudo enviar la notificacion a Telegram: {type(e).__name__}: {e}")

# --- Timeout por defecto para TODAS las peticiones HTTP a Alpaca ---
# BUG REAL visto en produccion (agosto 2026): el SDK alpaca-py (v0.44.0) no
# pone NINGUN timeout por defecto en sus peticiones HTTP (usa requests.Session
# internamente, y sin `timeout` explicito requests espera indefinidamente).
# Un simple corte de red momentaneo mientras se colocaba una orden dejo al
# bot congelado durante mas de 3 HORAS -exactamente el mismo tipo de fallo
# que ya vimos con IBKR (una llamada bloqueante que nunca vuelve), solo que
# aqui a nivel HTTP en vez de socket API-. Como el hilo principal se queda
# literalmente parado dentro de la llamada de red, ni siquiera el vigilante
# interno (hilo daemon) puede reaccionar -mismo problema del GIL que con
# IBKR-. La solucion de raiz aqui es mas directa que con IBKR: forzar un
# timeout razonable en cada peticion HTTP, para que un corte de red se
# traduzca en una excepcion normal (que el try/except de cada ticker ya
# maneja) en vez de una espera infinita.
HTTP_TIMEOUT_SEGUNDOS = 30


def _forzar_timeout_por_defecto(cliente, segundos=HTTP_TIMEOUT_SEGUNDOS):
    """Envuelve el metodo request() de la sesion HTTP interna de un cliente
    de alpaca-py para que SIEMPRE lleve un timeout, aunque la libreria no lo
    ponga por defecto."""
    peticion_original = cliente._session.request

    def peticion_con_timeout(*args, **kwargs):
        kwargs.setdefault("timeout", segundos)
        return peticion_original(*args, **kwargs)

    cliente._session.request = peticion_con_timeout

# --- Horarios de US (identico a bot_completo.py) ---
ZONA_NY = ZoneInfo("America/New_York")
HORA_INICIO_US = dt_time(4, 0)             # 4:00 ET (inicio del premercado)
HORA_APERTURA_REGULAR_US = dt_time(9, 30)  # 9:30 ET (apertura de la sesion regular)
HORA_CIERRE_US = dt_time(16, 0)            # 16:00 ET (cierre regular; sigue siendo la
                                            # referencia de "no comprar antes del cierre" y
                                            # "venta forzada antes del cierre")
HORA_CIERRE_EXTENDIDO_US = dt_time(20, 0)  # 20:00 ET (fin del postmercado)

# --- Festivos del mercado US (NYSE/Nasdaq) - peticion del usuario, sept.
# 2026: "el bot puede identificar los dias festivos en US para no operar
# ese dia?". Misma logica que bot_completo.py (ver ahi la explicacion
# completa): calculados por REGLA, no una lista fija que haya que
# mantener a mano cada año.
def _domingo_pascua(year):
    a = year % 19
    b = year // 100
    c = year % 100
    d = b // 4
    e = b % 4
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i = c // 4
    k = c % 4
    l = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l) // 451
    mes = (h + l - 7 * m + 114) // 31
    dia = ((h + l - 7 * m + 114) % 31) + 1
    return date(year, mes, dia)


def _n_esimo_dia_semana(year, month, weekday_objetivo, n):
    d = date(year, month, 1)
    primero = d + timedelta(days=(weekday_objetivo - d.weekday()) % 7)
    return primero + timedelta(weeks=n - 1)


def _ultimo_dia_semana(year, month, weekday_objetivo):
    siguiente_mes = date(year + 1, 1, 1) if month == 12 else date(year, month + 1, 1)
    d = siguiente_mes - timedelta(days=1)
    while d.weekday() != weekday_objetivo:
        d -= timedelta(days=1)
    return d


def _fecha_observada_nyse(d):
    if d.weekday() == 5:
        return d - timedelta(days=1)
    if d.weekday() == 6:
        return d + timedelta(days=1)
    return d


_cache_festivos_nyse = {}


def festivos_nyse(year):
    if year in _cache_festivos_nyse:
        return _cache_festivos_nyse[year]
    festivos = {
        _fecha_observada_nyse(date(year, 1, 1)),               # Año Nuevo
        _n_esimo_dia_semana(year, 1, 0, 3),                     # MLK Day
        _n_esimo_dia_semana(year, 2, 0, 3),                     # Washington's Birthday
        _domingo_pascua(year) - timedelta(days=2),              # Good Friday
        _ultimo_dia_semana(year, 5, 0),                         # Memorial Day
        _fecha_observada_nyse(date(year, 7, 4)),                # Independence Day
        _n_esimo_dia_semana(year, 9, 0, 1),                     # Labor Day
        _n_esimo_dia_semana(year, 11, 3, 4),                    # Thanksgiving (jueves=3)
        _fecha_observada_nyse(date(year, 12, 25)),              # Navidad
    }
    if year >= 2022:
        festivos.add(_fecha_observada_nyse(date(year, 6, 19)))  # Juneteenth (festivo NYSE desde 2022)
    _cache_festivos_nyse[year] = festivos
    return festivos


def es_festivo_us(fecha):
    """True si `fecha` (date, no datetime) es festivo de NYSE/Nasdaq."""
    return fecha in festivos_nyse(fecha.year)


MINUTOS_SIN_COMPRAR_ANTES_CIERRE = 90    # ultimos 90 min antes del cierre: no comprar (salvo promediar a la baja)
MINUTOS_VENTA_FORZADA_ANTES_CIERRE = 15  # ultimos 15 min: vender lo que tenga entre +0.5% y +2% de beneficio
BENEFICIO_MAX_VENTA_FORZADA_PCT = 2.0
UMBRAL_BENEFICIO_PCT = 0.5
MARGEN_ORDEN_LIMITADA_VENTA_PCT = 0.2

# Criterio de venta: trailing stop + refuerzo de 2 velas + salida parcial
# (peticion del usuario, sept. 2026). Al principio solo se aplico a
# ACCIONES; luego se extendio a CRYPTO tambien (mismo mecanismo, cada
# mercado usa su propio umbral de entrada: UMBRAL_BENEFICIO_PCT para
# acciones, UMBRAL_BENEFICIO_CRYPTO_PCT para cripto). UMBRAL_BENEFICIO_PCT
# ya esta en NETO (sin comision, Alpaca no cobra en acciones), asi que el
# trailing stop arma sobre beneficio neto de verdad.
TRAILING_STOP_VENTA_PCT = 0.3  # puntos de retroceso desde el maximo neto alcanzado

# Bug real de produccion (sept. 2026): el maximo trackeado vivia SOLO en
# memoria (un dict normal). Cada reinicio del proceso (un despliegue, una
# caida, systemctl restart) lo borraba por completo, asi que el bot
# "olvidaba" que una posicion habia llegado a +7% y trackeaba desde el
# valor que tuviera justo al arrancar -un retroceso real desde un maximo
# anterior al reinicio nunca disparaba el trailing stop, porque el bot
# nunca "vio" ese maximo-. Ahora se persiste en disco
# (ARCHIVO_ESTADO_VENTA) y se recarga al arrancar, para sobrevivir a
# reinicios igual que el historial de operaciones.
ARCHIVO_ESTADO_VENTA = "estado_venta_alpaca.json"
_maximo_beneficio_neto_por_posicion = {}  # ticker -> % neto maximo visto en la posicion actual

# Salida parcial (peticion del usuario, sept. 2026): en cuanto una posicion
# alcanza por primera vez su umbral minimo, se vende PORCENTAJE_SCALE_OUT de
# la posicion para asegurar beneficio ya, dejando el resto corriendo con el
# trailing stop -mejora el beneficio medio por operacion sin cambiar el
# perfil de riesgo, en vez del viejo "todo o nada"-. Solo se hace UNA vez
# por posicion (_scale_out_realizado), no en cada ciclo tras la primera vez.
PORCENTAJE_SCALE_OUT = 0.5
_scale_out_realizado = set()  # tickers ya con su venta parcial hecha


def cargar_estado_venta():
    """Recupera _maximo_beneficio_neto_por_posicion/_scale_out_realizado
    guardados en disco (ver ARCHIVO_ESTADO_VENTA) - se llama una vez al
    arrancar el bot, para no perder el trailing stop en cada reinicio."""
    global _maximo_beneficio_neto_por_posicion, _scale_out_realizado
    try:
        with open(ARCHIVO_ESTADO_VENTA, "r", encoding="utf-8") as f:
            datos = json.load(f)
        _maximo_beneficio_neto_por_posicion = dict(datos.get("maximo_beneficio_neto_por_posicion", {}))
        _scale_out_realizado = set(datos.get("scale_out_realizado", []))
        if _maximo_beneficio_neto_por_posicion or _scale_out_realizado:
            log(f"Estado de venta (trailing stop) recuperado de {ARCHIVO_ESTADO_VENTA}: "
                f"{len(_maximo_beneficio_neto_por_posicion)} posicion(es) trackeada(s).")
    except (FileNotFoundError, json.JSONDecodeError):
        pass


def _guardar_estado_venta():
    try:
        with open(ARCHIVO_ESTADO_VENTA, "w", encoding="utf-8") as f:
            json.dump({
                "maximo_beneficio_neto_por_posicion": _maximo_beneficio_neto_por_posicion,
                "scale_out_realizado": sorted(_scale_out_realizado),
            }, f, indent=2, sort_keys=True)
    except OSError as e:
        log(f"No se pudo guardar el estado de venta ({ARCHIVO_ESTADO_VENTA}): {type(e).__name__}: {e}")


def decidir_accion_venta(clave, beneficio_pct, umbral):
    """Logica compartida de venta (trailing stop + salida parcial),
    identica a la de bot_completo.py -ver ahi la explicacion completa-.
    Devuelve (accion, motivo), accion en
    {"MANTENER","VENTA_PARCIAL","VENTA_TOTAL"}. NO decide por si sola el
    refuerzo de 2 velas -eso lo comprueba el llamador solo si beneficio_pct
    ya supera el umbral, para no gastar una peticion de datos de mas-."""
    maximo_anterior = _maximo_beneficio_neto_por_posicion.get(clave, beneficio_pct)
    maximo_neto = max(maximo_anterior, beneficio_pct)
    _maximo_beneficio_neto_por_posicion[clave] = maximo_neto

    trailing_armado = maximo_neto >= umbral
    retroceso_pct = maximo_neto - beneficio_pct
    disparo_trailing = trailing_armado and retroceso_pct >= TRAILING_STOP_VENTA_PCT
    info = f"(maximo alcanzado {maximo_neto:.2f}%, retroceso {retroceso_pct:.2f} pts)"

    if disparo_trailing:
        _guardar_estado_venta()
        return "VENTA_TOTAL", f"trailing stop: retrocedio {retroceso_pct:.2f} pts desde el maximo de {maximo_neto:.2f}%"
    if trailing_armado and clave not in _scale_out_realizado:
        _scale_out_realizado.add(clave)
        _guardar_estado_venta()
        return "VENTA_PARCIAL", f"objetivo alcanzado ({beneficio_pct:.2f}% >= {umbral}%): asegurando el {PORCENTAJE_SCALE_OUT*100:.0f}%"
    _guardar_estado_venta()
    return "MANTENER", info


def cerrar_seguimiento_venta(clave):
    """Olvida el maximo trackeado y la marca de salida parcial de una
    posicion (llamar tras confirmar una venta TOTAL)."""
    _maximo_beneficio_neto_por_posicion.pop(clave, None)
    _scale_out_realizado.discard(clave)
    _guardar_estado_venta()

INTERVALO_SEGUNDOS = 130  # 2 min 10 s (ajustado tras pruebas en paper, ago 2026) - solo acciones
CRYPTO_INTERVALO_SEGUNDOS = 60  # cripto revisa cada 1 minuto, en su propia cadencia (peticion
                                 # del usuario, sept. 2026): mismo valor que en bot_completo.py/IBKR
LIMITE_EXPOSICION_PCT = 15   # % maximo del total de cartera por valor
IMPORTE_EUROS = 45           # presupuesto maximo por operacion (convertido a USD) - ajustado
                              # para capital real de ~300 EUR (antes 1000, pensado para el
                              # saldo simulado de $100.000 de la cuenta paper)
TIPO_CAMBIO_EUR_USD = 1.14   # actualiza a mano si quieres mas precision

# Peticion explicita del usuario (sept. 2026): si el importe estandar
# (IMPORTE_EUROS) no cabe en el efectivo disponible, en vez de omitir la
# compra entera se reduce al maximo que quepa, dejando siempre este margen
# de seguridad en la cuenta (nunca se deja la cuenta a 0 exacto).
MARGEN_EFECTIVO_MINIMO_USD = 5.0

DECIMALES_FRACCION = 4
VALOR_MINIMO_OPERACION_FRACCIONARIA_USD = 1.0

# Tope de posiciones abiertas SIMULTANEAS (acciones) y comprobacion de caja
# disponible antes de comprar (peticion del usuario, sept. 2026): sin esto,
# un dia de tendencia fuerte con muchas señales a la vez podria intentar
# abrir muchas posiciones nuevas sin comprobar si hay efectivo de verdad, o
# sin ningun limite al numero de valores distintos en cartera a la vez. No
# sustituye a LIMITE_EXPOSICION_PCT (por VALOR de cada posicion): es un
# limite independiente, sobre el NUMERO de posiciones y la CAJA real.
MAX_POSICIONES_ABIERTAS = 12

# --- Lista de valores: misma seleccion de 30 tickers que en bot_completo.py ---
ACTIVOS = ["AAPL", "MSFT", "NVDA", "AMZN", "GOOGL", "META", "TSLA", "AVGO", "AMD", "NFLX",
           "INTC", "QCOM", "CSCO", "SMCI",
           "JPM", "BAC", "WFC", "C", "V", "MA",
           "XOM", "CVX",
           "WMT", "DIS", "KO", "JNJ", "PFE", "F", "T", "GE"]

# --- Cripto en Alpaca (añadido sept. 2026, petición del usuario) ---
# A diferencia de IBKR, Alpaca opera cripto con las MISMAS claves API que
# acciones (sin exchange que descubrir, sin tif especial que adivinar: los
# símbolos llevan "/" -p.ej. "BTC/USD"- y eso basta para distinguirlos de
# un ticker de accion, ver es_cripto()). Mismas 6 monedas que en
# bot_completo.py (IBKR); LINK y BCH confirmados en la lista de pares
# soportados por Alpaca, SOL NO aparecía en esa lista en el momento de
# escribir esto -si el bot la ve rechazada sistemáticamente, quitarla de
# aquí-.
ACTIVOS_CRYPTO = ["BTC/USD", "ETH/USD", "LTC/USD", "BCH/USD", "LINK/USD", "SOL/USD"]

# Comision REAL de Alpaca para cripto (a diferencia de acciones, que Alpaca
# no cobra comision propia): tarifa "taker" del primer tramo de volumen
# (0-100.000 USD en 30 dias, el unico realista para este capital), la que
# aplica una orden a mercado/IOC como las que usa este bot. NO es 0% como
# las acciones -ver ALPACA_NOTES.md para la tabla completa de tramos-.
COMISION_CRIPTO_ALPACA_PCT = 0.25 / 100

# Peticion del usuario (sept. 2026): limite propio de este bot al CONJUNTO
# de toda la cripto (no solo por moneda individual, que sigue usando el
# mismo LIMITE_EXPOSICION_PCT del 15% que las acciones), sobre el valor
# TOTAL de la cuenta (acciones + cash + cripto -portfolio_value de Alpaca
# ya las suma todas-).
LIMITE_EXPOSICION_CRYPTO_TOTAL_PCT = 20

UMBRAL_BENEFICIO_CRYPTO_PCT = 0.3  # cripto: umbral mas bajo que acciones (igual que en
                                    # bot_completo.py/IBKR) - 24/7 y mas rapida
VALOR_MINIMO_OPERACION_CRIPTO_USD = 5.0
DECIMALES_FRACCION_CRIPTO = 6  # mas precision que acciones: BTC/ETH suelen necesitarla


def es_cripto(ticker):
    """Los simbolos de cripto en Alpaca llevan barra ('BTC/USD'); los de
    acciones nunca la llevan -no hace falta nada mas para distinguirlos."""
    return "/" in ticker

# --- Temporalidades para el analisis MACD (identico a bot_completo.py) ---
# duration_dias: cuantos dias hacia atras pedir para tener suficientes velas
# (el analisis exige al menos 35 velas por temporalidad).
TEMPORALIDADES = [
    {"nombre": "1 minuto",   "timeframe": TimeFrame(1, TimeFrameUnit.Minute), "duration_dias": 3,    "tipo": "corta"},
    {"nombre": "5 minutos",  "timeframe": TimeFrame(5, TimeFrameUnit.Minute), "duration_dias": 5,    "tipo": "corta"},
    {"nombre": "15 minutos", "timeframe": TimeFrame(15, TimeFrameUnit.Minute), "duration_dias": 10,  "tipo": "corta"},
    {"nombre": "30 minutos", "timeframe": TimeFrame(30, TimeFrameUnit.Minute), "duration_dias": 20,  "tipo": "corta"},
    {"nombre": "1 hora",     "timeframe": TimeFrame(1, TimeFrameUnit.Hour),   "duration_dias": 60,   "tipo": "corta"},
    {"nombre": "1 dia",      "timeframe": TimeFrame(1, TimeFrameUnit.Day),    "duration_dias": 400,  "tipo": "larga"},
    {"nombre": "1 semana",   "timeframe": TimeFrame(1, TimeFrameUnit.Week),   "duration_dias": 5 * 365, "tipo": "larga"},
]
NOMBRES_4_CORTAS = ["1 minuto", "5 minutos", "15 minutos", "30 minutos"]


# --- Vigilante de congelacion del proceso (identico a bot_completo.py) ---
# Archivos con nombre DISTINTO al bot de IBKR, para poder correr los dos a
# la vez en la misma carpeta sin que se pisen el latido/PID entre ellos.
_ultimo_latido = time.monotonic()
_ultimo_latido_archivo = 0.0
UMBRAL_CONGELACION_SEGUNDOS = 20 * 60
ARCHIVO_LATIDO = "latido_bot_alpaca.txt"
ARCHIVO_PID = "bot_alpaca.pid"
INTERVALO_MIN_ESCRITURA_LATIDO_SEGUNDOS = 10


def actualizar_latido():
    global _ultimo_latido, _ultimo_latido_archivo
    ahora = time.monotonic()
    _ultimo_latido = ahora
    if ahora - _ultimo_latido_archivo >= INTERVALO_MIN_ESCRITURA_LATIDO_SEGUNDOS:
        _ultimo_latido_archivo = ahora
        try:
            with open(ARCHIVO_LATIDO, "w", encoding="utf-8") as f:
                f.write(str(time.time()))
        except OSError:
            pass


def escribir_pid():
    try:
        with open(ARCHIVO_PID, "w", encoding="utf-8") as f:
            f.write(str(os.getpid()))
    except OSError as e:
        log(f"No se pudo escribir el archivo de PID ({ARCHIVO_PID}): {type(e).__name__}: {e}")


def vigilante_congelacion():
    """Primera linea de defensa (hilo interno); para una proteccion real
    hace falta un vigilante EXTERNO -mismo mecanismo que vigilante_externo.ps1
    del bot de IBKR, apuntando a ARCHIVO_LATIDO/ARCHIVO_PID de este bot-."""
    while True:
        time.sleep(60)
        inactividad = time.monotonic() - _ultimo_latido
        if inactividad > UMBRAL_CONGELACION_SEGUNDOS:
            print(f"\n[VIGILANTE] El programa lleva {inactividad / 60:.0f} minutos sin dar "
                  f"ninguna señal de vida. Forzando el cierre del proceso. Si no tienes un "
                  f"supervisor externo que lo reinicie automaticamente, el bot se quedara "
                  f"parado hasta que lo reinicies tu a mano.", flush=True)
            notificar_telegram(f"🛑 El bot de Alpaca lleva {inactividad / 60:.0f} min sin dar señal de vida "
                               f"(congelado) y se ha forzado su cierre. Comprueba que el servicio "
                               f"bot-alpaca se reinicie solo (systemd Restart=always) o reinicialo a mano.")
            os._exit(1)


def log(mensaje):
    actualizar_latido()
    ahora = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{ahora}] {mensaje}")


# --- Horarios ---
def es_horario_operativo():
    """True si el mercado US esta en horario extendido (4:00-20:00 ET), de
    lunes a viernes y sin ser festivo de NYSE/Nasdaq (ver
    festivos_nyse()/es_festivo_us() mas arriba, peticion del usuario sept.
    2026)."""
    ahora = datetime.now(ZONA_NY)
    if ahora.weekday() >= 5 or es_festivo_us(ahora.date()):
        return False
    return HORA_INICIO_US <= ahora.time() < HORA_CIERRE_EXTENDIDO_US


def en_postmercado_us():
    ahora = datetime.now(ZONA_NY)
    if ahora.weekday() >= 5 or es_festivo_us(ahora.date()):
        return False
    return HORA_CIERRE_US <= ahora.time() < HORA_CIERRE_EXTENDIDO_US


def fuera_de_sesion_regular_us():
    ahora = datetime.now(ZONA_NY)
    if ahora.weekday() >= 5 or es_festivo_us(ahora.date()):
        return False
    hora = ahora.time()
    en_premercado = HORA_INICIO_US <= hora < HORA_APERTURA_REGULAR_US
    en_postmercado = HORA_CIERRE_US <= hora < HORA_CIERRE_EXTENDIDO_US
    return en_premercado or en_postmercado


def minutos_hasta_cierre():
    if not es_horario_operativo():
        return None
    ahora = datetime.now(ZONA_NY)
    cierre_hoy = ahora.replace(hour=HORA_CIERRE_US.hour, minute=HORA_CIERRE_US.minute,
                                second=0, microsecond=0)
    return (cierre_hoy - ahora).total_seconds() / 60


def en_ventana_sin_compra():
    minutos = minutos_hasta_cierre()
    if minutos is None:
        return False
    return 0 <= minutos <= MINUTOS_SIN_COMPRAR_ANTES_CIERRE


def en_ventana_venta_forzada():
    minutos = minutos_hasta_cierre()
    if minutos is None:
        return False
    return 0 <= minutos <= MINUTOS_VENTA_FORZADA_ANTES_CIERRE


# --- MACD (identico a bot_completo.py) ---
def calcular_macd(cierres, rapida=12, lenta=26, senal=9):
    ema_rapida = cierres.ewm(span=rapida, adjust=False).mean()
    ema_lenta = cierres.ewm(span=lenta, adjust=False).mean()
    macd = ema_rapida - ema_lenta
    linea_senal = macd.ewm(span=senal, adjust=False).mean()
    histograma = macd - linea_senal
    return macd, linea_senal, histograma


def decidir_senal(detalle):
    """Misma logica de decision que analizar_activo() en bot_completo.py,
    a partir de un dict {nombre_temporalidad: True/False/None}."""
    # Peticion del usuario (sept. 2026): el atajo de "4 cortas alcistas"
    # tambien respeta que ninguna larga (dia/semana) este en contra -antes
    # las ignoraba por completo, así que bastaba con las 4 cortas alcistas
    # para comprar aunque la tendencia diaria o semanal fuera claramente
    # bajista (la peor categoria de entrada). Bug real corregido: la regla
    # de "1 de 7 en contra" de mas abajo nunca llegaba a aplicarse en este
    # caso, porque el atajo la adelantaba siempre que las 4 cortas
    # estuvieran alcistas (lo mas habitual).
    largas_en_contra = any(detalle[tf["nombre"]] is False for tf in TEMPORALIDADES if tf["tipo"] == "larga")
    cuatro_cortas_alcistas = (
        all(detalle[n] is not None for n in NOMBRES_4_CORTAS)
        and all(detalle[n] for n in NOMBRES_4_CORTAS)
        and not largas_en_contra
    )
    faltan_datos = any(detalle[tf["nombre"]] is None for tf in TEMPORALIDADES)
    total_false = sum(1 for tf in TEMPORALIDADES if detalle[tf["nombre"]] is False)
    cortas_ok = all(detalle[tf["nombre"]] for tf in TEMPORALIDADES
                     if tf["tipo"] == "corta" and detalle[tf["nombre"]] is not None)
    largas_ok = all(detalle[tf["nombre"]] for tf in TEMPORALIDADES
                     if tf["tipo"] == "larga" and detalle[tf["nombre"]] is not None)
    # La UNICA temporalidad en contra (si hay exactamente 1) - usado para
    # distinguir si es una corta (retroceso normal, sigue siendo buena
    # entrada) o una larga (contra la tendencia dominante, mala entrada).
    tf_en_contra = next((tf for tf in TEMPORALIDADES if detalle[tf["nombre"]] is False), None)

    if cuatro_cortas_alcistas:
        return "COMPRA"
    if faltan_datos:
        return "SIN_DATOS"
    if total_false == 0:
        return "COMPRA"
    if total_false == 1:
        # Peticion del usuario (sept. 2026): la excepcion de "1 de 7 en
        # contra" solo vale si la discrepancia es en una temporalidad CORTA
        # (1min-1h) -un retroceso normal, a menudo mejor precio de entrada-.
        # Si la unica en contra es la diaria o semanal, NO se compra: seria
        # entrar contra la tendencia dominante, la peor categoria de
        # entrada (antes esto se trataba igual que un "1 minuto en contra",
        # bug real corregido aqui).
        if tf_en_contra is not None and tf_en_contra["tipo"] == "corta":
            return "COMPRA"
        return "BLOQUEADO_TF_LARGA"
    if cortas_ok and not largas_ok:
        return "BLOQUEADO"
    return "SIN_SENAL"


def macd_alcista_o_bajista(velas, tipo, modo="cerrada"):
    """Devuelve True/False/None (None si faltan datos) para UNA temporalidad,
    a partir de la lista de barras de Alpaca ya ordenada cronologicamente.
    Para temporalidades 'larga', `modo` decide si se usa la vela EN CURSO
    (dia/semana todavia sin cerrar) o solo barras ya cerradas -ver
    _resultados_largas_cacheados()-."""
    if len(velas) < 35:
        return None
    cierres = pd.Series([float(v.close) for v in velas])
    macd, linea_senal, histograma = calcular_macd(cierres)
    if tipo == "corta":
        return bool(macd.iloc[-1] > linea_senal.iloc[-1])
    if modo == "en_curso":
        # Ya ha pasado suficiente del periodo (ver UMBRAL_FRACCION_VELA_EN_CURSO)
        # como para que la vela en formacion aporte señal real -se incluye.
        return bool(histograma.iloc[-1] > histograma.iloc[-3])
    # modo "cerrada" (por defecto): solo barras YA CERRADAS. La ULTIMA vela
    # de la serie (el dia/semana en curso) sigue formandose en tiempo real
    # -un lunes por la mañana esa barra puede tener minutos de datos- y
    # mezclarla con barras cerradas es ruido, ademas de impedir cachear el
    # resultado con seguridad (cambiaria en cada ciclo). Se compara la
    # penultima vela cerrada contra la de 2 barras atras (iloc[-2] vs
    # iloc[-4]), nunca la ultima (iloc[-1], en formacion).
    return bool(histograma.iloc[-2] > histograma.iloc[-4])


# --- Datos de mercado: UNA peticion por temporalidad para TODOS los
# tickers a la vez (en vez de una por ticker x temporalidad como en IBKR).
# Alpaca permite pedir varios simbolos en la misma llamada, y el limite de
# la API de datos es de 200 peticiones/minuto -con 30 tickers x 7
# temporalidades por separado (210 peticiones) nos arriesgariamos a superarlo
# cada ciclo; agrupando por temporalidad son solo 7 peticiones por ciclo-.
INTENTOS_MAXIMOS_DATOS = 3
ESPERA_ENTRE_INTENTOS_DATOS_SEGUNDOS = 15

_data_client = StockHistoricalDataClient(ALPACA_API_KEY, ALPACA_SECRET_KEY)
_forzar_timeout_por_defecto(_data_client)


def pedir_velas_lote(tickers, timeframe, duration_dias):
    """Pide barras historicas de una temporalidad para VARIOS tickers a la
    vez, con reintentos igual que pedir_velas() en bot_completo.py. Devuelve
    un dict {ticker: [velas]} (lista vacia si ese ticker no tuvo datos)."""
    inicio = datetime.now(ZONA_NY) - timedelta(days=duration_dias)
    peticion = StockBarsRequest(symbol_or_symbols=tickers, timeframe=timeframe, start=inicio)

    for intento in range(1, INTENTOS_MAXIMOS_DATOS + 1):
        try:
            barset = _data_client.get_stock_bars(peticion)
            resultado = {t: list(barset.data.get(t, [])) for t in tickers}
            if any(resultado.values()):
                return resultado
        except Exception as e:
            log(f"ERROR al pedir velas en lote (intento {intento}/{INTENTOS_MAXIMOS_DATOS}): "
                f"{type(e).__name__}: {e}")
        if intento < INTENTOS_MAXIMOS_DATOS:
            log(f"Sin datos en el intento {intento}/{INTENTOS_MAXIMOS_DATOS}, "
                f"reintentando en {ESPERA_ENTRE_INTENTOS_DATOS_SEGUNDOS}s...")
            time.sleep(ESPERA_ENTRE_INTENTOS_DATOS_SEGUNDOS)
        else:
            log(f"Sin datos tras el ultimo intento ({intento}/{INTENTOS_MAXIMOS_DATOS}), "
                f"se sigue con lo que haya (posiblemente vacio para algunos tickers).")

    return {t: [] for t in tickers}


# Cache de las temporalidades LARGAS (dia/semana): peticion del usuario,
# sept. 2026. Con un horizonte de trading de HORAS, dia/semana se usan como
# filtro de fondo (evitar comprar contra la tendencia dominante), no como
# señal de entrada -para eso ya estan las cortas (1min-1h), que se piden en
# cada ciclo sin cache-. Como esas velas solo cambian de verdad una vez al
# dia/una vez a la semana (cuando CIERRA la barra), pedirlas de nuevo cada
# pocos minutos no aporta nada.
#
# Matiz (peticion del usuario): la vela de HOY/ESTA SEMANA (en formacion) SI
# se tiene en cuenta una vez que lleva al menos UMBRAL_FRACCION_VELA_EN_CURSO
# de su periodo transcurrido -antes de eso, es ruido puro-. Por debajo del
# umbral se cachea una unica vez al dia (modo "cerrada", no puede cambiar).
# Por encima, se incluye la vela en curso (modo "en_curso"), refrescando
# cada INTERVALO_REFRESCO_VELA_EN_CURSO_SEGUNDOS en vez de en cada ciclo -
# sigue ahorrando peticiones sin quedarse con un dato obsoleto-. Acciones y
# cripto COMPARTEN este cache sin colision de claves entre tf (los simbolos
# de cripto llevan "/" y los de acciones no, pero aqui se cachea por tf, no
# por ticker suelto, asi que ni hace falta esa distincion).
UMBRAL_FRACCION_VELA_EN_CURSO = 0.4
INTERVALO_REFRESCO_VELA_EN_CURSO_SEGUNDOS = 60 * 60  # 1 hora

VENTANA_DIA_POR_MERCADO = {
    "US": (ZONA_NY, HORA_INICIO_US, HORA_CIERRE_EXTENDIDO_US),
}


def _fraccion_transcurrida_del_dia(mercado):
    """Fraccion (0.0-1.0) de la sesion de HOY ya transcurrida. CRYPTO no
    tiene sesion (opera 24/7): se usa el dia de calendario UTC completo."""
    if mercado == "CRYPTO":
        ahora_utc = datetime.now(timezone.utc)
        return (ahora_utc.hour * 3600 + ahora_utc.minute * 60 + ahora_utc.second) / 86400

    zona, inicio, cierre = VENTANA_DIA_POR_MERCADO.get(mercado, (ZONA_NY, HORA_INICIO_US, HORA_CIERRE_EXTENDIDO_US))
    ahora = datetime.now(zona)
    duracion_seg = (datetime.combine(ahora.date(), cierre) - datetime.combine(ahora.date(), inicio)).total_seconds()
    if duracion_seg <= 0:
        return 1.0
    transcurrido_seg = (datetime.combine(ahora.date(), ahora.time()) - datetime.combine(ahora.date(), inicio)).total_seconds()
    return max(0.0, min(1.0, transcurrido_seg / duracion_seg))


def _fraccion_transcurrida_de_la_semana(mercado):
    """Igual que _fraccion_transcurrida_del_dia pero para la semana
    (lunes-viernes en acciones, semana de calendario completa en cripto)."""
    if mercado == "CRYPTO":
        ahora = datetime.now(timezone.utc)
        dias_transcurridos = ahora.weekday() + (ahora.hour * 3600 + ahora.minute * 60 + ahora.second) / 86400
        return dias_transcurridos / 7

    if datetime.now(ZONA_NY).weekday() > 4:  # sabado/domingo: semana ya cerrada a estos efectos
        return 1.0
    dias_completos = datetime.now(ZONA_NY).weekday()  # 0 lunes, ..., 4 viernes
    return (dias_completos + _fraccion_transcurrida_del_dia(mercado)) / 5


_cache_largas_por_dia = {}  # {"<tf nombre>": {"fecha", "modo", "ultima_actualizacion", "valores": {ticker: bool|None}}}


def _resultados_largas_cacheados(tickers, tf, pedir_fn, mercado):
    hoy = datetime.now(ZONA_NY).date()
    fraccion = (_fraccion_transcurrida_del_dia(mercado) if tf["nombre"] == "1 dia"
                else _fraccion_transcurrida_de_la_semana(mercado))
    modo = "en_curso" if fraccion >= UMBRAL_FRACCION_VELA_EN_CURSO else "cerrada"

    cache_tf = _cache_largas_por_dia.get(tf["nombre"])
    valido = (
        cache_tf is not None and cache_tf["fecha"] == hoy and cache_tf["modo"] == modo
        and all(t in cache_tf["valores"] for t in tickers)
        and (modo == "cerrada"
             or time.monotonic() - cache_tf["ultima_actualizacion"] < INTERVALO_REFRESCO_VELA_EN_CURSO_SEGUNDOS)
    )
    if valido:
        return {t: cache_tf["valores"][t] for t in tickers}

    velas_por_ticker = pedir_fn(tickers, tf["timeframe"], tf["duration_dias"])
    valores = {t: macd_alcista_o_bajista(velas_por_ticker.get(t, []), tf["tipo"], modo) for t in tickers}
    _cache_largas_por_dia[tf["nombre"]] = {
        "fecha": hoy, "modo": modo, "ultima_actualizacion": time.monotonic(), "valores": valores,
    }
    return valores


def analizar_todos_los_activos(tickers):
    """Pide las temporalidades CORTAS para TODOS los tickers en lote (una
    peticion por temporalidad); las LARGAS (dia/semana) se sirven del cache
    diario en vez de pedirse en cada ciclo (ver _resultados_largas_cacheados).
    Devuelve {ticker: decision} + {ticker: precio_actual (ultimo cierre de
    1 minuto)}."""
    velas_por_temporalidad = {}
    resultados_largas = {}
    for tf in TEMPORALIDADES:
        if tf["tipo"] == "larga":
            resultados_largas[tf["nombre"]] = _resultados_largas_cacheados(tickers, tf, pedir_velas_lote, "US")
            continue
        velas_por_temporalidad[tf["nombre"]] = pedir_velas_lote(tickers, tf["timeframe"], tf["duration_dias"])

    decisiones = {}
    precios = {}
    for ticker in tickers:
        detalle = {}
        for tf in TEMPORALIDADES:
            if tf["tipo"] == "larga":
                detalle[tf["nombre"]] = resultados_largas[tf["nombre"]].get(ticker)
                continue
            velas = velas_por_temporalidad[tf["nombre"]].get(ticker, [])
            detalle[tf["nombre"]] = macd_alcista_o_bajista(velas, tf["tipo"])
        decisiones[ticker] = decidir_senal(detalle)

        velas_1min = velas_por_temporalidad["1 minuto"].get(ticker, [])
        precios[ticker] = float(velas_1min[-1].close) if velas_1min else None

    return decisiones, precios


def precio_actual_ticker(ticker):
    """Precio actual de UN solo ticker (usado para decisiones de venta,
    fuera del analisis en lote de compras)."""
    resultado = pedir_velas_lote([ticker], TEMPORALIDADES[0]["timeframe"], TEMPORALIDADES[0]["duration_dias"])
    velas = resultado.get(ticker, [])
    return float(velas[-1].close) if velas else None


# --- Cripto: mismas funciones de arriba, pero con CryptoHistoricalDataClient/
# CryptoBarsRequest en vez de StockHistoricalDataClient/StockBarsRequest (los
# simbolos de cripto no se pueden pedir con el cliente de acciones). El resto
# de la logica de analisis (MACD, decidir_senal) es la misma, se reutiliza
# tal cual -no hace falta duplicarla, solo la parte de "de donde vienen las
# velas"-.
_crypto_data_client = CryptoHistoricalDataClient(ALPACA_API_KEY, ALPACA_SECRET_KEY)
_forzar_timeout_por_defecto(_crypto_data_client)


def pedir_velas_lote_cripto(tickers, timeframe, duration_dias):
    """Version de pedir_velas_lote() para cripto."""
    inicio = datetime.now(ZONA_NY) - timedelta(days=duration_dias)
    peticion = CryptoBarsRequest(symbol_or_symbols=tickers, timeframe=timeframe, start=inicio)

    for intento in range(1, INTENTOS_MAXIMOS_DATOS + 1):
        try:
            barset = _crypto_data_client.get_crypto_bars(peticion)
            resultado = {t: list(barset.data.get(t, [])) for t in tickers}
            if any(resultado.values()):
                return resultado
        except Exception as e:
            log(f"ERROR al pedir velas de cripto en lote (intento {intento}/{INTENTOS_MAXIMOS_DATOS}): "
                f"{type(e).__name__}: {e}")
        if intento < INTENTOS_MAXIMOS_DATOS:
            log(f"CRIPTO: sin datos en el intento {intento}/{INTENTOS_MAXIMOS_DATOS}, "
                f"reintentando en {ESPERA_ENTRE_INTENTOS_DATOS_SEGUNDOS}s...")
            time.sleep(ESPERA_ENTRE_INTENTOS_DATOS_SEGUNDOS)
        else:
            log(f"CRIPTO: sin datos tras el ultimo intento ({intento}/{INTENTOS_MAXIMOS_DATOS}), "
                f"se sigue con lo que haya (posiblemente vacio para algunas monedas).")

    return {t: [] for t in tickers}


def analizar_todos_los_activos_cripto(tickers):
    """Version de analizar_todos_los_activos() para cripto: las LARGAS
    (dia/semana) tambien usan el cache diario compartido (ver
    _resultados_largas_cacheados) - cripto no tiene "cierre" de mercado,
    pero tampoco tiene sentido recalcular la tendencia semanal en cada
    ciclo de 1 minuto."""
    velas_por_temporalidad = {}
    resultados_largas = {}
    for tf in TEMPORALIDADES:
        if tf["tipo"] == "larga":
            resultados_largas[tf["nombre"]] = _resultados_largas_cacheados(tickers, tf, pedir_velas_lote_cripto, "CRYPTO")
            continue
        velas_por_temporalidad[tf["nombre"]] = pedir_velas_lote_cripto(tickers, tf["timeframe"], tf["duration_dias"])

    decisiones = {}
    precios = {}
    for ticker in tickers:
        detalle = {}
        for tf in TEMPORALIDADES:
            if tf["tipo"] == "larga":
                detalle[tf["nombre"]] = resultados_largas[tf["nombre"]].get(ticker)
                continue
            velas = velas_por_temporalidad[tf["nombre"]].get(ticker, [])
            detalle[tf["nombre"]] = macd_alcista_o_bajista(velas, tf["tipo"])
        decisiones[ticker] = decidir_senal(detalle)

        velas_1min = velas_por_temporalidad["1 minuto"].get(ticker, [])
        precios[ticker] = float(velas_1min[-1].close) if velas_1min else None

    return decisiones, precios


def precio_actual_ticker_cripto(ticker):
    """Version de precio_actual_ticker() para cripto."""
    resultado = pedir_velas_lote_cripto([ticker], TEMPORALIDADES[0]["timeframe"], TEMPORALIDADES[0]["duration_dias"])
    velas = resultado.get(ticker, [])
    return float(velas[-1].close) if velas else None


def macd_5min_bajista_cripto(ticker):
    resultado = pedir_velas_lote_cripto([ticker], TimeFrame(5, TimeFrameUnit.Minute), 5)
    velas = resultado.get(ticker, [])
    if len(velas) < 35:
        return None
    cierres = pd.Series([float(v.close) for v in velas])
    macd, linea_senal, _ = calcular_macd(cierres)
    return bool(macd.iloc[-1] < linea_senal.iloc[-1])


def macd_5min_bajista_cripto_2_velas(ticker):
    """Version 'reforzada' de macd_5min_bajista_cripto(): exige que las DOS
    ultimas velas de 5 min (no solo la ultima) tengan MACD por debajo de su
    linea de señal - ver macd_5min_bajista_2_velas() (acciones) y
    TRAILING_STOP_VENTA_PCT/UMBRAL_BENEFICIO_CRYPTO_PCT mas abajo (peticion
    del usuario, sept. 2026: extender el trailing stop + refuerzo a cripto)."""
    resultado = pedir_velas_lote_cripto([ticker], TimeFrame(5, TimeFrameUnit.Minute), 5)
    velas = resultado.get(ticker, [])
    if len(velas) < 36:
        return None
    cierres = pd.Series([float(v.close) for v in velas])
    macd, linea_senal, _ = calcular_macd(cierres)
    return bool(macd.iloc[-1] < linea_senal.iloc[-1] and macd.iloc[-2] < linea_senal.iloc[-2])


def estimar_comision_cripto_alpaca(valor_operacion):
    """Comision REAL de Alpaca para cripto (a diferencia de acciones, sin
    comision). Ver COMISION_CRIPTO_ALPACA_PCT."""
    if valor_operacion <= 0:
        return 0.0
    return valor_operacion * COMISION_CRIPTO_ALPACA_PCT


def calcular_exposicion_total_cripto_usd(posiciones):
    """Suma el valor de mercado (market_value) de TODAS las posiciones de
    cripto abiertas -para el limite propio LIMITE_EXPOSICION_CRYPTO_TOTAL_PCT,
    ver revisar_compras_cripto()."""
    return sum(float(p.market_value) for p in posiciones if es_cripto(p.symbol))


# --- Comisiones: se asume 0 EUR en compras Y en ventas (a peticion expresa
# del usuario). NOTA: en la realidad Alpaca sigue repercutiendo tasas
# regulatorias minimas de SEC/FINRA en ventas (unos pocos centimos por cada
# operacion, del orden de 0.0023% + 0.000119 USD/accion) que no son
# comision de Alpaca sino de la SEC/FINRA -pero son tan pequeñas para el
# tamaño de esta cartera que se ignoran a proposito para simplificar el
# calculo de beneficio neto-.


# --- Cliente de trading ---
_trading_client = TradingClient(ALPACA_API_KEY, ALPACA_SECRET_KEY, paper=ALPACA_PAPER)
_forzar_timeout_por_defecto(_trading_client)

ESPERA_MAXIMA_ESTADO_ORDEN_SEGUNDOS = 10
INTERVALO_CHEQUEO_ESTADO_ORDEN_SEGUNDOS = 0.5
ESTADOS_FINALES_ORDEN = {"filled", "canceled", "rejected", "expired", "done_for_day"}


def esperar_estado_final_orden(order_id, espera_maxima=ESPERA_MAXIMA_ESTADO_ORDEN_SEGUNDOS):
    """Espera a que la orden llegue a un estado final, igual que
    esperar_estado_final_orden() en bot_completo.py (evita fiarse de un
    estado transitorio leido demasiado pronto)."""
    transcurrido = 0.0
    estado = "unknown"
    while transcurrido < espera_maxima:
        try:
            orden = _trading_client.get_order_by_id(order_id)
            estado = str(orden.status.value if hasattr(orden.status, "value") else orden.status).lower()
        except Exception as e:
            log(f"ERROR al consultar el estado de la orden {order_id}: {type(e).__name__}: {e}")
            return "unknown"
        if estado in ESTADOS_FINALES_ORDEN:
            return estado
        time.sleep(INTERVALO_CHEQUEO_ESTADO_ORDEN_SEGUNDOS)
        transcurrido += INTERVALO_CHEQUEO_ESTADO_ORDEN_SEGUNDOS
    return estado


def cancelar_ordenes_abiertas(ticker):
    """Cancela cualquier orden todavia ABIERTA (no rellenada) de un ticker,
    antes de mandar una orden nueva. Bug real visto en produccion (agosto
    2026, META en premercado): una orden limitada anterior que se quedo sin
    rellenar (el precio se alejo del limite) se queda "viva" en Alpaca
    reteniendo TODA la cantidad de la posicion (held_for_orders); el
    siguiente intento de venta, aunque la posicion siga apareciendo con
    cantidad > 0, es rechazado por la API con "insufficient qty available
    for order (... available: 0)". Cancelando cualquier orden abierta antes
    de operar, la cantidad vuelve a estar disponible para la orden nueva."""
    try:
        ordenes_abiertas = _trading_client.get_orders(
            filter=GetOrdersRequest(status=QueryOrderStatus.OPEN, symbols=[ticker]))
    except Exception as e:
        log(f"{ticker} - no se pudieron consultar ordenes abiertas antes de operar: {type(e).__name__}: {e}")
        return
    for orden in ordenes_abiertas:
        try:
            _trading_client.cancel_order_by_id(orden.id)
            # Alpaca procesa la cancelacion de forma ASINCRONA (la orden pasa
            # primero por "pending_cancel"): si se manda la orden nueva justo
            # despues sin esperar, la cantidad puede seguir figurando como
            # retenida (held_for_orders) por la orden vieja todavia no
            # liberada del todo, y se repite el mismo error "insufficient
            # qty available" aunque el codigo SI intente cancelarla primero
            # (bug real visto en produccion: recurrio varios dias despues de
            # añadir la cancelacion, por esta condicion de carrera). Se
            # espera aqui a que la cancelacion llegue a un estado final
            # antes de continuar.
            estado_cancelacion = esperar_estado_final_orden(orden.id)
            log(f"{ticker} - orden abierta anterior ({orden.id}) cancelada antes de mandar una nueva "
                f"(estado final: {estado_cancelacion}).")
        except Exception as e:
            log(f"{ticker} - no se pudo cancelar la orden abierta {orden.id}: {type(e).__name__}: {e}")


# --- Historial persistente de operaciones ejecutadas (compras y ventas) ---
# La API de Alpaca no da directamente el beneficio realizado de cada venta
# (get_orders() devuelve la orden, pero no el coste medio de compra en el
# momento de vender), asi que se registra aqui mismo, justo cuando ya se
# conoce ese dato, para poder consultar despues el estado de cartera y las
# operaciones cerradas de cualquier rango de fechas con cartera_alpaca.py.
# Solo registro, no participa en ninguna decision de trading.
ARCHIVO_HISTORIAL_OPERACIONES = "historial_operaciones_alpaca.json"


def cargar_historial_operaciones():
    try:
        with open(ARCHIVO_HISTORIAL_OPERACIONES, "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return []


def registrar_operacion_historial(ticker, lado, cantidad, precio, coste_medio=None, beneficio_pct=None):
    registro = {
        "fecha_hora": datetime.now().isoformat(timespec="seconds"),
        "ticker": ticker,
        "lado": lado,  # "COMPRA" o "VENTA"
        "cantidad": cantidad,
        "precio": precio,
        # Modo de la cuenta en el momento de la operacion (peticion del
        # usuario, sept. 2026: poder distinguir desde Telegram que
        # operaciones fueron con dinero real y cuales de cuando el bot
        # corria en paper -el historial se acumula entre cambios de modo,
        # sin este campo no habria forma de saberlo a posteriori-).
        "modo": "PAPER" if ALPACA_PAPER else "REAL",
    }
    if coste_medio is not None:
        registro["coste_medio"] = coste_medio
    if beneficio_pct is not None:
        registro["beneficio_pct"] = beneficio_pct
    try:
        operaciones = cargar_historial_operaciones()
        operaciones.append(registro)
        with open(ARCHIVO_HISTORIAL_OPERACIONES, "w", encoding="utf-8") as f:
            json.dump(operaciones, f, indent=2, sort_keys=True)
    except OSError as e:
        log(f"No se pudo guardar el historial de operaciones ({ARCHIVO_HISTORIAL_OPERACIONES}): {type(e).__name__}: {e}")


def obtener_ejecucion_real(order_id, cantidad_prevista, precio_previsto):
    """Tras confirmar que una orden ha quedado 'filled', intenta leer la
    cantidad y precio REALES de ejecucion (filled_qty/filled_avg_price). Si
    no estan disponibles por cualquier motivo, cae en los valores previstos
    -son solo para el registro historico, no afectan a ninguna decision-."""
    try:
        orden = _trading_client.get_order_by_id(order_id)
        cantidad_real = float(orden.filled_qty) if orden.filled_qty else cantidad_prevista
        precio_real = float(orden.filled_avg_price) if orden.filled_avg_price else precio_previsto
        return cantidad_real, precio_real
    except Exception:
        return cantidad_prevista, precio_previsto


def es_cantidad_fraccionaria(cantidad):
    return abs(cantidad - round(cantidad)) > 1e-6


def calcular_precio_limite_venta(precio_actual):
    return round(precio_actual * (1 - MARGEN_ORDEN_LIMITADA_VENTA_PCT / 100), 2)


def obtener_posiciones(client=None):
    """Devuelve la lista de posiciones abiertas (solo largas: este bot
    nunca abre cortos). Acepta un `client` distinto del que usa el bot para
    operar (por defecto `_trading_client`, la cuenta REAL/PAPER activa) -
    usado por /carterapaper de telegram_bot.py para consultar la cuenta
    PAPER incluso cuando el bot esta operando en REAL (peticion del
    usuario, sept. 2026)."""
    cliente = client or _trading_client
    try:
        return [p for p in cliente.get_all_positions() if float(p.qty) > 0]
    except Exception as e:
        log(f"ERROR al obtener las posiciones abiertas: {type(e).__name__}: {e}")
        return []


def obtener_valor_total_cartera_usd():
    try:
        cuenta = _trading_client.get_account()
        return float(cuenta.portfolio_value)
    except Exception as e:
        log(f"ERROR al obtener el valor total de la cartera: {type(e).__name__}: {e}")
        return None


def obtener_efectivo_disponible_usd():
    """Devuelve el efectivo REALMENTE disponible para nuevas compras (no el
    valor total de la cartera), en USD. Se usa para no intentar comprar mas
    de lo que la cuenta puede permitirse de verdad, ademas de los limites
    por % ya existentes (peticion del usuario, sept. 2026)."""
    try:
        cuenta = _trading_client.get_account()
        return float(cuenta.cash)
    except Exception as e:
        log(f"ERROR al obtener el efectivo disponible: {type(e).__name__}: {e}")
        return None


def obtener_valor_posicion_actual_usd(posiciones, ticker):
    for p in posiciones:
        if p.symbol == ticker and float(p.qty) > 0:
            return float(p.market_value)
    return 0.0


# --- Ventas ---
def revisar_ventas():
    # Las posiciones de cripto las gestiona revisar_ventas_cripto() aparte
    # (24/7, sin el chequeo de es_horario_operativo() de mas abajo, que es
    # especifico del mercado de acciones de US).
    posiciones = [p for p in obtener_posiciones() if not es_cripto(p.symbol)]
    if not posiciones:
        log("VENTAS: no hay posiciones de acciones abiertas.")
        return

    if not es_horario_operativo():
        log("VENTAS: fuera de horario operativo (4:00-20:00 ET), no se intenta vender nada este ciclo.")
        return

    # Poda del maximo de beneficio neto y de la marca de salida parcial
    # trackeados por posicion (ver TRAILING_STOP_VENTA_PCT/PORCENTAJE_SCALE_OUT
    # mas arriba): si un ticker de ACCION ya no esta entre las posiciones
    # abiertas (se vendio del todo), se olvida -si se vuelve a comprar mas
    # adelante, el trailing empieza de cero-. Los tickers de CRIPTO (con
    # "/") se ignoran aqui aposta -su propia poda vive en
    # revisar_ventas_cripto()-, ya que ambas funciones comparten el mismo
    # dict/set y esta corre en un ciclo distinto (130s vs 60s de cripto).
    tickers_vivos = {p.symbol for p in posiciones}
    for ticker_viejo in list(_maximo_beneficio_neto_por_posicion.keys()):
        if not es_cripto(ticker_viejo) and ticker_viejo not in tickers_vivos:
            cerrar_seguimiento_venta(ticker_viejo)

    log(f"\n########## VENTAS ##########")
    for pos in sorted(posiciones, key=lambda p: p.symbol):
        ticker = pos.symbol
        try:
            cantidad = float(pos.qty)
            coste_medio = float(pos.avg_entry_price)
            if cantidad <= 0 or coste_medio <= 0:
                continue

            precio_actual = precio_actual_ticker(ticker)
            if precio_actual is None:
                log(f"VENTAS: {ticker} - no se pudo obtener precio actual, se omite.")
                continue

            # Sin comision (ni de Alpaca ni de terceros, a peticion del usuario).
            beneficio_pct = (precio_actual - coste_medio) / coste_medio * 100

            info_posicion = f"{cantidad:g} acciones, precio medio {coste_medio:.4f} USD"

            fuera_sesion = fuera_de_sesion_regular_us()

            if en_ventana_venta_forzada() and UMBRAL_BENEFICIO_PCT <= beneficio_pct <= BENEFICIO_MAX_VENTA_FORZADA_PCT:
                log(f"VENTAS: {ticker} - {info_posicion} - beneficio {beneficio_pct:.2f}%, "
                    f"dentro de los ultimos {MINUTOS_VENTA_FORZADA_ANTES_CIERRE} min antes del "
                    f"cierre -> VENTA FORZADA (orden a mercado).")
                # A mercado, no limitada (peticion del usuario, sept. 2026):
                # el objetivo de la venta forzada es GARANTIZAR la salida
                # antes del cierre, y una orden limitada corria el riesgo de
                # no ejecutarse a tiempo si el precio se alejaba del limite.
                cancelar_ordenes_abiertas(ticker)
                orden = MarketOrderRequest(symbol=ticker, qty=cantidad, side=OrderSide.SELL,
                                            time_in_force=TimeInForce.DAY)
                trade = _trading_client.submit_order(order_data=orden)
                estado = esperar_estado_final_orden(trade.id)
                log(f"VENTAS: {ticker} - orden a mercado, estado: {estado}")
                if estado == "filled":
                    cantidad_real, precio_real = obtener_ejecucion_real(trade.id, cantidad, precio_actual)
                    registrar_operacion_historial(ticker, "VENTA", cantidad_real, precio_real,
                                                   coste_medio=coste_medio, beneficio_pct=beneficio_pct)
                    notificar_telegram(f"🔴 VENTA FORZADA <b>{ticker}</b>: {formato_es(cantidad_real, 4)} acciones a "
                                        f"{formato_es(precio_real)} USD (total {formato_es(cantidad_real * precio_real)} USD, "
                                        f"beneficio {formato_es(beneficio_pct, signo=True)}%)")
                    cerrar_seguimiento_venta(ticker)
                continue

            # Criterio de venta: trailing stop (principal) + 2 velas de 5min
            # bajistas seguidas (refuerzo) + salida parcial al alcanzar el
            # objetivo (peticion del usuario, sept. 2026) -ver
            # decidir_accion_venta()/PORCENTAJE_SCALE_OUT mas arriba-.
            accion, motivo = decidir_accion_venta(ticker, beneficio_pct, UMBRAL_BENEFICIO_PCT)

            if accion == "MANTENER" and beneficio_pct >= UMBRAL_BENEFICIO_PCT:
                if macd_5min_bajista_2_velas(ticker):
                    accion, motivo = "VENTA_TOTAL", "2 velas de 5min bajistas seguidas"

            if accion == "MANTENER":
                log(f"VENTAS: {ticker} - {info_posicion} - beneficio {beneficio_pct:.2f}% {motivo} -> se mantiene.")
                continue

            cantidad_a_vender = cantidad
            if accion == "VENTA_PARCIAL":
                parcial = round(cantidad * PORCENTAJE_SCALE_OUT, DECIMALES_FRACCION)
                if parcial <= 0 or parcial >= cantidad:
                    accion, cantidad_a_vender = "VENTA_TOTAL", cantidad
                else:
                    cantidad_a_vender = parcial

            etiqueta_accion = "VENTA PARCIAL" if accion == "VENTA_PARCIAL" else "VENTA"

            if fuera_sesion:
                precio_limite = precio_actual
                log(f"VENTAS: {ticker} - {info_posicion} - beneficio {beneficio_pct:.2f}%, "
                    f"{motivo} -> {etiqueta_accion} de {cantidad_a_vender:g} (orden limitada al precio exacto, "
                    f"fuera de sesion regular).")
                orden = LimitOrderRequest(symbol=ticker, qty=cantidad_a_vender, limit_price=precio_limite,
                                           side=OrderSide.SELL, time_in_force=TimeInForce.DAY,
                                           extended_hours=True)
            else:
                log(f"VENTAS: {ticker} - {info_posicion} - beneficio {beneficio_pct:.2f}%, {motivo} -> "
                    f"{etiqueta_accion} de {cantidad_a_vender:g} (orden a mercado).")
                orden = MarketOrderRequest(symbol=ticker, qty=cantidad_a_vender, side=OrderSide.SELL,
                                            time_in_force=TimeInForce.DAY)

            cancelar_ordenes_abiertas(ticker)
            trade = _trading_client.submit_order(order_data=orden)
            estado = esperar_estado_final_orden(trade.id)
            log(f"VENTAS: {ticker} - orden colocada, estado: {estado}")
            if estado == "filled":
                cantidad_real, precio_real = obtener_ejecucion_real(trade.id, cantidad_a_vender, precio_actual)
                registrar_operacion_historial(ticker, "VENTA", cantidad_real, precio_real,
                                               coste_medio=coste_medio, beneficio_pct=beneficio_pct)
                notificar_telegram(f"🔴 {etiqueta_accion} <b>{ticker}</b>: {formato_es(cantidad_real, 4)} acciones a "
                                    f"{formato_es(precio_real)} USD (total {formato_es(cantidad_real * precio_real)} USD, "
                                    f"beneficio {formato_es(beneficio_pct, signo=True)}%)")
                if accion == "VENTA_TOTAL":
                    cerrar_seguimiento_venta(ticker)
        except Exception as e:
            log(f"VENTAS: {ticker} - ERROR inesperado al procesar la posicion: {type(e).__name__}: {e}. Se omite.")


def macd_5min_bajista(ticker):
    resultado = pedir_velas_lote([ticker], TimeFrame(5, TimeFrameUnit.Minute), 5)
    velas = resultado.get(ticker, [])
    if len(velas) < 35:
        return None
    cierres = pd.Series([float(v.close) for v in velas])
    macd, linea_senal, _ = calcular_macd(cierres)
    return bool(macd.iloc[-1] < linea_senal.iloc[-1])


def macd_5min_bajista_2_velas(ticker):
    """Version 'reforzada' de macd_5min_bajista(): exige que las DOS
    ultimas velas de 5 min (no solo la ultima) tengan MACD por debajo de su
    linea de señal, para filtrar el ruido de una vela bajista suelta que
    resulta ser solo una pausa dentro de una subida mas larga. Solo se usa
    en el criterio de venta de ACCIONES (ver TRAILING_STOP_VENTA_PCT); no
    aplica a cripto, que sigue con macd_5min_bajista_cripto (una sola vela)."""
    resultado = pedir_velas_lote([ticker], TimeFrame(5, TimeFrameUnit.Minute), 5)
    velas = resultado.get(ticker, [])
    if len(velas) < 36:
        return None
    cierres = pd.Series([float(v.close) for v in velas])
    macd, linea_senal, _ = calcular_macd(cierres)
    return bool(macd.iloc[-1] < linea_senal.iloc[-1] and macd.iloc[-2] < linea_senal.iloc[-2])


def revisar_ventas_cripto():
    """Version de revisar_ventas() para cripto: SIN el chequeo de
    es_horario_operativo() (cripto es 24/7) y SIN "venta forzada antes del
    cierre" (no tiene un unico cierre diario del que calcular minutos-hasta-
    cierre, igual que en bot_completo.py/IBKR). Usa el umbral mas bajo
    UMBRAL_BENEFICIO_CRYPTO_PCT y la comision REAL de Alpaca para cripto
    (estimar_comision_cripto_alpaca), a diferencia de las acciones (sin
    comision, ver mas arriba)."""
    posiciones = [p for p in obtener_posiciones() if es_cripto(p.symbol)]

    # Poda del maximo/salida-parcial trackeados, solo para tickers de CRIPTO
    # (ver el comentario equivalente en revisar_ventas() - acciones-; ambas
    # funciones comparten el mismo dict/set pero podan cada una su propio
    # subconjunto, en su propio ciclo).
    tickers_cripto_vivos = {p.symbol for p in posiciones}
    for ticker_viejo in list(_maximo_beneficio_neto_por_posicion.keys()):
        if es_cripto(ticker_viejo) and ticker_viejo not in tickers_cripto_vivos:
            cerrar_seguimiento_venta(ticker_viejo)

    if not posiciones:
        return  # silencioso: se llama cada ciclo, no tiene sentido repetir "no hay posiciones"

    log(f"\n########## VENTAS - CRIPTO ##########")
    for pos in sorted(posiciones, key=lambda p: p.symbol):
        ticker = pos.symbol
        try:
            cantidad = float(pos.qty)
            coste_medio = float(pos.avg_entry_price)
            if cantidad <= 0 or coste_medio <= 0:
                continue

            precio_actual = precio_actual_ticker_cripto(ticker)
            if precio_actual is None:
                log(f"VENTAS: {ticker} - no se pudo obtener precio actual, se omite.")
                continue

            valor_compra = cantidad * coste_medio
            valor_venta = cantidad * precio_actual
            comision_total = estimar_comision_cripto_alpaca(valor_compra) + estimar_comision_cripto_alpaca(valor_venta)
            beneficio_pct_bruto = (precio_actual - coste_medio) / coste_medio * 100
            comision_total_pct = (comision_total / valor_compra * 100) if valor_compra else 0.0
            beneficio_pct = beneficio_pct_bruto - comision_total_pct

            info_posicion = (f"{cantidad:g} unidades, precio medio {coste_medio:.4f} USD, "
                              f"comision estimada {comision_total:.2f} USD")

            # Mismo criterio que acciones (trailing stop + refuerzo de 2
            # velas + salida parcial, peticion del usuario, sept. 2026), con
            # el umbral y la comision propios de cripto.
            accion, motivo = decidir_accion_venta(ticker, beneficio_pct, UMBRAL_BENEFICIO_CRYPTO_PCT)

            if accion == "MANTENER" and beneficio_pct >= UMBRAL_BENEFICIO_CRYPTO_PCT:
                if macd_5min_bajista_cripto_2_velas(ticker):
                    accion, motivo = "VENTA_TOTAL", "2 velas de 5min bajistas seguidas"

            if accion == "MANTENER":
                log(f"VENTAS: {ticker} - {info_posicion} - beneficio neto {beneficio_pct:.2f}% "
                    f"(bruto {beneficio_pct_bruto:.2f}%) {motivo} -> se mantiene.")
                continue

            cantidad_a_vender = cantidad
            if accion == "VENTA_PARCIAL":
                parcial = round(cantidad * PORCENTAJE_SCALE_OUT, DECIMALES_FRACCION_CRIPTO)
                valor_parcial = parcial * precio_actual
                if parcial <= 0 or parcial >= cantidad or valor_parcial < VALOR_MINIMO_OPERACION_CRIPTO_USD:
                    # Cantidad/importe demasiado pequeños para dividir con
                    # sentido: se vende todo de una vez en su lugar.
                    accion, cantidad_a_vender = "VENTA_TOTAL", cantidad
                else:
                    cantidad_a_vender = parcial

            etiqueta_accion = "VENTA PARCIAL" if accion == "VENTA_PARCIAL" else "VENTA"
            precio_limite = calcular_precio_limite_venta(precio_actual)
            log(f"VENTAS: {ticker} - {info_posicion} - beneficio neto {beneficio_pct:.2f}% "
                f"(bruto {beneficio_pct_bruto:.2f}%), {motivo} -> {etiqueta_accion} de {cantidad_a_vender:g} "
                f"(orden limitada IOC).")
            cancelar_ordenes_abiertas(ticker)
            orden = LimitOrderRequest(symbol=ticker, qty=cantidad_a_vender, limit_price=precio_limite,
                                       side=OrderSide.SELL, time_in_force=TimeInForce.IOC)
            trade = _trading_client.submit_order(order_data=orden)
            estado = esperar_estado_final_orden(trade.id)
            log(f"VENTAS: {ticker} - orden limitada IOC a {precio_limite} USD, estado: {estado}")
            if estado == "filled":
                cantidad_real, precio_real = obtener_ejecucion_real(trade.id, cantidad_a_vender, precio_limite)
                registrar_operacion_historial(ticker, "VENTA", cantidad_real, precio_real,
                                               coste_medio=coste_medio, beneficio_pct=beneficio_pct)
                notificar_telegram(f"🔴 {etiqueta_accion} <b>{ticker}</b>: {formato_es(cantidad_real, 6)} a "
                                    f"{formato_es(precio_real)} USD (total {formato_es(cantidad_real * precio_real)} USD, "
                                    f"beneficio {formato_es(beneficio_pct, signo=True)}%)")
                if accion == "VENTA_TOTAL":
                    cerrar_seguimiento_venta(ticker)
        except Exception as e:
            log(f"VENTAS: {ticker} - ERROR inesperado al procesar la posicion cripto: {type(e).__name__}: {e}. Se omite.")


# --- Compras ---
def revisar_compras():
    if not es_horario_operativo():
        log("COMPRAS: fuera de horario operativo (4:00-20:00 ET), no se analiza nada este ciclo.")
        return

    if en_postmercado_us():
        log("COMPRAS: en postmercado (16:00-20:00 ET), no se analiza ningun valor en busca de "
            "señales de compra en este ciclo (solo se vende en este tramo).")
        return

    valor_total_cartera_usd = obtener_valor_total_cartera_usd()
    if valor_total_cartera_usd is None:
        log("COMPRAS: no se pudo obtener el valor total de la cartera, se omite este ciclo.")
        return
    limite_por_valor_usd = valor_total_cartera_usd * (LIMITE_EXPOSICION_PCT / 100)

    posiciones = obtener_posiciones()
    fuera_sesion = fuera_de_sesion_regular_us()

    # Tope de posiciones simultaneas y caja disponible (ver
    # MAX_POSICIONES_ABIERTAS): se calculan UNA vez al principio del ciclo y
    # se van reservando de forma optimista segun se decide cada compra, para
    # que varias señales dentro del MISMO ciclo no se salten el limite
    # entre ellas.
    posiciones_abiertas_tickers = {p.symbol for p in posiciones}
    efectivo_disponible_usd = obtener_efectivo_disponible_usd()
    if efectivo_disponible_usd is None:
        log("COMPRAS: no se pudo obtener el efectivo disponible; no se aplicara el limite de caja "
            "disponible este ciclo (el resto de limites de exposicion siguen activos).")

    log(f"\n########## COMPRAS ##########")
    log(f"COMPRAS: analizando {len(ACTIVOS)} valores en lote...")
    decisiones, precios = analizar_todos_los_activos(ACTIVOS)

    analizados = sum(1 for d in decisiones.values() if d != "SIN_DATOS")
    senales = 0
    errores = 0

    for ticker in ACTIVOS:
        decision = decisiones.get(ticker)
        if decision != "COMPRA":
            continue
        senales += 1

        try:
            precio_actual = precios.get(ticker)
            if precio_actual is None:
                log(f"COMPRAS: {ticker} - senal de COMPRA pero no se pudo obtener precio, se omite.")
                continue

            if en_ventana_sin_compra():
                pos_existente = next((p for p in posiciones if p.symbol == ticker), None)
                if pos_existente is None or precio_actual >= float(pos_existente.avg_entry_price):
                    log(f"COMPRAS: {ticker} - dentro de la ventana de no-compra (ultimos "
                        f"{MINUTOS_SIN_COMPRAR_ANTES_CIERRE} min antes del cierre) y no promedia "
                        f"a la baja, se omite.")
                    continue

            valor_posicion_actual = obtener_valor_posicion_actual_usd(posiciones, ticker)
            margen_disponible = limite_por_valor_usd - valor_posicion_actual
            if margen_disponible <= 0:
                log(f"COMPRAS: {ticker} - senal de COMPRA pero ya tiene {valor_posicion_actual:.2f} USD "
                    f"({LIMITE_EXPOSICION_PCT}% del limite = {limite_por_valor_usd:.2f} USD alcanzado) -> se omite.")
                continue

            importe_a_usar = min(IMPORTE_EUROS * TIPO_CAMBIO_EUR_USD, margen_disponible)

            if importe_a_usar < VALOR_MINIMO_OPERACION_FRACCIONARIA_USD:
                log(f"COMPRAS: {ticker} - senal de COMPRA pero el margen disponible "
                    f"({importe_a_usar:.2f} USD) no llega al minimo de "
                    f"{VALOR_MINIMO_OPERACION_FRACCIONARIA_USD:.2f} USD por operacion, se omite.")
                continue

            # Tope de posiciones simultaneas: solo se aplica a valores NUEVOS
            # (si ya se tiene el ticker, esto es promediar/añadir, no abrir
            # una posicion mas) - ver MAX_POSICIONES_ABIERTAS.
            if valor_posicion_actual <= 0 and ticker not in posiciones_abiertas_tickers \
                    and len(posiciones_abiertas_tickers) >= MAX_POSICIONES_ABIERTAS:
                log(f"COMPRAS: {ticker} - senal de COMPRA pero ya hay {len(posiciones_abiertas_tickers)} "
                    f"posiciones abiertas (limite MAX_POSICIONES_ABIERTAS={MAX_POSICIONES_ABIERTAS}) y "
                    f"este seria un valor nuevo, se omite.")
                continue

            if efectivo_disponible_usd is not None and importe_a_usar > efectivo_disponible_usd:
                margen_minimo_usd = MARGEN_EFECTIVO_MINIMO_USD
                importe_ajustado = efectivo_disponible_usd - margen_minimo_usd
                if importe_ajustado < VALOR_MINIMO_OPERACION_FRACCIONARIA_USD:
                    log(f"COMPRAS: {ticker} - senal de COMPRA pero el efectivo disponible "
                        f"({efectivo_disponible_usd:.2f} USD) menos el margen de seguridad "
                        f"({margen_minimo_usd:.2f} USD) no llega al minimo de "
                        f"{VALOR_MINIMO_OPERACION_FRACCIONARIA_USD:.2f} USD por operacion, se omite.")
                    continue
                log(f"COMPRAS: {ticker} - importe estandar ({importe_a_usar:.2f} USD) reducido a "
                    f"{importe_ajustado:.2f} USD para no dejar la cuenta por debajo de "
                    f"{margen_minimo_usd:.2f} USD (efectivo disponible: {efectivo_disponible_usd:.2f} USD).")
                importe_a_usar = importe_ajustado

            # Reserva optimista: se descuenta/anota AQUI, no tras confirmar la
            # orden, para que la SIGUIENTE señal de este mismo ciclo ya vea
            # el hueco/caja reducidos.
            posiciones_abiertas_tickers.add(ticker)
            if efectivo_disponible_usd is not None:
                efectivo_disponible_usd -= importe_a_usar

            cantidad_estimada = round(importe_a_usar / precio_actual, DECIMALES_FRACCION)

            if fuera_sesion:
                # Fuera de sesion regular Alpaca exige orden LIMITADA (no
                # admite ordenes a mercado), pero desde marzo 2024 SI admite
                # `qty` fraccionario en ordenes limitadas con
                # extended_hours=True, igual que en sesion regular.
                log(f"COMPRAS: {ticker} - senal de COMPRA, comprando ~{cantidad_estimada:g} acciones "
                    f"(importe {importe_a_usar:.2f} USD) a ~{precio_actual} USD (posicion actual: "
                    f"{valor_posicion_actual:.2f} USD, limite: {limite_por_valor_usd:.2f} USD). "
                    f"[pre/postmercado: orden limitada al precio exacto]")
                orden = LimitOrderRequest(symbol=ticker, qty=cantidad_estimada, limit_price=precio_actual,
                                           side=OrderSide.BUY, time_in_force=TimeInForce.DAY,
                                           extended_hours=True)
            else:
                log(f"COMPRAS: {ticker} - senal de COMPRA, comprando ~{cantidad_estimada:g} acciones "
                    f"(importe {importe_a_usar:.2f} USD) a ~{precio_actual} USD (posicion actual: "
                    f"{valor_posicion_actual:.2f} USD, limite: {limite_por_valor_usd:.2f} USD).")
                orden = MarketOrderRequest(symbol=ticker, notional=round(importe_a_usar, 2),
                                            side=OrderSide.BUY, time_in_force=TimeInForce.DAY)

            cancelar_ordenes_abiertas(ticker)
            trade = _trading_client.submit_order(order_data=orden)
            estado = esperar_estado_final_orden(trade.id)
            log(f"COMPRAS: {ticker} - estado de la orden: {estado}")
            if estado == "filled":
                cantidad_real, precio_real = obtener_ejecucion_real(trade.id, cantidad_estimada, precio_actual)
                registrar_operacion_historial(ticker, "COMPRA", cantidad_real, precio_real)
                notificar_telegram(f"🟢 COMPRA <b>{ticker}</b>: {formato_es(cantidad_real, 4)} acciones a "
                                    f"{formato_es(precio_real)} USD (total {formato_es(cantidad_real * precio_real)} USD)")
        except Exception as e:
            log(f"COMPRAS: {ticker} - ERROR inesperado al procesar la señal de compra: {type(e).__name__}: {e}. Se omite.")
            errores += 1

    log(f"COMPRAS: {analizados} analizados, {senales} señales de compra, {errores} errores.")


def revisar_compras_cripto():
    """Version de revisar_compras() para cripto: SIN el chequeo de
    es_horario_operativo() (24/7), SIN distincion pre/postmercado, y con un
    limite ADICIONAL propio -LIMITE_EXPOSICION_CRYPTO_TOTAL_PCT- sobre el
    CONJUNTO de toda la cripto (no solo por moneda individual, que sigue
    usando el mismo LIMITE_EXPOSICION_PCT que las acciones), peticion
    explicita del usuario (sept. 2026)."""
    valor_total_cartera_usd = obtener_valor_total_cartera_usd()
    if valor_total_cartera_usd is None:
        log("COMPRAS: no se pudo obtener el valor total de la cartera, se omite este ciclo de cripto.")
        return

    limite_por_valor_usd = valor_total_cartera_usd * (LIMITE_EXPOSICION_PCT / 100)
    limite_cripto_total_usd = valor_total_cartera_usd * (LIMITE_EXPOSICION_CRYPTO_TOTAL_PCT / 100)

    posiciones = obtener_posiciones()
    exposicion_cripto_actual_usd = calcular_exposicion_total_cripto_usd(posiciones)

    # Caja disponible (ver MAX_POSICIONES_ABIERTAS/obtener_efectivo_disponible_usd
    # en revisar_compras()): no hace falta un tope de NUMERO de posiciones
    # aparte para cripto (el universo son solo 6 monedas como mucho, y el
    # limite de exposicion TOTAL de arriba ya acota el riesgo agregado), pero
    # SI se comprueba que haya efectivo real antes de comprar.
    efectivo_disponible_usd = obtener_efectivo_disponible_usd()
    if efectivo_disponible_usd is None:
        log("COMPRAS: no se pudo obtener el efectivo disponible; no se aplicara el limite de caja "
            "disponible este ciclo de cripto (el resto de limites de exposicion siguen activos).")

    log(f"\n########## COMPRAS - CRIPTO ##########")
    log(f"COMPRAS: analizando {len(ACTIVOS_CRYPTO)} criptomonedas en lote...")
    decisiones, precios = analizar_todos_los_activos_cripto(ACTIVOS_CRYPTO)

    analizados = sum(1 for d in decisiones.values() if d != "SIN_DATOS")
    senales = 0
    errores = 0

    for ticker in ACTIVOS_CRYPTO:
        decision = decisiones.get(ticker)
        if decision != "COMPRA":
            continue
        senales += 1

        try:
            precio_actual = precios.get(ticker)
            if precio_actual is None:
                log(f"COMPRAS: {ticker} - senal de COMPRA pero no se pudo obtener precio, se omite.")
                continue

            valor_posicion_actual = obtener_valor_posicion_actual_usd(posiciones, ticker)
            margen_disponible = limite_por_valor_usd - valor_posicion_actual
            if margen_disponible <= 0:
                log(f"COMPRAS: {ticker} - senal de COMPRA pero ya tiene {valor_posicion_actual:.2f} USD "
                    f"({LIMITE_EXPOSICION_PCT}% del limite = {limite_por_valor_usd:.2f} USD alcanzado) -> se omite.")
                continue

            importe_a_usar = min(IMPORTE_EUROS * TIPO_CAMBIO_EUR_USD, margen_disponible)

            if exposicion_cripto_actual_usd + importe_a_usar > limite_cripto_total_usd:
                log(f"COMPRAS: {ticker} - senal de COMPRA pero comprar {importe_a_usar:.2f} USD mas "
                    f"superaria el limite de exposicion TOTAL en cripto ({LIMITE_EXPOSICION_CRYPTO_TOTAL_PCT}% "
                    f"de la cartera = {limite_cripto_total_usd:.2f} USD; ya invertido en cripto: "
                    f"{exposicion_cripto_actual_usd:.2f} USD), se omite.")
                continue

            if importe_a_usar < VALOR_MINIMO_OPERACION_CRIPTO_USD:
                log(f"COMPRAS: {ticker} - senal de COMPRA pero el margen disponible "
                    f"({importe_a_usar:.2f} USD) no llega al minimo de "
                    f"{VALOR_MINIMO_OPERACION_CRIPTO_USD:.2f} USD por operacion, se omite.")
                continue

            if efectivo_disponible_usd is not None and importe_a_usar > efectivo_disponible_usd:
                margen_minimo_usd = MARGEN_EFECTIVO_MINIMO_USD
                importe_ajustado = efectivo_disponible_usd - margen_minimo_usd
                if importe_ajustado < VALOR_MINIMO_OPERACION_CRIPTO_USD:
                    log(f"COMPRAS: {ticker} - senal de COMPRA pero el efectivo disponible "
                        f"({efectivo_disponible_usd:.2f} USD) menos el margen de seguridad "
                        f"({margen_minimo_usd:.2f} USD) no llega al minimo de "
                        f"{VALOR_MINIMO_OPERACION_CRIPTO_USD:.2f} USD por operacion, se omite.")
                    continue
                log(f"COMPRAS: {ticker} - importe estandar ({importe_a_usar:.2f} USD) reducido a "
                    f"{importe_ajustado:.2f} USD para no dejar la cuenta por debajo de "
                    f"{margen_minimo_usd:.2f} USD (efectivo disponible: {efectivo_disponible_usd:.2f} USD).")
                importe_a_usar = importe_ajustado
            if efectivo_disponible_usd is not None:
                efectivo_disponible_usd -= importe_a_usar

            cantidad_estimada = round(importe_a_usar / precio_actual, DECIMALES_FRACCION_CRIPTO)

            log(f"COMPRAS: {ticker} (cripto) - senal de COMPRA, comprando ~{cantidad_estimada:g} unidades "
                f"(importe {importe_a_usar:.2f} USD) a ~{precio_actual} USD (posicion actual: "
                f"{valor_posicion_actual:.2f} USD, limite: {limite_por_valor_usd:.2f} USD).")
            orden = MarketOrderRequest(symbol=ticker, notional=round(importe_a_usar, 2),
                                        side=OrderSide.BUY, time_in_force=TimeInForce.IOC)

            cancelar_ordenes_abiertas(ticker)
            trade = _trading_client.submit_order(order_data=orden)
            estado = esperar_estado_final_orden(trade.id)
            log(f"COMPRAS: {ticker} - estado de la orden: {estado}")
            if estado == "filled":
                cantidad_real, precio_real = obtener_ejecucion_real(trade.id, cantidad_estimada, precio_actual)
                registrar_operacion_historial(ticker, "COMPRA", cantidad_real, precio_real)
                notificar_telegram(f"🟢 COMPRA <b>{ticker}</b>: {formato_es(cantidad_real, 6)} a "
                                    f"{formato_es(precio_real)} USD (total {formato_es(cantidad_real * precio_real)} USD)")
                # Para que la SIGUIENTE cripto de este mismo ciclo vea el
                # limite total ya actualizado (sin esto, dos señales en el
                # mismo ciclo podrian sumar mas del limite entre las dos).
                exposicion_cripto_actual_usd += importe_a_usar
        except Exception as e:
            log(f"COMPRAS: {ticker} - ERROR inesperado al procesar la señal de compra cripto: {type(e).__name__}: {e}. Se omite.")
            errores += 1

    log(f"COMPRAS CRIPTO: {analizados} analizados, {senales} señales de compra, {errores} errores.")


def _emoji_pl(valor):
    return "🟢" if valor >= 0 else "🔴"


def generar_resumen():
    """Resumen de cierre: posiciones abiertas (P/L no realizado) + operaciones
    cerradas HOY (P/L realizado, leido del historial persistente -ver
    registrar_operacion_historial()-). Se registra en el log en texto plano
    y, si esta configurado, tambien se manda como mensaje de Telegram en
    formato tabla (ver notificar_telegram() / ALPACA_NOTES.md)."""
    posiciones = obtener_posiciones()
    lineas_log = ["📊 RESUMEN DE CIERRE (Alpaca)", "", "Posiciones abiertas:"]
    filas_abiertas = []

    if not posiciones:
        lineas_log.append("(ninguna)")
    else:
        total_valor = 0.0
        total_pl = 0.0
        for p in sorted(posiciones, key=lambda p: p.symbol):
            valor = float(p.market_value)
            pl = float(p.unrealized_pl)
            pl_pct = float(p.unrealized_plpc) * 100
            total_valor += valor
            total_pl += pl
            filas_abiertas.append((p.symbol, float(p.qty), valor, pl, pl_pct))
            lineas_log.append(f"{p.symbol}: {float(p.qty):g} acciones, valor {valor:.2f} USD, "
                              f"P/L {pl:+.2f} USD ({pl_pct:+.2f}%)")
        lineas_log.append(f"TOTAL invertido: {total_valor:.2f} USD, P/L no realizado {total_pl:+.2f} USD")

    hoy = datetime.now().date()
    ventas_hoy = [o for o in cargar_historial_operaciones()
                  if o.get("lado") == "VENTA" and datetime.fromisoformat(o["fecha_hora"]).date() == hoy]
    lineas_log.append("")
    lineas_log.append("Operaciones cerradas hoy:")
    filas_cerradas = []
    ganancia_total = 0.0
    if not ventas_hoy:
        lineas_log.append("(ninguna)")
    else:
        for o in sorted(ventas_hoy, key=lambda o: o["fecha_hora"]):
            coste_medio = o.get("coste_medio")
            ganancia = (o["precio"] - coste_medio) * o["cantidad"] if coste_medio is not None else None
            if ganancia is not None:
                ganancia_total += ganancia
            beneficio_pct = o.get("beneficio_pct")
            filas_cerradas.append((o["ticker"], o["cantidad"], o["precio"], ganancia, beneficio_pct))
            lineas_log.append(f"{o['ticker']}: {o['cantidad']:g} acciones a {o['precio']:.2f} USD"
                              + (f", ganancia {ganancia:+.2f} USD" if ganancia is not None else "")
                              + (f" ({beneficio_pct:+.2f}%)" if beneficio_pct is not None else ""))
        lineas_log.append(f"TOTAL ganancia/perdida realizada hoy: {ganancia_total:+.2f} USD")

    log("\n" + "=" * 60 + "\n" + "\n".join(lineas_log) + "\n" + "=" * 60)

    # Version HTML compacta (tabla monoespaciada) para Telegram.
    bloques_html = ["📊 <b>RESUMEN DE CIERRE</b>", "", "<b>Posiciones abiertas:</b>"]
    if not filas_abiertas:
        bloques_html.append("(ninguna)")
    else:
        tabla = [f"  {'Ticker':<7}{'P/L %':>10}{'P/L USD':>12}"]
        for symbol, cantidad, valor, pl, pl_pct in filas_abiertas:
            tabla.append(f"{_emoji_pl(pl)} {symbol:<6}{formato_es(pl_pct, signo=True):>9}%{formato_es(pl, signo=True):>12}")
        bloques_html.append("<pre>" + "\n".join(tabla) + "</pre>")
    bloques_html.append("<b>Operaciones cerradas hoy:</b>")
    if not filas_cerradas:
        bloques_html.append("(ninguna)")
    else:
        tabla = [f"  {'Ticker':<7}{'Cant.':>8}{'Gan. USD':>12}"]
        for ticker, cantidad, precio, ganancia, beneficio_pct in filas_cerradas:
            emoji = _emoji_pl(ganancia) if ganancia is not None else "⚪"
            ganancia_str = f"{formato_es(ganancia, signo=True):>12}" if ganancia is not None else f"{'N/D':>12}"
            tabla.append(f"{emoji} {ticker:<6}{formato_es(cantidad, 2):>8}{ganancia_str}")
        bloques_html.append("<pre>" + "\n".join(tabla) + "</pre>")
        bloques_html.append(f"<b>TOTAL</b> ganancia/perdida realizada hoy: {formato_es(ganancia_total, signo=True)} USD")

    notificar_telegram("\n".join(bloques_html))




def evitar_suspension_windows():
    """Evita que Windows suspenda/hiberne el SISTEMA mientras el bot esta
    activo. NO fuerza la pantalla a quedarse encendida (sin
    ES_DISPLAY_REQUIRED, peticion del usuario sept. 2026: el bot no debe
    apagar la pantalla, esa decision es solo de la configuracion de
    energia de Windows -en la practica no aplica en el servidor AWS/Linux
    donde corre el bot ahora, pero se mantiene igual que bot_completo.py
    por si se ejecuta en Windows-)."""
    try:
        import ctypes
        ES_CONTINUOUS = 0x80000000
        ES_SYSTEM_REQUIRED = 0x00000001
        ES_AWAYMODE_REQUIRED = 0x00000040
        ctypes.windll.kernel32.SetThreadExecutionState(
            ES_CONTINUOUS | ES_SYSTEM_REQUIRED | ES_AWAYMODE_REQUIRED
        )
        log("Suspension/hibernacion automatica de Windows desactivada mientras el bot este en "
            "marcha (la pantalla sigue su configuracion normal de energia, sin forzarla).")
    except Exception:
        pass  # no es Windows, o no se pudo aplicar; no es critico (p.ej. en un servidor Linux)


def main():
    evitar_suspension_windows()
    escribir_pid()
    actualizar_latido()
    cargar_estado_venta()
    threading.Thread(target=vigilante_congelacion, daemon=True).start()

    modo_texto = "PAPER (simulado)" if ALPACA_PAPER else "REAL - DINERO REAL"
    log("#" * 60)
    log(f"BOT ALPACA - MODO DE CUENTA: {modo_texto}")
    if not ALPACA_PAPER:
        log("ATENCION: esta es una cuenta REAL. Las ordenes de este bot son DINERO REAL, no una simulacion.")
    log("#" * 60)

    resumenes_enviados_hoy = set()
    proxima_revision_cripto = 0.0
    proxima_revision_acciones = 0.0

    while True:
        try:
            # Cripto opera 24/7 (añadido sept. 2026): a diferencia de antes,
            # cuando el mercado de US estaba cerrado el bot dormia horas de
            # un tiron (segundos_hasta_apertura()). Ahora ya no tiene
            # sentido dormir asi -siempre hay algo que revisar en cripto-,
            # asi que el bucle nunca duerme del todo.
            #
            # Cripto (CRYPTO_INTERVALO_SEGUNDOS = 1 min) y acciones
            # (INTERVALO_SEGUNDOS = 2 min 10s) corren en su propia cadencia,
            # cada una independiente de la otra (peticion del usuario, sept.
            # 2026: cripto cada minuto), igual que en bot_completo.py/IBKR.
            # revisar_ventas()/revisar_compras() (acciones) se siguen auto-
            # limitando con su propio chequeo de es_horario_operativo().
            hoy = datetime.now(ZONA_NY).date()
            ahora_ny = datetime.now(ZONA_NY).time()
            if es_horario_operativo() and ahora_ny >= HORA_CIERRE_EXTENDIDO_US and hoy not in resumenes_enviados_hoy:
                try:
                    generar_resumen()
                except Exception as e:
                    log(f"RESUMEN: error al generar el resumen: {type(e).__name__}: {e}")
                resumenes_enviados_hoy.add(hoy)

            ahora_mono = time.monotonic()

            if ahora_mono >= proxima_revision_cripto:
                inicio_cripto = time.monotonic()
                log("=" * 60)
                log("Iniciando nuevo ciclo de revision CRIPTO.")
                try:
                    revisar_ventas_cripto()
                except Exception as e:
                    log(f"ERROR inesperado en revisar_ventas_cripto: {type(e).__name__}: {e}")
                try:
                    revisar_compras_cripto()
                except Exception as e:
                    log(f"ERROR inesperado en revisar_compras_cripto: {type(e).__name__}: {e}")
                duracion_cripto = time.monotonic() - inicio_cripto
                if duracion_cripto > CRYPTO_INTERVALO_SEGUNDOS:
                    log(f"AVISO: el ciclo CRIPTO ha tardado {duracion_cripto:.0f}s, mas que su "
                        f"intervalo configurado ({CRYPTO_INTERVALO_SEGUNDOS}s).")
                proxima_revision_cripto = inicio_cripto + CRYPTO_INTERVALO_SEGUNDOS
                actualizar_latido()

            if ahora_mono >= proxima_revision_acciones:
                inicio_acciones = time.monotonic()
                log("=" * 60)
                log("Iniciando nuevo ciclo de revision ACCIONES.")
                try:
                    revisar_ventas()
                except Exception as e:
                    log(f"ERROR inesperado en revisar_ventas: {type(e).__name__}: {e}")
                try:
                    revisar_compras()
                except Exception as e:
                    log(f"ERROR inesperado en revisar_compras: {type(e).__name__}: {e}")
                duracion_acciones = time.monotonic() - inicio_acciones
                if duracion_acciones > INTERVALO_SEGUNDOS:
                    log(f"AVISO: el ciclo ACCIONES ha tardado {duracion_acciones:.0f}s, mas que el "
                        f"intervalo configurado ({INTERVALO_SEGUNDOS}s).")
                proxima_revision_acciones = inicio_acciones + INTERVALO_SEGUNDOS
                actualizar_latido()

            proxima_revision = min(proxima_revision_cripto, proxima_revision_acciones)
            segundos_espera = max(proxima_revision - time.monotonic(), 1)
            log(f"Esperando {segundos_espera:.0f}s hasta la siguiente revision "
                f"(cripto cada {CRYPTO_INTERVALO_SEGUNDOS}s, acciones cada {INTERVALO_SEGUNDOS}s)...")
            time.sleep(segundos_espera)
            actualizar_latido()
        except KeyboardInterrupt:
            log("Detenido por el usuario (Ctrl+C).")
            break
        except Exception as e:
            log(f"ERROR FATAL fuera del ciclo principal: {type(e).__name__}: {e}. "
                f"Reiniciando el ciclo en 15 segundos...")
            notificar_telegram(f"⚠️ <b>ERROR FATAL</b> en el bot de Alpaca: {type(e).__name__}: {e}. "
                               f"Reiniciando el ciclo en 15s.")
            time.sleep(15)


if __name__ == "__main__":
    main()
