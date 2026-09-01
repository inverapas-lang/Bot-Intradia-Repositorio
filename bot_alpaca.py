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
from datetime import datetime, time as dt_time, timedelta
from zoneinfo import ZoneInfo

import pandas as pd
import requests

try:
    from alpaca.trading.client import TradingClient
    from alpaca.trading.requests import MarketOrderRequest, LimitOrderRequest, GetOrdersRequest
    from alpaca.trading.enums import OrderSide, TimeInForce, QueryOrderStatus
    from alpaca.data.historical import StockHistoricalDataClient
    from alpaca.data.requests import StockBarsRequest
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

MINUTOS_SIN_COMPRAR_ANTES_CIERRE = 90    # ultimos 90 min antes del cierre: no comprar (salvo promediar a la baja)
MINUTOS_VENTA_FORZADA_ANTES_CIERRE = 15  # ultimos 15 min: vender lo que tenga entre +0.5% y +2% de beneficio
BENEFICIO_MAX_VENTA_FORZADA_PCT = 2.0
UMBRAL_BENEFICIO_PCT = 0.5
MARGEN_ORDEN_LIMITADA_VENTA_PCT = 0.2

INTERVALO_SEGUNDOS = 130  # 2 min 10 s (ajustado tras pruebas en paper, ago 2026)
LIMITE_EXPOSICION_PCT = 15   # % maximo del total de cartera por valor
IMPORTE_EUROS = 1000         # presupuesto maximo por operacion (convertido a USD)
TIPO_CAMBIO_EUR_USD = 1.14   # actualiza a mano si quieres mas precision

DECIMALES_FRACCION = 4
VALOR_MINIMO_OPERACION_FRACCIONARIA_USD = 1.0

# --- Lista de valores: misma seleccion de 30 tickers que en bot_completo.py ---
ACTIVOS = ["AAPL", "MSFT", "NVDA", "AMZN", "GOOGL", "META", "TSLA", "AVGO", "AMD", "NFLX",
           "INTC", "QCOM", "CSCO", "SMCI",
           "JPM", "BAC", "WFC", "C", "V", "MA",
           "XOM", "CVX",
           "WMT", "DIS", "KO", "JNJ", "PFE", "F", "T", "GE"]

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
            os._exit(1)


def log(mensaje):
    actualizar_latido()
    ahora = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{ahora}] {mensaje}")


# --- Horarios ---
def es_horario_operativo():
    """True si el mercado US esta en horario extendido (4:00-20:00 ET),
    de lunes a viernes. NOTA: igual que en bot_completo.py, no tiene en
    cuenta festivos del mercado, solo fin de semana."""
    ahora = datetime.now(ZONA_NY)
    if ahora.weekday() >= 5:
        return False
    return HORA_INICIO_US <= ahora.time() < HORA_CIERRE_EXTENDIDO_US


def en_postmercado_us():
    ahora = datetime.now(ZONA_NY)
    if ahora.weekday() >= 5:
        return False
    return HORA_CIERRE_US <= ahora.time() < HORA_CIERRE_EXTENDIDO_US


def fuera_de_sesion_regular_us():
    ahora = datetime.now(ZONA_NY)
    if ahora.weekday() >= 5:
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


def segundos_hasta_apertura():
    """Segundos hasta el inicio del premercado (4:00 ET), saltando fines de
    semana. Se usa cuando el mercado esta cerrado del todo."""
    ahora = datetime.now(ZONA_NY)
    candidato = ahora.replace(hour=HORA_INICIO_US.hour, minute=HORA_INICIO_US.minute,
                               second=0, microsecond=0)
    if candidato <= ahora:
        candidato += timedelta(days=1)
    while candidato.weekday() >= 5:
        candidato += timedelta(days=1)
    return max((candidato - ahora).total_seconds(), 0)


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
    cuatro_cortas_alcistas = (
        all(detalle[n] is not None for n in NOMBRES_4_CORTAS)
        and all(detalle[n] for n in NOMBRES_4_CORTAS)
    )
    faltan_datos = any(detalle[tf["nombre"]] is None for tf in TEMPORALIDADES)
    total_false = sum(1 for tf in TEMPORALIDADES if detalle[tf["nombre"]] is False)
    cortas_ok = all(detalle[tf["nombre"]] for tf in TEMPORALIDADES
                     if tf["tipo"] == "corta" and detalle[tf["nombre"]] is not None)
    largas_ok = all(detalle[tf["nombre"]] for tf in TEMPORALIDADES
                     if tf["tipo"] == "larga" and detalle[tf["nombre"]] is not None)

    if cuatro_cortas_alcistas:
        return "COMPRA"
    if faltan_datos:
        return "SIN_DATOS"
    if total_false == 0:
        return "COMPRA"
    if total_false == 1:
        return "COMPRA"
    if cortas_ok and not largas_ok:
        return "BLOQUEADO"
    return "SIN_SENAL"


def macd_alcista_o_bajista(velas, tipo):
    """Devuelve True/False/None (None si faltan datos) para UNA temporalidad,
    a partir de la lista de barras de Alpaca ya ordenada cronologicamente."""
    if len(velas) < 35:
        return None
    cierres = pd.Series([float(v.close) for v in velas])
    macd, linea_senal, histograma = calcular_macd(cierres)
    if tipo == "corta":
        return bool(macd.iloc[-1] > linea_senal.iloc[-1])
    ultimas_3 = histograma.iloc[-3:]
    return bool(ultimas_3.iloc[2] > ultimas_3.iloc[0])


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


def analizar_todos_los_activos(tickers):
    """Pide las 7 temporalidades para TODOS los tickers (7 peticiones en
    total) y devuelve {ticker: decision} + {ticker: precio_actual (ultimo
    cierre de 1 minuto)}."""
    velas_por_temporalidad = {}
    for tf in TEMPORALIDADES:
        velas_por_temporalidad[tf["nombre"]] = pedir_velas_lote(tickers, tf["timeframe"], tf["duration_dias"])

    decisiones = {}
    precios = {}
    for ticker in tickers:
        detalle = {}
        for tf in TEMPORALIDADES:
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


def obtener_posiciones():
    """Devuelve la lista de posiciones abiertas (solo largas: este bot
    nunca abre cortos)."""
    try:
        return [p for p in _trading_client.get_all_positions() if float(p.qty) > 0]
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


def obtener_valor_posicion_actual_usd(posiciones, ticker):
    for p in posiciones:
        if p.symbol == ticker and float(p.qty) > 0:
            return float(p.market_value)
    return 0.0


# --- Ventas ---
def revisar_ventas():
    posiciones = obtener_posiciones()
    if not posiciones:
        log("VENTAS: no hay posiciones abiertas.")
        return

    if not es_horario_operativo():
        log("VENTAS: fuera de horario operativo (4:00-20:00 ET), no se intenta vender nada este ciclo.")
        return

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
                precio_limite = calcular_precio_limite_venta(precio_actual)
                log(f"VENTAS: {ticker} - {info_posicion} - beneficio {beneficio_pct:.2f}%, "
                    f"dentro de los ultimos {MINUTOS_VENTA_FORZADA_ANTES_CIERRE} min antes del "
                    f"cierre -> VENTA FORZADA (orden limitada a {precio_limite} USD).")
                cancelar_ordenes_abiertas(ticker)
                orden = LimitOrderRequest(symbol=ticker, qty=cantidad, limit_price=precio_limite,
                                           side=OrderSide.SELL, time_in_force=TimeInForce.DAY)
                trade = _trading_client.submit_order(order_data=orden)
                estado = esperar_estado_final_orden(trade.id)
                log(f"VENTAS: {ticker} - orden limitada, estado: {estado}")
                if estado == "filled":
                    cantidad_real, precio_real = obtener_ejecucion_real(trade.id, cantidad, precio_limite)
                    registrar_operacion_historial(ticker, "VENTA", cantidad_real, precio_real,
                                                   coste_medio=coste_medio, beneficio_pct=beneficio_pct)
                    notificar_telegram(f"🔴 VENTA FORZADA <b>{ticker}</b>: {formato_es(cantidad_real, 4)} acciones a "
                                        f"{formato_es(precio_real)} USD (beneficio {formato_es(beneficio_pct, signo=True)}%)")
                continue

            if beneficio_pct < UMBRAL_BENEFICIO_PCT:
                log(f"VENTAS: {ticker} - {info_posicion} - beneficio {beneficio_pct:.2f}%, por debajo del umbral -> se mantiene.")
                continue

            bajista = macd_5min_bajista(ticker)
            if bajista is None:
                log(f"VENTAS: {ticker} - {info_posicion} - datos insuficientes para MACD 5min, se mantiene por precaucion.")
                continue

            if not bajista:
                log(f"VENTAS: {ticker} - {info_posicion} - beneficio {beneficio_pct:.2f}%, MACD 5min ALCISTA -> se deja correr.")
                continue

            if fuera_sesion:
                precio_limite = precio_actual
                log(f"VENTAS: {ticker} - {info_posicion} - beneficio {beneficio_pct:.2f}%, "
                    f"MACD 5min BAJISTA -> VENDIENDO (orden limitada al precio exacto, "
                    f"fuera de sesion regular).")
                orden = LimitOrderRequest(symbol=ticker, qty=cantidad, limit_price=precio_limite,
                                           side=OrderSide.SELL, time_in_force=TimeInForce.DAY,
                                           extended_hours=True)
            else:
                log(f"VENTAS: {ticker} - {info_posicion} - beneficio {beneficio_pct:.2f}%, MACD 5min BAJISTA -> VENDIENDO (orden a mercado).")
                orden = MarketOrderRequest(symbol=ticker, qty=cantidad, side=OrderSide.SELL,
                                            time_in_force=TimeInForce.DAY)

            cancelar_ordenes_abiertas(ticker)
            trade = _trading_client.submit_order(order_data=orden)
            estado = esperar_estado_final_orden(trade.id)
            log(f"VENTAS: {ticker} - orden colocada, estado: {estado}")
            if estado == "filled":
                cantidad_real, precio_real = obtener_ejecucion_real(trade.id, cantidad, precio_actual)
                registrar_operacion_historial(ticker, "VENTA", cantidad_real, precio_real,
                                               coste_medio=coste_medio, beneficio_pct=beneficio_pct)
                notificar_telegram(f"🔴 VENTA <b>{ticker}</b>: {formato_es(cantidad_real, 4)} acciones a "
                                    f"{formato_es(precio_real)} USD (beneficio {formato_es(beneficio_pct, signo=True)}%)")
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
                notificar_telegram(f"🟢 COMPRA <b>{ticker}</b>: {formato_es(cantidad_real, 4)} acciones a {formato_es(precio_real)} USD")
        except Exception as e:
            log(f"COMPRAS: {ticker} - ERROR inesperado al procesar la señal de compra: {type(e).__name__}: {e}. Se omite.")
            errores += 1

    log(f"COMPRAS: {analizados} analizados, {senales} señales de compra, {errores} errores.")


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


TRAMO_ESPERA_LARGA_SEGUNDOS = 60  # bastante por debajo de UMBRAL_CONGELACION_SEGUNDOS (20 min)


def esperar_en_tramos(segundos_totales):
    """Espera el numero de segundos indicado, pero en tramos cortos que
    refrescan el latido en cada uno (actualizar_latido()). Un unico
    time.sleep() largo (p.ej. los ~7-8 horas que el mercado esta cerrado de
    noche) deja pasar mas de UMBRAL_CONGELACION_SEGUNDOS sin dar señal de
    vida, y el vigilante interno lo confunde con una congelacion real y mata
    el proceso -bug real visto en produccion: 'fuera de horario, esperando
    464 minutos' seguido de '[VIGILANTE] 20 minutos sin señal de vida' a los
    20 minutos exactos-. Con tramos de 60s (bien por debajo del umbral de 20
    min) esto no puede volver a pasar."""
    restante = segundos_totales
    while restante > 0:
        tramo = min(TRAMO_ESPERA_LARGA_SEGUNDOS, restante)
        time.sleep(tramo)
        actualizar_latido()
        restante -= tramo


def evitar_suspension_windows():
    try:
        import ctypes
        ES_CONTINUOUS = 0x80000000
        ES_SYSTEM_REQUIRED = 0x00000001
        ES_DISPLAY_REQUIRED = 0x00000002
        ES_AWAYMODE_REQUIRED = 0x00000040
        ctypes.windll.kernel32.SetThreadExecutionState(
            ES_CONTINUOUS | ES_SYSTEM_REQUIRED | ES_DISPLAY_REQUIRED | ES_AWAYMODE_REQUIRED
        )
        log("Suspension automatica de Windows desactivada mientras el bot este en marcha.")
    except Exception:
        pass  # no es Windows, o no se pudo aplicar; no es critico (p.ej. en un servidor Linux)


def main():
    evitar_suspension_windows()
    escribir_pid()
    actualizar_latido()
    threading.Thread(target=vigilante_congelacion, daemon=True).start()

    modo_texto = "PAPER (simulado)" if ALPACA_PAPER else "REAL - DINERO REAL"
    log("#" * 60)
    log(f"BOT ALPACA - MODO DE CUENTA: {modo_texto}")
    if not ALPACA_PAPER:
        log("ATENCION: esta es una cuenta REAL. Las ordenes de este bot son DINERO REAL, no una simulacion.")
    log("#" * 60)

    resumenes_enviados_hoy = set()

    while True:
        try:
            if not es_horario_operativo():
                segundos_espera = segundos_hasta_apertura()
                minutos_espera = segundos_espera / 60
                log(f"Fuera de horario operativo (4:00-20:00 ET). Esperando {minutos_espera:.0f} "
                    f"minutos hasta la proxima apertura...")
                esperar_en_tramos(segundos_espera)
                continue

            hoy = datetime.now(ZONA_NY).date()
            ahora_ny = datetime.now(ZONA_NY).time()
            if ahora_ny >= HORA_CIERRE_EXTENDIDO_US and hoy not in resumenes_enviados_hoy:
                try:
                    generar_resumen()
                except Exception as e:
                    log(f"RESUMEN: error al generar el resumen: {type(e).__name__}: {e}")
                resumenes_enviados_hoy.add(hoy)

            inicio_ciclo = time.monotonic()
            log("=" * 60)
            log("Iniciando nuevo ciclo de revision.")
            try:
                revisar_ventas()
            except Exception as e:
                log(f"ERROR inesperado en revisar_ventas: {type(e).__name__}: {e}")
            try:
                revisar_compras()
            except Exception as e:
                log(f"ERROR inesperado en revisar_compras: {type(e).__name__}: {e}")

            duracion_ciclo = time.monotonic() - inicio_ciclo
            log("Ciclo completado.")
            espera = max(INTERVALO_SEGUNDOS - duracion_ciclo, 0)
            if duracion_ciclo > INTERVALO_SEGUNDOS:
                log(f"AVISO: el ciclo ha tardado {duracion_ciclo:.0f}s, mas que el intervalo "
                    f"configurado ({INTERVALO_SEGUNDOS}s). Se pasa a la siguiente revision sin esperar.")
            else:
                log(f"Ciclo completado en {duracion_ciclo:.0f}s. Esperando {espera:.0f}s hasta la siguiente revision...")
            time.sleep(espera)
            actualizar_latido()
        except KeyboardInterrupt:
            log("Detenido por el usuario (Ctrl+C).")
            break
        except Exception as e:
            log(f"ERROR FATAL fuera del ciclo principal: {type(e).__name__}: {e}. "
                f"Reiniciando el ciclo en 15 segundos...")
            time.sleep(15)


if __name__ == "__main__":
    main()
