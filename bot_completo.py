"""
BOT COMPLETO - bucle automatico cada 4 minutos. Soporta estos mercados:
  - US (NYSE/Nasdaq, en USD): premercado 4:00-9:30 ET + mercado regular 9:30-16:00 ET +
    postmercado 16:00-20:00 ET. En premercado se compra Y se vende con normalidad (orden
    limitada al precio exacto, no a mercado: la liquidez es mucho menor). En postmercado
    SOLO se vende (tambien con orden limitada al precio exacto), no se compra -decision
    explicita del usuario, para no abrir posiciones nuevas con la liquidez tan baja de esa
    franja, pero sin bloquear la salida de posiciones que ya tocaria cerrar-. Las ventanas
    de "no comprar antes del cierre" y "venta forzada antes del cierre" siguen ancladas al
    cierre REGULAR (16:00 ET), sin cambios.
  - HK (Hong Kong Stock Exchange, en HKD): sesion 9:30-16:00 hora de Hong Kong
    (simplificado, ignora la pausa de mediodia real del mercado).
  - KR (Korea Exchange / KRX, en KRW): sesion 9:00-15:30 hora de Corea.

EU (Euronext / Borsa Italiana, en EUR) esta definido en el codigo (ACTIVOS_EU)
pero DESACTIVADO: pendiente de contratar la suscripcion de datos de mercado
de Europa en IBKR. Para reactivarlo, añade ACTIVOS_EU a la lista ACTIVOS y
vuelve a incluir es_horario_operativo("EU") en la comprobacion de main().

En cada ciclo:
  1. Revisa las posiciones abiertas y vende las que cumplan la regla de venta
     (a partir de +0.5% de beneficio, vender si el MACD de 5 min esta bajista;
     sin stop loss de perdida).
  2. Escanea todos los valores buscando senal de COMPRA (MACD en 7 temporalidades,
     con la excepcion de "solo 1 de 7 en contra"; o directamente si las 4
     temporalidades cortas -1min, 5min, 15min y 30min- estan todas alcistas).
     Cada valor solo se analiza si su mercado esta en horario operativo en
     ese momento.
  3. Compra (hasta 1000 EUR o equivalente) cualquier valor con senal de
     COMPRA, respetando el limite del 15% del valor total de la cartera por
     valor (calculado en USD equivalente). En el mercado US se permite
     comprar fracciones de accion (hasta 4 decimales); en HK y KR, donde
     IBKR no admite fracciones, se compran acciones/lotes enteros con
     redondeo hacia abajo.

Ante fallos de datos (p.ej. error 162 "sesion conectada desde otra IP"), se
reintenta automaticamente antes de omitir el valor.

Se detiene con Ctrl+C. Todo el log se imprime en pantalla con fecha y hora.

IMPORTANTE: este script envia ordenes REALES (aunque en cuenta paper).
Los tipos de cambio EUR/USD, USD/HKD y USD/KRW son aproximados y fijos;
actualizalos manualmente si quieres mas precision.
Revisa bien la configuracion antes de dejarlo corriendo desatendido.
"""

import json
import math
import os
import threading
import time
from datetime import date, datetime, time as dt_time, timedelta, timezone
from collections import defaultdict
from zoneinfo import ZoneInfo

import pandas as pd
import requests
from ib_async import IB, Stock, Crypto, Forex, MarketOrder, LimitOrder, ExecutionFilter

# --- Notificaciones a Telegram (opcional, igual que en bot_alpaca.py) ---
# Si no se configuran estas dos variables, notificar_telegram() simplemente no
# hace nada -el bot funciona igual sin Telegram, esto es un extra opcional-.
# Ver NOTES.md para como crear el bot de Telegram y conseguir estos valores.
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
TELEGRAM_TIMEOUT_SEGUNDOS = 10


def formato_es(numero, decimales=2, signo=False):
    """Formatea un numero al estilo español (punto para miles, coma para
    decimales: 1234.5 -> '1.234,50'), igual que en bot_alpaca.py."""
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

# --- Horarios por mercado ---
ZONA_NY = ZoneInfo("America/New_York")
HORA_INICIO_US = dt_time(4, 0)             # 4:00 ET (inicio del premercado)
HORA_APERTURA_REGULAR_US = dt_time(9, 30)  # 9:30 ET (apertura de la sesion regular)
HORA_CIERRE_US = dt_time(16, 0)            # 16:00 ET (cierre regular; sigue siendo la
                                            # referencia de "no comprar antes del cierre" y
                                            # "venta forzada antes del cierre", sin cambios)
HORA_CIERRE_EXTENDIDO_US = dt_time(20, 0)  # 20:00 ET (fin del postmercado)

ZONA_EU = ZoneInfo("Europe/Paris")
HORA_INICIO_EU = dt_time(9, 0)     # 9:00 hora de Paris/Amsterdam/Bruselas/Milan
HORA_CIERRE_EU = dt_time(17, 30)   # 17:30 hora de Paris/Amsterdam/Bruselas/Milan

ZONA_HK = ZoneInfo("Asia/Hong_Kong")
HORA_INICIO_HK = dt_time(9, 30)    # 9:30 hora de Hong Kong (simplificado, ignora pausa de mediodia)
HORA_CIERRE_HK = dt_time(16, 0)    # 16:00 hora de Hong Kong

ZONA_KR = ZoneInfo("Asia/Seoul")
HORA_INICIO_KR = dt_time(9, 0)     # 9:00 hora de Corea
HORA_CIERRE_KR = dt_time(15, 30)   # 15:30 hora de Corea

# --- Festivos del mercado US (NYSE/Nasdaq) - peticion del usuario, sept.
# 2026: "el bot puede identificar los dias festivos en US para no operar
# ese dia?". Calculados por REGLA (no una lista fija que haya que
# mantener a mano cada año) siguiendo el calendario oficial de festivos de
# NYSE: Año Nuevo, Martin Luther King Jr. Day (3er lunes de enero),
# Washington's Birthday (3er lunes de febrero), Good Friday (viernes antes
# de Pascua), Memorial Day (ultimo lunes de mayo), Juneteenth (19 de junio,
# festivo NYSE desde 2022), Independence Day (4 de julio), Labor Day (1er
# lunes de septiembre), Thanksgiving (4o jueves de noviembre) y Navidad (25
# de diciembre). Cuando el festivo cae en sabado se observa el viernes
# anterior; si cae en domingo, el lunes siguiente (regla estandar de NYSE).
# NO cubre HK ni KR (calendario lunar, no calculable por regla simple) -
# limitacion ya documentada, esos mercados solo comprueban fin de semana.
def _domingo_pascua(year):
    """Domingo de Pascua (calendario gregoriano) via el algoritmo de
    Meeus/Jones/Butcher. Necesario para Good Friday (Pascua - 2 dias)."""
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
    """n-esima ocurrencia (1=primera) de un dia de la semana (0=lunes,
    ..., 6=domingo) dentro de ese mes/año."""
    d = date(year, month, 1)
    primero = d + timedelta(days=(weekday_objetivo - d.weekday()) % 7)
    return primero + timedelta(weeks=n - 1)


def _ultimo_dia_semana(year, month, weekday_objetivo):
    """Ultima ocurrencia de un dia de la semana dentro de ese mes/año."""
    siguiente_mes = date(year + 1, 1, 1) if month == 12 else date(year, month + 1, 1)
    d = siguiente_mes - timedelta(days=1)
    while d.weekday() != weekday_objetivo:
        d -= timedelta(days=1)
    return d


def _fecha_observada_nyse(d):
    """Si el festivo cae en fin de semana, la fecha que NYSE observa en su
    lugar (sabado -> viernes anterior, domingo -> lunes siguiente)."""
    if d.weekday() == 5:
        return d - timedelta(days=1)
    if d.weekday() == 6:
        return d + timedelta(days=1)
    return d


_cache_festivos_nyse = {}


def festivos_nyse(year):
    """Devuelve el set de fechas (date) festivas de NYSE/Nasdaq para ese
    año, calculadas por regla y cacheadas (no cambian una vez calculadas)."""
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

# --- Reintentos ante el error 162 (sesion de datos conectada desde otra IP) ---
INTENTOS_MAXIMOS = 3
ESPERA_ENTRE_INTENTOS_SEGUNDOS = 15

# --- Cortacircuitos ante una caida generalizada de los datos de mercado ---
# Si el socket API sigue "conectado" (ib.isConnected() y reqCurrentTime()
# funcionan) pero TWS/IB Gateway ha perdido la conexion con los "market data
# farms" de IBKR, reqHistoricalData falla en TODOS los valores sin
# excepcion. Sin este cortacircuitos, el bot se queda horas reintentando
# 3 veces x 15s por cada valor de la lista, uno detras de otro, sin avisar
# claramente de que el problema es de datos de mercado (visto en produccion:
# un ciclo tardo 3003s). Tras UMBRAL_FALLOS_SEGUIDOS_DATOS valores SEGUIDOS
# sin ningun dato en ninguno de sus intentos, se deja de reintentar (una
# sola llamada rapida por valor) y se avisa una vez, hasta que algun valor
# vuelva a traer datos.
UMBRAL_FALLOS_SEGUIDOS_DATOS = 8
_fallos_seguidos_datos = 0
_aviso_datos_caidos_emitido = False
_aviso_reconexion_fallida_emitido = False  # evita repetir el aviso de Telegram en cada vuelta mientras siga caida

# --- Reintentos de reconexion con IB Gateway tras un corte de conexion ---
REINTENTOS_RECONEXION = 5
ESPERA_ENTRE_REINTENTOS_RECONEXION_SEGUNDOS = 15

# --- Ventanas de cierre de mercado ---
MINUTOS_SIN_COMPRAR_ANTES_CIERRE = 90    # 1.5 horas: no se compra nada en este margen antes del cierre
MINUTOS_VENTA_FORZADA_ANTES_CIERRE = 15  # ultimos 15 min: se vende lo que tenga entre +0.5% y +2% de beneficio
BENEFICIO_MAX_VENTA_FORZADA_PCT = 2.0     # por encima de este %, se deja correr con la logica normal del MACD

CIERRE_POR_MERCADO = {
    "US": (ZONA_NY, HORA_CIERRE_US),
    "HK": (ZONA_HK, HORA_CIERRE_HK),
    "KR": (ZONA_KR, HORA_CIERRE_KR),
}

APERTURA_POR_MERCADO = {
    "US": (ZONA_NY, HORA_INICIO_US),
    "HK": (ZONA_HK, HORA_INICIO_HK),
    "KR": (ZONA_KR, HORA_INICIO_KR),
}

MINUTOS_ANTES_DE_APERTURA_PARA_DESPERTAR = 15  # con todos los mercados cerrados, despertar 15 min antes de la mas proxima

CURRENCY_A_MERCADO = {"USD": "US", "HKD": "HK", "KRW": "KR", "EUR": "EU"}
# NOTA: la cripto (PAXOS/ZEROHASH) tambien cotiza en USD, asi que esta tabla NO sirve
# para distinguirla de US -ver mercado_de_posicion()/contrato_pertenece_a_mercado(),
# que comprueban primero secType == "CRYPTO" antes de mirar la divisa-.

# --- Criptomonedas (PAXOS/ZEROHASH, via IBKR) ---
# Horario "Crypto Basic" (nivel por defecto en la mayoria de cuentas nuevas):
# opera de domingo 3:00 AM ET a viernes 4:00 PM ET (cerrado la mayor parte
# del fin de semana). Si tu cuenta tiene el nivel "Crypto Plus" (24/7,
# fines de semana incluidos), pon esto a True.
# Confirmado por el usuario (sept. 2026): asumir Crypto Plus (24/7). Si en
# algun momento se comprueba que la cuenta es en realidad "Crypto Basic",
# poner esto a False.
CRYPTO_24_7 = True
HORA_CIERRE_CRYPTO_VIERNES = dt_time(16, 0)   # viernes 16:00 ET
HORA_APERTURA_CRYPTO_DOMINGO = dt_time(3, 0)  # domingo 3:00 ET

# Comision de IBKR en cripto (via Paxos): 0.12%-0.18% del valor operado (se
# usa la tasa mas alta citada, 0.18%, por prudencia), minimo 1.75 USD, con
# tope del 1% del valor operado (protege a las operaciones pequeñas de que
# el minimo fijo se coma un % desproporcionado). Fuente: pagina oficial de
# precios de IBKR (ver ALPACA_NOTES.md... perdon, NOTES.md, seccion cripto).
COMISION_CRIPTO_PCT = 0.18 / 100
COMISION_CRIPTO_MINIMA_USD = 1.75
COMISION_CRIPTO_MAX_PCT = 1.0 / 100
VALOR_MINIMO_OPERACION_CRIPTO_USD = 5.0  # margen de seguridad razonable, ajustable
DECIMALES_FRACCION_CRIPTO = 6  # mas precision que acciones (DECIMALES_FRACCION=4): BTC/ETH suelen necesitarla

# --- Configuracion general ---
IMPORTE_EUROS = 1000
IMPORTE_EUROS_HK = 3500  # HK opera en lotes fijos (a veces 500+ acciones): presupuesto mayor para poder cubrirlos
TIPO_CAMBIO_EUR_USD = 1.14   # Valor de arranque; se refresca solo con el precio real de mercado
                              # (Forex EUR.USD via IBKR) cada INTERVALO_ACTUALIZACION_TIPO_CAMBIO_SEGUNDOS,
                              # ver actualizar_tipo_cambio_eur_usd() - peticion del usuario, sept. 2026:
                              # antes era un valor fijo que habia que actualizar a mano.
TIPO_CAMBIO_USD_HKD = 7.80   # 1 USD = 7.80 HKD (aprox, el HKD esta fijado al USD) - sigue fijo a mano
TIPO_CAMBIO_USD_KRW = 1480   # 1 USD = 1480 KRW (aprox) - sigue fijo a mano
UMBRAL_BENEFICIO_PCT = 0.5  # % minimo de beneficio para activar la vigilancia de venta
UMBRAL_BENEFICIO_CRYPTO_PCT = 0.3  # cripto: umbral mas bajo que acciones (peticion del usuario, sept.
                                    # 2026) - ya es NETO de comision (beneficio_pct la resta antes de
                                    # compararlo), y cripto es 24/7 y mas rapida: esperar al 0.5% de
                                    # acciones puede dejar escapar subidas que luego revierten.
MARGEN_ORDEN_LIMITADA_VENTA_PCT = 0.2  # % por debajo del precio actual al vender con orden limitada
INTERVALO_SEGUNDOS = 4 * 60  # 4 minutos: US/HK/KR
CRYPTO_INTERVALO_SEGUNDOS = 60  # cripto revisa cada 1 minuto (peticion del usuario, sept. 2026):
                                 # 24/7 y mas rapida que acciones, un ciclo cada 4 min puede dejar
                                 # escapar movimientos cortos. Corre en su propia cadencia dentro
                                 # de main(), independiente del ciclo de US/HK/KR (ver ciclo_completo()
                                 # con el parametro `mercados`).
LIMITE_EXPOSICION_PCT = 15   # % maximo del total de cartera (en USD equivalente) por valor

# Bug/limite real de produccion (sept. 2026): IBKR rechaza cualquier compra de
# cripto que haga que el conjunto de posiciones de cripto supere el 30% del
# equity total de la cuenta (o 3 millones USD, lo que sea menor) - "Error 201:
# Order rejected - ... would cause your crypto account(s) to exceed the
# lesser of: 30% of your total account equity...". Esto es un LIMITE PROPIO
# de IBKR (politica de riesgo/regulatoria de la cuenta, no un bug de este
# codigo) y es independiente de LIMITE_EXPOSICION_PCT (que es por VALOR
# individual, no por el conjunto de toda la cripto). Se deja un margen de
# seguridad (25% en vez del 30% real) para no ir pegado al limite exacto de
# IBKR y encadenar rechazos en cada intento.
LIMITE_EXPOSICION_CRYPTO_TOTAL_PCT = 25

# Tope de posiciones abiertas SIMULTANEAS (todas las abiertas a la vez, en
# cualquier mercado) y comprobacion de caja disponible antes de comprar
# (peticion del usuario, sept. 2026): sin esto, un dia de tendencia fuerte
# con muchas señales a la vez podria intentar abrir muchas posiciones
# nuevas de golpe sin comprobar si la cuenta tiene fondos de verdad, o sin
# ningun limite al numero total de valores distintos en cartera a la vez.
# No sustituye a LIMITE_EXPOSICION_PCT (por VALOR de cada posicion) ni a
# LIMITE_EXPOSICION_CRYPTO_TOTAL_PCT (agregado de cripto): es un tercer
# limite independiente, sobre el NUMERO de posiciones y la CAJA real.
MAX_POSICIONES_ABIERTAS = 12

# --- Fracciones de accion ---
# IBKR solo admite comprar fracciones de accion en el mercado US (y solo para
# una parte de los valores, los que tengan ese permiso habilitado). En HK y
# KR las acciones se compran siempre en unidades/lotes enteros.
FRACCIONABLE_POR_MERCADO = {"US": True, "EU": False, "HK": False, "KR": False, "CRYPTO": True}
DECIMALES_FRACCION = 4  # precision al calcular la cantidad fraccionaria a comprar
VALOR_MINIMO_OPERACION_FRACCIONARIA_USD = 1.0  # por debajo de esto, IBKR rechaza la orden

# --- Lista de valores: mercado US (NYSE, SMART, USD) ---
ACTIVOS_US = [
    {"ticker": t, "exchange": "SMART", "currency": "USD", "mercado": "US"}
    for t in ["AAPL",   # Apple Inc.
              "MSFT",   # Microsoft Corporation
              "NVDA",   # NVIDIA Corporation
              "AMZN",   # Amazon.com, Inc.
              "GOOGL",  # Alphabet Inc.
              "META",   # Meta Platforms, Inc.
              "TSLA",   # Tesla, Inc.
              "AVGO",   # Broadcom Inc.
              "AMD",    # Advanced Micro Devices, Inc.
              "NFLX",   # Netflix, Inc.
              "INTC",   # Intel Corporation
              "QCOM",   # Qualcomm Incorporated
              "CSCO",   # Cisco Systems, Inc.
              "SMCI",   # Super Micro Computer, Inc.
              "JPM",    # JPMorgan Chase & Co.
              "BAC",    # Bank of America
              "WFC",    # Wells Fargo & Company
              "C",      # Citigroup
              "V",      # Visa
              "MA",     # Mastercard
              "XOM",    # Exxon Mobil
              "CVX",    # Chevron
              "WMT",    # Walmart
              "DIS",    # The Walt Disney Company
              "KO",     # The Coca-Cola Company
              "JNJ",    # Johnson & Johnson
              "PFE",    # Pfizer
              "F",      # Ford Motor Company
              "T",      # AT&T Inc.
              "GE"]     # GE Aerospace
]

# --- Lista de valores: mercado EU (Euronext / Borsa Italiana, EUR) ---
# exchange segun IBKR: AEB=Amsterdam, SBF=Paris, ENEXT.BE=Bruselas, BVME=Milan (Borsa Italiana)
ACTIVOS_EU = [
    {"ticker": "ASML", "exchange": "AEB",     "currency": "EUR", "mercado": "EU"},
    {"ticker": "MC",    "exchange": "SBF",     "currency": "EUR", "mercado": "EU"},
    {"ticker": "TTE",   "exchange": "SBF",     "currency": "EUR", "mercado": "EU"},
    {"ticker": "RMS",   "exchange": "SBF",     "currency": "EUR", "mercado": "EU"},
    {"ticker": "OR",    "exchange": "SBF",     "currency": "EUR", "mercado": "EU"},
    {"ticker": "SU",    "exchange": "SBF",     "currency": "EUR", "mercado": "EU"},
    {"ticker": "SAN",   "exchange": "SBF",     "currency": "EUR", "mercado": "EU"},
    {"ticker": "AIR",   "exchange": "SBF",     "currency": "EUR", "mercado": "EU"},
    {"ticker": "AI",    "exchange": "SBF",     "currency": "EUR", "mercado": "EU"},
    {"ticker": "SAF",   "exchange": "SBF",     "currency": "EUR", "mercado": "EU"},
    {"ticker": "ABI",   "exchange": "ENEXT.BE","currency": "EUR", "mercado": "EU"},
    {"ticker": "RACE",  "exchange": "BVME",    "currency": "EUR", "mercado": "EU"},
    {"ticker": "BNP",   "exchange": "SBF",     "currency": "EUR", "mercado": "EU"},
    {"ticker": "PRX",   "exchange": "AEB",     "currency": "EUR", "mercado": "EU"},
    {"ticker": "STLAM", "exchange": "BVME",    "currency": "EUR", "mercado": "EU"},
    {"ticker": "UCG",   "exchange": "BVME",    "currency": "EUR", "mercado": "EU"},
    {"ticker": "ISP",   "exchange": "BVME",    "currency": "EUR", "mercado": "EU"},
    {"ticker": "CS",    "exchange": "SBF",     "currency": "EUR", "mercado": "EU"},
    {"ticker": "INGA",  "exchange": "AEB",     "currency": "EUR", "mercado": "EU"},
    {"ticker": "ARGX",  "exchange": "ENEXT.BE","currency": "EUR", "mercado": "EU"},
]

# --- Lista de valores: mercado HK (Hong Kong Stock Exchange, HKD) ---
# Principales valores por capitalizacion/liquidez, excluyendo dobles
# cotizaciones en Singapur/ADR en US.
ACTIVOS_HK = [
    {"ticker": "1299", "exchange": "SEHK", "currency": "HKD", "mercado": "HK"},  # AIA
    {"ticker": "388",  "exchange": "SEHK", "currency": "HKD", "mercado": "HK"},  # HKEX
    {"ticker": "2388", "exchange": "SEHK", "currency": "HKD", "mercado": "HK"},  # BOC Hong Kong
    {"ticker": "16",   "exchange": "SEHK", "currency": "HKD", "mercado": "HK"},  # Sun Hung Kai Properties
    {"ticker": "2259", "exchange": "SEHK", "currency": "HKD", "mercado": "HK"},  # Zijin Gold International
    {"ticker": "992",  "exchange": "SEHK", "currency": "HKD", "mercado": "HK"},  # Lenovo
    {"ticker": "19",   "exchange": "SEHK", "currency": "HKD", "mercado": "HK"},  # Swire Pacific
    {"ticker": "1",    "exchange": "SEHK", "currency": "HKD", "mercado": "HK"},  # CK Hutchison Holdings
    {"ticker": "1109", "exchange": "SEHK", "currency": "HKD", "mercado": "HK"},  # China Resources Land
    {"ticker": "762",  "exchange": "SEHK", "currency": "HKD", "mercado": "HK"},  # China Unicom
    {"ticker": "700",  "exchange": "SEHK", "currency": "HKD", "mercado": "HK"},  # Tencent Holdings
    {"ticker": "9988", "exchange": "SEHK", "currency": "HKD", "mercado": "HK"},  # Alibaba Group-W
    {"ticker": "5",    "exchange": "SEHK", "currency": "HKD", "mercado": "HK"},  # HSBC Holdings
    {"ticker": "3690", "exchange": "SEHK", "currency": "HKD", "mercado": "HK"},  # Meituan-W
    {"ticker": "939",  "exchange": "SEHK", "currency": "HKD", "mercado": "HK"},  # China Construction Bank
    {"ticker": "1398", "exchange": "SEHK", "currency": "HKD", "mercado": "HK"},  # ICBC
    {"ticker": "1810", "exchange": "SEHK", "currency": "HKD", "mercado": "HK"},  # Xiaomi Group-W
    {"ticker": "3988", "exchange": "SEHK", "currency": "HKD", "mercado": "HK"},  # Bank of China
    {"ticker": "941",  "exchange": "SEHK", "currency": "HKD", "mercado": "HK"},  # China Mobile
    {"ticker": "2318", "exchange": "SEHK", "currency": "HKD", "mercado": "HK"},  # Ping An Insurance
    {"ticker": "1024", "exchange": "SEHK", "currency": "HKD", "mercado": "HK"},  # Kuaishou Technology
    {"ticker": "9618", "exchange": "SEHK", "currency": "HKD", "mercado": "HK"},  # JD.com-SW
    {"ticker": "1211", "exchange": "SEHK", "currency": "HKD", "mercado": "HK"},  # BYD Company
    {"ticker": "883",  "exchange": "SEHK", "currency": "HKD", "mercado": "HK"},  # CNOOC
    {"ticker": "857",  "exchange": "SEHK", "currency": "HKD", "mercado": "HK"},  # PetroChina
    {"ticker": "2628", "exchange": "SEHK", "currency": "HKD", "mercado": "HK"},  # China Life Insurance
    {"ticker": "981",  "exchange": "SEHK", "currency": "HKD", "mercado": "HK"},  # SMIC
    {"ticker": "3968", "exchange": "SEHK", "currency": "HKD", "mercado": "HK"},  # China Merchants Bank
]

# --- Lista de valores: mercado KR (Korea Exchange / KRX, KRW) ---
# Top 10 por capitalizacion, excluyendo ADRs cotizados en US (KB, SHG, SKM, KEP...).
ACTIVOS_KR = [
    {"ticker": "005930", "exchange": "KRX", "currency": "KRW", "mercado": "KR"},  # Samsung Electronics
    {"ticker": "000660", "exchange": "KRX", "currency": "KRW", "mercado": "KR"},  # SK Hynix
    {"ticker": "005380", "exchange": "KRX", "currency": "KRW", "mercado": "KR"},  # Hyundai Motor
    {"ticker": "402340", "exchange": "KRX", "currency": "KRW", "mercado": "KR"},  # SK Square
    {"ticker": "373220", "exchange": "KRX", "currency": "KRW", "mercado": "KR"},  # LG Energy Solution
    {"ticker": "028260", "exchange": "KRX", "currency": "KRW", "mercado": "KR"},  # Samsung C&T
    {"ticker": "032830", "exchange": "KRX", "currency": "KRW", "mercado": "KR"},  # Samsung Life Insurance
    {"ticker": "329180", "exchange": "KRX", "currency": "KRW", "mercado": "KR"},  # HD Hyundai Heavy Industries
    {"ticker": "012330", "exchange": "KRX", "currency": "KRW", "mercado": "KR"},  # Hyundai Mobis
    {"ticker": "000270", "exchange": "KRX", "currency": "KRW", "mercado": "KR"},  # Kia
]

# --- Lista de valores: criptomonedas (USD) ---
# IBKR ofrece cripto a traves de proveedores/exchanges distintos ("PAXOS",
# "ZEROHASH"...), con contratos/conId DIFERENTES incluso para la misma
# moneda, y solo uno de ellos tiene datos de mercado activos para una cuenta
# concreta. NO se hardcodea aqui: no fue posible acertarlo por prueba y
# error (ver HISTORIA REAL mas abajo), asi que se deja que IBKR resuelva el
# contrato correcto el mismo, sin indicar ningun exchange -ver
# crear_contrato(), que para CRYPTO deja exchange="" y usa
# ib.qualifyContracts() (ya se llama justo despues en analizar_activo) para
# que sea IBKR quien decida cual es el contrato valido para esta cuenta,
# exactamente igual que ya se hacia para completar el contrato de una
# posicion ya abierta en revisar_ventas.
#
# HISTORIA REAL de este bug (sept. 2026, cuenta U25302975) - se deja
# documentada entera porque cada paso parecia razonable con la informacion
# que habia en ese momento, y aun asi todos los intentos de ADIVINAR un
# exchange concreto fallaron:
# 1) exchange="PAXOS" (valor original, conId resuelto 479624278):
#    reqHistoricalData se quedaba colgado con TimeoutError en TODOS los
#    intentos, sin ningun error de permisos.
# 2) Una captura de pantalla de "suscripciones de datos" mostraba "ZEROHASHE
#    Cryptocurrency" -> se interpreto (paso en falso) que la cuenta usaba
#    ZEROHASH, y se cambio a exchange="ZEROHASH" (conId 541686651). El
#    TimeoutError PERSISTIO igual.
# 3) Se añadio logging de errores reales de la API (on_error_ib): la causa
#    de (1) y (2) nunca fue el exchange, sino whatToShow='TRADES' en vez de
#    'AGGTRADES' para contratos CRYPTO (ver pedir_velas()).
# 4) Con AGGTRADES + exchange="ZEROHASH": la VENTA de la posicion real de la
#    usuaria (contrato de su posicion, resuelto por IBKR via
#    qualifyContracts a partir de su conId REAL, distinto de 479624278 y de
#    541686651 -un tercer conId- ver revisar_ventas) SI obtuvo precio
#    correctamente. Pero la COMPRA (contrato construido a mano con
#    exchange="ZEROHASH") fallo con "Error 162: No market data permissions
#    for ZEROHASH CRYPTO" -> se interpreto (otro paso en falso) que la
#    cuenta debia estar en PAXOS.
# 5) Se cambio a exchange="PAXOS": la COMPRA volvio a fallar, esta vez con
#    "Error 162: No market data permissions for PAXOS CRYPTO" (conId
#    479624278 de nuevo). Es decir: NI "PAXOS" NI "ZEROHASH", tal cual los
#    resuelve IBKR por defecto para BTC/USD, tienen datos en esta cuenta -
#    solo el conId especifico de la posicion real de la usuaria (un tercero,
#    desconocido de antemano) los tiene. No hay forma de adivinar ese conId
#    a mano de forma fiable.
# 6) Solucion definitiva: dejar de especificar exchange al construir el
#    contrato para comprar (igual que ya se hacia, por necesidad, al
#    vender), y confiar en que ib.qualifyContracts() -que ya se llama justo
#    despues en analizar_activo()- resuelva el contrato realmente valido
#    para la cuenta conectada, sea cual sea su conId/exchange real.
EXCHANGE_CRYPTO = ""  # se deja vacio a proposito, ver historia arriba: NO hardcodear un exchange
ACTIVOS_CRYPTO = [
    {"ticker": t, "exchange": EXCHANGE_CRYPTO, "currency": "USD", "mercado": "CRYPTO"}
    for t in ["BTC", "ETH", "LTC", "BCH", "SOL", "LINK"]
]

# HK excluido (agosto 2026): con el capital actual (~300 EUR, limite de exposicion 15% => ~45
# USD por posicion), ningun valor de ACTIVOS_HK cabe en 1 lote minimo (lotes fijos de 100-2000
# acciones) y ademas la comision minima de IBKR por orden en HK (~2.25 USD) se comeria ~10% del
# valor de la posicion en cada operacion. Ver NOTES.md ("Comisiones estimadas") para el detalle.
# Las posiciones de HK que ya se tengan abiertas se siguen vendiendo con normalidad (revisar_ventas
# no depende de esta lista); solo se deja de ESCANEAR HK en busca de nuevas señales de compra.
# EU sigue excluido: pendiente de suscripcion de datos de mercado.
ACTIVOS = ACTIVOS_US + ACTIVOS_KR + ACTIVOS_CRYPTO

TEMPORALIDADES = [
    {"nombre": "1 minuto",   "barSize": "1 min",   "duration": "1 D",  "tipo": "corta"},
    {"nombre": "5 minutos",  "barSize": "5 mins",  "duration": "2 D",  "tipo": "corta"},
    {"nombre": "15 minutos", "barSize": "15 mins", "duration": "5 D",  "tipo": "corta"},
    {"nombre": "30 minutos", "barSize": "30 mins", "duration": "10 D", "tipo": "corta"},
    {"nombre": "1 hora",     "barSize": "1 hour",  "duration": "1 M",  "tipo": "corta"},
    {"nombre": "1 dia",      "barSize": "1 day",   "duration": "1 Y",  "tipo": "larga"},
    {"nombre": "1 semana",   "barSize": "1 week",  "duration": "5 Y",  "tipo": "larga"},
]

# Atajo de compra: si estas 4 temporalidades cortas estan todas alcistas,
# se compra sin mirar el resto (vease analizar_activo).
NOMBRES_4_CORTAS = ["1 minuto", "5 minutos", "15 minutos", "30 minutos"]

# Atajo de compra EXCLUSIVO de cripto (peticion del usuario, sept. 2026):
# ademas del atajo general de arriba (1/5/15/30 min), cripto comprueba
# TAMBIEN estas otras temporalidades propias (1/3/10/20 min) - si las 4
# salen alcistas, compra directamente, igual que el atajo general (ver
# atajo_cripto_alcista() y su uso en analizar_activo). La de "1 minuto" NO
# esta aqui: se reutiliza la ya calculada en TEMPORALIDADES/detalle (mismo
# barSize, mismo calculo), para no pedirla dos veces a IBKR.
TEMPORALIDADES_CRIPTO_ATAJO_EXTRA = [
    {"nombre": "3 minutos",  "barSize": "3 mins",  "duration": "1 D"},
    {"nombre": "10 minutos", "barSize": "10 mins", "duration": "2 D"},
    {"nombre": "20 minutos", "barSize": "20 mins", "duration": "2 D"},
]


# --- Vigilante de congelacion del proceso ---
# Se ha visto en produccion que una llamada bloqueante a IBKR (p.ej.
# reqContractDetails) puede quedarse colgada durante HORAS sin devolver
# nunca ni lanzar una excepcion, tipicamente tras un corte de red brusco
# (WinError 10054). Como eso pasa dentro del hilo principal, ningun
# try/except de nuestro propio codigo puede detectarlo ni recuperarse: el
# hilo esta literalmente parado, no lanzando errores.
#
# IMPORTANTE (aprendido en produccion): un hilo Python DENTRO del mismo
# proceso (vigilante_congelacion, mas abajo) NO es del todo fiable para
# esto. Si el hilo principal se queda atascado en una llamada de bajo nivel
# que no cede el turno (el "GIL" de Python), NINGUN otro hilo del mismo
# proceso puede ejecutarse tampoco -ni siquiera el vigilante-. Se vio
# exactamente esto: el aviso del vigilante no salio solo, hizo falta
# pulsar Ctrl+C para que "despertara". Por eso, ADEMAS del hilo interno
# (que sirve de primera linea de defensa por si acaso), se escribe un
# archivo de "latido" en disco que un proceso EXTERNO (fuera de Python del
# todo, ver vigilante_externo.ps1) puede vigilar de forma fiable, inmune a
# que el interprete de Python este bloqueado.
_ultimo_latido = time.monotonic()
_ultimo_latido_archivo = 0.0
UMBRAL_CONGELACION_SEGUNDOS = 20 * 60  # 20 min sin actividad -> se asume congelado
ARCHIVO_LATIDO = "latido_bot.txt"
ARCHIVO_PID = "bot.pid"
INTERVALO_MIN_ESCRITURA_LATIDO_SEGUNDOS = 10  # no reescribir el archivo en CADA log, basta cada 10s

# Parada limpia solicitada desde fuera (p.ej. /parar de telegram_bot_ibkr.py):
# un archivo "de señal" en vez de matar el proceso a la fuerza, para no
# interrumpir una orden a medio colocar. run.bot.bat comprueba este mismo
# archivo tras cada ejecucion para decidir si reiniciar el bucle o parar del
# todo (ver run.bot.bat).
ARCHIVO_DETENER = "detener_bot.flag"


def peticion_de_parada_pendiente():
    return os.path.exists(ARCHIVO_DETENER)


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
            pass  # si falla escribir el archivo no es motivo para romper nada mas


def escribir_pid():
    """Guarda el PID (identificador de proceso) actual en un archivo, para
    que vigilante_externo.ps1 sepa exactamente que proceso matar si detecta
    una congelacion, sin arriesgarse a matar otro python.exe distinto que
    pueda estar corriendo en el mismo ordenador."""
    try:
        with open(ARCHIVO_PID, "w", encoding="utf-8") as f:
            f.write(str(os.getpid()))
    except OSError as e:
        log(f"No se pudo escribir el archivo de PID ({ARCHIVO_PID}): {type(e).__name__}: {e}")


def vigilante_congelacion():
    """Hilo en segundo plano (daemon) que comprueba cada minuto si ha
    pasado demasiado tiempo desde la ultima señal de vida. Sirve como
    primera linea de defensa, pero NO es del todo fiable por si solo (ver
    nota mas arriba) -para una proteccion de verdad, usar tambien
    vigilante_externo.ps1 como proceso separado."""
    while True:
        time.sleep(60)
        inactividad = time.monotonic() - _ultimo_latido
        if inactividad > UMBRAL_CONGELACION_SEGUNDOS:
            print(f"\n[VIGILANTE] El programa lleva {inactividad / 60:.0f} minutos sin dar "
                  f"ninguna señal de vida: probablemente esta congelado dentro de una llamada "
                  f"a IBKR que nunca ha respondido. Forzando el cierre del proceso. Si no tienes "
                  f"un supervisor externo que lo reinicie automaticamente (script .bat en bucle, "
                  f"Tarea Programada, etc.), el bot se quedara parado hasta que lo reinicies tu "
                  f"a mano.", flush=True)
            notificar_telegram(f"🛑 El bot de IBKR lleva {inactividad / 60:.0f} min sin dar señal de vida "
                               f"(congelado, probablemente en una llamada a IBKR sin respuesta) y se ha "
                               f"forzado su cierre. Comprueba que run.bot.bat/el supervisor externo lo "
                               f"reinicie solo, o reinicialo a mano.")
            os._exit(1)


# Archivo de log en disco, ademas de la consola: run.bot.bat no redirige la
# salida a ningun archivo (se ve en su propia ventana de CMD), y en Windows
# no hay nada como journalctl para leerla desde fuera. Este archivo es lo
# que usa /log de telegram_bot_ibkr.py para poder consultar la actividad
# reciente desde el movil sin tener que mirar la ventana de CMD.
ARCHIVO_LOG = "bot_completo.log"
TAMANO_MAXIMO_LOG_BYTES = 5 * 1024 * 1024  # 5 MB: se trunca al alcanzarlo, ver _rotar_log_si_hace_falta()


def _rotar_log_si_hace_falta():
    """Si el archivo de log supera el tamaño maximo, se queda solo con la
    mitad final -evita que crezca sin limite en un bot que corre 24/7
    semanas seguidas-. No es una rotacion con archivos .1/.2/etc: para este
    uso (consulta rapida de actividad reciente desde el movil) basta con
    conservar lo mas reciente."""
    try:
        if os.path.getsize(ARCHIVO_LOG) <= TAMANO_MAXIMO_LOG_BYTES:
            return
        with open(ARCHIVO_LOG, "r", encoding="utf-8", errors="replace") as f:
            contenido = f.read()
        with open(ARCHIVO_LOG, "w", encoding="utf-8") as f:
            f.write(contenido[len(contenido) // 2:])
    except OSError:
        pass


_lineas_desde_ultima_rotacion = 0


def log(mensaje):
    global _lineas_desde_ultima_rotacion
    actualizar_latido()
    ahora = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    linea = f"[{ahora}] {mensaje}"
    print(linea)
    try:
        with open(ARCHIVO_LOG, "a", encoding="utf-8") as f:
            f.write(linea + "\n")
    except OSError:
        pass  # igual que con el latido: si falla escribir el archivo no debe romper nada mas
    _lineas_desde_ultima_rotacion += 1
    if _lineas_desde_ultima_rotacion >= 200:  # no comprobar el tamaño en CADA linea, basta cada 200
        _lineas_desde_ultima_rotacion = 0
        _rotar_log_si_hace_falta()


def es_horario_operativo(mercado):
    """True si el mercado indicado ('US', 'EU', 'HK' o 'KR') esta en horario
    operativo ahora mismo, de lunes a viernes. Para US, tambien tiene en
    cuenta los festivos de NYSE/Nasdaq (ver festivos_nyse()/es_festivo_us(),
    peticion del usuario sept. 2026) - EU/HK/KR siguen sin esta comprobacion
    (calendario de festivos no calculable por regla simple, limitacion ya
    documentada)."""
    if mercado == "US":
        ahora = datetime.now(ZONA_NY)
        if ahora.weekday() >= 5 or es_festivo_us(ahora.date()):
            return False
        return HORA_INICIO_US <= ahora.time() < HORA_CIERRE_EXTENDIDO_US
    elif mercado == "EU":
        ahora = datetime.now(ZONA_EU)
        if ahora.weekday() >= 5:
            return False
        return HORA_INICIO_EU <= ahora.time() < HORA_CIERRE_EU
    elif mercado == "HK":
        ahora = datetime.now(ZONA_HK)
        if ahora.weekday() >= 5:
            return False
        return HORA_INICIO_HK <= ahora.time() < HORA_CIERRE_HK
    elif mercado == "KR":
        ahora = datetime.now(ZONA_KR)
        if ahora.weekday() >= 5:
            return False
        return HORA_INICIO_KR <= ahora.time() < HORA_CIERRE_KR
    elif mercado == "CRYPTO":
        return es_horario_operativo_cripto()
    return False


def es_horario_operativo_cripto():
    """Horario de cripto en IBKR (via Paxos). Por defecto asume el nivel
    "Crypto Basic" (domingo 3:00 AM ET a viernes 4:00 PM ET, cerrado el
    resto del fin de semana) -el nivel por defecto en la mayoria de cuentas
    nuevas-. Si tu cuenta tiene el nivel "Crypto Plus" (24/7, fines de
    semana incluidos), pon CRYPTO_24_7 = True mas arriba en el archivo."""
    if CRYPTO_24_7:
        return True
    ahora = datetime.now(ZONA_NY)
    dia = ahora.weekday()  # lunes=0 ... domingo=6
    hora = ahora.time()
    if dia == 4 and hora >= HORA_CIERRE_CRYPTO_VIERNES:  # viernes tras el cierre
        return False
    if dia == 5:  # sabado: siempre cerrado en Crypto Basic
        return False
    if dia == 6 and hora < HORA_APERTURA_CRYPTO_DOMINGO:  # domingo antes de la apertura
        return False
    return True


def en_postmercado_us():
    """True si estamos en el postmercado de US (16:00-20:00 ET). En este
    tramo el bot solo compra, nunca vende (decision explicita del usuario:
    la liquidez es mucho menor que en sesion regular, y no se quiere
    arriesgar a salir de una posicion en esas condiciones)."""
    ahora = datetime.now(ZONA_NY)
    if ahora.weekday() >= 5 or es_festivo_us(ahora.date()):
        return False
    return HORA_CIERRE_US <= ahora.time() < HORA_CIERRE_EXTENDIDO_US


def fuera_de_sesion_regular_us():
    """True si el mercado US esta operativo (horario extendido 4:00-20:00
    ET) pero fuera de la sesion regular (9:30-16:00 ET) -es decir, en pre o
    postmercado-. En ese caso la liquidez es mucho menor, asi que las
    ordenes deben ser LIMITADAS al precio exacto (con outsideRth activado)
    en vez de a mercado, para no arriesgarse a una ejecucion a un precio muy
    distinto del que se vio al analizar la señal."""
    ahora = datetime.now(ZONA_NY)
    if ahora.weekday() >= 5 or es_festivo_us(ahora.date()):
        return False
    hora = ahora.time()
    en_premercado = HORA_INICIO_US <= hora < HORA_APERTURA_REGULAR_US
    en_postmercado = HORA_CIERRE_US <= hora < HORA_CIERRE_EXTENDIDO_US
    return en_premercado or en_postmercado


def minutos_hasta_cierre(mercado):
    """Devuelve los minutos que faltan para el cierre de ese mercado, o None
    si el mercado no esta operativo ahora mismo o no tiene cierre definido."""
    if mercado not in CIERRE_POR_MERCADO:
        return None
    if not es_horario_operativo(mercado):
        return None

    zona, hora_cierre = CIERRE_POR_MERCADO[mercado]
    ahora = datetime.now(zona)
    cierre_hoy = ahora.replace(hour=hora_cierre.hour, minute=hora_cierre.minute,
                                second=0, microsecond=0)
    return (cierre_hoy - ahora).total_seconds() / 60


def en_ventana_sin_compra(mercado):
    """True si estamos dentro de los ultimos MINUTOS_SIN_COMPRAR_ANTES_CIERRE
    minutos antes del cierre de ese mercado (no se compra nada en ese margen)."""
    minutos = minutos_hasta_cierre(mercado)
    if minutos is None:
        return False
    return 0 <= minutos <= MINUTOS_SIN_COMPRAR_ANTES_CIERRE


def en_ventana_venta_forzada(mercado):
    """True si estamos dentro de los ultimos MINUTOS_VENTA_FORZADA_ANTES_CIERRE
    minutos antes del cierre de ese mercado (se vende todo lo que tenga
    beneficio >= UMBRAL_BENEFICIO_PCT, sin mirar el MACD)."""
    minutos = minutos_hasta_cierre(mercado)
    if minutos is None:
        return False
    return 0 <= minutos <= MINUTOS_VENTA_FORZADA_ANTES_CIERRE


def proxima_apertura(mercado):
    """Devuelve el datetime (con zona horaria) de la proxima apertura de ese
    mercado, saltando fines de semana (y festivos de NYSE si es US -ver
    es_festivo_us(), peticion del usuario sept. 2026-)."""
    zona, hora_apertura = APERTURA_POR_MERCADO[mercado]
    ahora = datetime.now(zona)
    candidato = ahora.replace(hour=hora_apertura.hour, minute=hora_apertura.minute,
                              second=0, microsecond=0)
    if candidato <= ahora:
        candidato += timedelta(days=1)
    while candidato.weekday() >= 5 or (mercado == "US" and es_festivo_us(candidato.date())):
        candidato += timedelta(days=1)
    return candidato


def proxima_apertura_cripto():
    """Proxima apertura SEMANAL de cripto (horario "Crypto Basic"): el
    domingo a las 3:00 AM ET. A diferencia de proxima_apertura() (mercados
    con apertura diaria), esta solo se abre una vez por semana -no se usa
    si CRYPTO_24_7 = True, en ese caso el mercado nunca se considera
    "cerrado" y no hace falta calcular una proxima apertura."""
    ahora = datetime.now(ZONA_NY)
    candidato = ahora.replace(hour=HORA_APERTURA_CRYPTO_DOMINGO.hour,
                              minute=HORA_APERTURA_CRYPTO_DOMINGO.minute,
                              second=0, microsecond=0)
    while candidato.weekday() != 6 or candidato <= ahora:  # 6 = domingo
        candidato += timedelta(days=1)
    return candidato


def segundos_hasta_pre_apertura():
    """Con todos los mercados cerrados, calcula cuantos segundos hay que
    esperar hasta MINUTOS_ANTES_DE_APERTURA_PARA_DESPERTAR minutos antes de
    la apertura mas proxima entre todos los mercados soportados."""
    ahora = datetime.now(ZONA_NY)  # solo para tener un instante de referencia comparable
    esperas = []
    for mercado in APERTURA_POR_MERCADO:
        apertura = proxima_apertura(mercado)
        zona_mercado = APERTURA_POR_MERCADO[mercado][0]
        ahora_mercado = datetime.now(zona_mercado)
        segundos = (apertura - ahora_mercado).total_seconds()
        esperas.append(segundos)

    if not CRYPTO_24_7:
        # Sin esto, con todos los mercados de acciones cerrados durante el
        # fin de semana, el bot calcularia la espera hasta la apertura de US
        # el lunes y se perderia toda la ventana de cripto que reabre antes,
        # el domingo a las 3:00 AM ET (horario "Crypto Basic").
        apertura_cripto = proxima_apertura_cripto()
        esperas.append((apertura_cripto - ahora).total_seconds())

    segundos_hasta_mas_proxima = min(esperas)
    segundos_despertar = segundos_hasta_mas_proxima - (MINUTOS_ANTES_DE_APERTURA_PARA_DESPERTAR * 60)
    return max(segundos_despertar, 0)


def en_alguna_ventana_pre_apertura():
    """True si algun mercado esta dentro de los ultimos
    MINUTOS_ANTES_DE_APERTURA_PARA_DESPERTAR minutos antes de su apertura.
    Evita que el bucle principal se quede reintentando sin avanzar de verdad
    justo antes de que abra un mercado."""
    for mercado in APERTURA_POR_MERCADO:
        zona_mercado = APERTURA_POR_MERCADO[mercado][0]
        ahora_mercado = datetime.now(zona_mercado)
        apertura = proxima_apertura(mercado)
        minutos_para_abrir = (apertura - ahora_mercado).total_seconds() / 60
        if 0 <= minutos_para_abrir <= MINUTOS_ANTES_DE_APERTURA_PARA_DESPERTAR:
            return True
    return False


def calcular_macd(cierres, rapida=12, lenta=26, senal=9):
    ema_rapida = cierres.ewm(span=rapida, adjust=False).mean()
    ema_lenta = cierres.ewm(span=lenta, adjust=False).mean()
    macd = ema_rapida - ema_lenta
    linea_senal = macd.ewm(span=senal, adjust=False).mean()
    histograma = macd - linea_senal
    return macd, linea_senal, histograma


def pedir_velas(ib, contrato, duration, barSize):
    """Pide velas historicas con reintentos automaticos: si la respuesta viene
    vacia o falla (por ejemplo por el error 162 de sesion conectada desde otra
    IP, o por timeout de conexion), espera y reintenta antes de rendirse.

    Cortacircuitos: si ya se han encadenado UMBRAL_FALLOS_SEGUIDOS_DATOS
    valores SEGUIDOS sin ningun dato (senal de que TWS/IB Gateway ha perdido
    la conexion con los market data farms, no solo un fallo puntual de un
    valor concreto), se hace un unico intento rapido sin esperas de 15s, para
    no perder horas reintentando algo que muy probablemente va a seguir
    fallando igual en el siguiente valor tambien."""
    global _fallos_seguidos_datos, _aviso_datos_caidos_emitido

    disyuntor_activo = _fallos_seguidos_datos >= UMBRAL_FALLOS_SEGUIDOS_DATOS
    if disyuntor_activo and not _aviso_datos_caidos_emitido:
        log(f"AVISO: {UMBRAL_FALLOS_SEGUIDOS_DATOS} valores seguidos sin ningun dato historico. "
            f"Probable caida de la conexion de TWS/IB Gateway con los market data farms de IBKR "
            f"(el socket API puede seguir 'conectado' aunque esto pase). Revisa TWS/IB Gateway; "
            f"mientras tanto se deja de reintentar 3 veces por valor para no perder horas.")
        notificar_telegram(f"⚠️ {UMBRAL_FALLOS_SEGUIDOS_DATOS} valores seguidos sin ningun dato historico. "
                           f"Probable caida de la conexion de TWS/IB Gateway con los market data farms "
                           f"de IBKR. Revisa TWS/IB Gateway.")
        _aviso_datos_caidos_emitido = True

    intentos = 1 if disyuntor_activo else INTENTOS_MAXIMOS

    # Para contratos CRYPTO, IBKR exige whatToShow='AGGTRADES' en
    # reqHistoricalData; 'TRADES' (el que usan acciones/US/HK/KR) no esta
    # soportado para CRYPTO y no da un error claro, sino que se queda
    # colgado hasta agotar el timeout en todos los intentos (bug real visto
    # en produccion, sept. 2026 - el mismo sintoma que el problema de
    # exchange PAXOS/ZEROHASH, pero con causa distinta). Los contratos de
    # forex (secType='CASH', p.ej. EUR.USD para el tipo de cambio real, ver
    # actualizar_tipo_cambio_eur_usd()) tampoco tienen "TRADES" -se piden
    # con 'MIDPOINT', el precio medio entre bid/ask, el estandar para FX-.
    secType_contrato = getattr(contrato, 'secType', None)
    if secType_contrato == 'CRYPTO':
        what_to_show = 'AGGTRADES'
    elif secType_contrato == 'CASH':
        what_to_show = 'MIDPOINT'
    else:
        what_to_show = 'TRADES'

    for intento in range(1, intentos + 1):
        try:
            velas = ib.reqHistoricalData(
                contrato, endDateTime='', durationStr=duration,
                barSizeSetting=barSize, whatToShow=what_to_show,
                useRTH=False, formatDate=1,
            )
        except Exception as e:
            log(f"{contrato.symbol} - error al pedir datos en el intento {intento}/{intentos}: "
                f"{type(e).__name__}: {e}")
            velas = []

        if velas:
            _fallos_seguidos_datos = 0
            if _aviso_datos_caidos_emitido:
                notificar_telegram("✅ Los datos de mercado de IBKR han vuelto, el bot sigue operando con normalidad.")
            _aviso_datos_caidos_emitido = False
            return velas

        if intento < intentos:
            log(f"{contrato.symbol} - sin datos en el intento {intento}/{intentos}, "
                f"reintentando en {ESPERA_ENTRE_INTENTOS_SEGUNDOS}s...")
            ib.sleep(ESPERA_ENTRE_INTENTOS_SEGUNDOS)
        else:
            # El ultimo intento tambien fallo: se deja constancia explicita
            # en el log (antes se descartaba en silencio si fallaba sin
            # lanzar excepcion, dejando un hueco dificil de diagnosticar).
            log(f"{contrato.symbol} - sin datos tras el ultimo intento ({intento}/{intentos}), "
                f"se omite este valor en este ciclo.")

    _fallos_seguidos_datos += 1
    return []


# Cache de proceso: el exchange de cripto realmente valido para la cuenta
# conectada, descubierto UNA vez (ver descubrir_exchange_cripto()) y
# reutilizado para construir el contrato de CUALQUIER cripto (incluidas las
# que la cuenta no tiene abiertas todavia), en vez de adivinar un exchange
# fijo -ver la historia real documentada junto a ACTIVOS_CRYPTO-.
_exchange_cripto_cache = None


def descubrir_exchange_cripto(ib):
    """Busca entre las posiciones abiertas alguna de CRYPTO ya resuelta por
    IBKR a partir de su conId real (si su `exchange` viene vacio, como es
    habitual en ib.positions(), se completa aqui mismo con
    ib.qualifyContracts(), igual que ya hace revisar_ventas) y devuelve ese
    exchange. None si la cuenta no tiene ninguna posicion de cripto abierta
    todavia (primer arranque sin haber comprado nunca nada de cripto) -en
    ese caso no hay forma de saber que exchange es el correcto sin
    arriesgarse a adivinar, asi que se deja para el siguiente ciclo (en
    cuanto haya una compra que confirme el exchange valido, ya sea manual o
    del propio bot)."""
    for pos in ib.positions():
        if getattr(pos.contract, "secType", None) == "CRYPTO" and pos.position > 0:
            contrato = pos.contract
            if not contrato.exchange:
                ib.qualifyContracts(contrato)
            if contrato.exchange:
                return contrato.exchange
    return None


def crear_contrato(ib, activo):
    if activo["mercado"] == "CRYPTO":
        global _exchange_cripto_cache
        if _exchange_cripto_cache is None:
            _exchange_cripto_cache = descubrir_exchange_cripto(ib)
            if _exchange_cripto_cache:
                log(f"CRYPTO: exchange valido para esta cuenta descubierto a partir de una "
                    f"posicion real: '{_exchange_cripto_cache}'. Se reutiliza para todas las "
                    f"criptomonedas de ACTIVOS_CRYPTO.")
        exchange = _exchange_cripto_cache or activo["exchange"]
        return Crypto(activo["ticker"], exchange, activo["currency"])
    return Stock(activo["ticker"], activo["exchange"], activo["currency"])


def _sin_flags_legacy(orden):
    """Las ordenes con cantidad fraccionaria (acciones no enteras) son
    rechazadas por IBKR si estos dos flags heredados de la API antigua se
    quedan en su valor por defecto (True); hay que ponerlos explicitamente
    a False. No afecta a las ordenes con cantidad entera."""
    orden.eTradeOnly = False
    orden.firmQuoteOnly = False
    return orden


def crear_orden_mercado(accion, cantidad):
    return _sin_flags_legacy(MarketOrder(accion, cantidad))


def es_cantidad_fraccionaria(cantidad):
    return abs(cantidad - round(cantidad)) > 1e-6


def crear_orden_mercado_cash(accion, importe_efectivo):
    """Orden a mercado especificada por IMPORTE EN EFECTIVO en vez de numero
    de acciones. Es la unica forma que acepta la API de IBKR para comprar o
    vender una cantidad fraccionaria de acciones: enviar una cantidad de
    acciones fraccionaria directamente (p.ej. totalQuantity=3.1449) es
    rechazado por IBKR con el error 10243 ("No se puede introducir la orden
    de tamano fraccionario a traves de la API"), visto en produccion en
    TODAS las compras fraccionarias de US -> ninguna llegaba a ejecutarse."""
    orden = MarketOrder(accion, 0)
    orden.cashQty = round(importe_efectivo, 2)
    return _sin_flags_legacy(orden)


ESPERA_MAXIMA_ESTADO_ORDEN_SEGUNDOS = 10
INTERVALO_CHEQUEO_ESTADO_ORDEN_SEGUNDOS = 0.5


def esperar_estado_final_orden(ib, trade, espera_maxima=ESPERA_MAXIMA_ESTADO_ORDEN_SEGUNDOS):
    """Espera a que la orden llegue a un estado final (Filled, Cancelled,
    Inactive...) en vez de comprobar el estado tras una espera fija de unos
    segundos. IBKR a veces manda avisos intermedios (p.ej. error 10349,
    "TIF ajustado a DAY") con un estado transitorio en el mismo instante que
    NO es el resultado definitivo; comprobar demasiado pronto puede hacer
    que el log registre "PreSubmitted" para una orden que en realidad se
    completo (o cancelo) un par de segundos despues. Si se agota el tiempo
    maximo sin llegar a un estado final, se devuelve el ultimo estado visto
    (la orden sigue viva en IBKR, simplemente tarda mas de lo esperado)."""
    transcurrido = 0.0
    while not trade.isDone() and transcurrido < espera_maxima:
        ib.sleep(INTERVALO_CHEQUEO_ESTADO_ORDEN_SEGUNDOS)
        transcurrido += INTERVALO_CHEQUEO_ESTADO_ORDEN_SEGUNDOS
    return trade.orderStatus.status


def crear_orden_limitada(accion, cantidad, precio_limite, fuera_horario_regular=False):
    orden = _sin_flags_legacy(LimitOrder(accion, cantidad, precio_limite))
    if fuera_horario_regular:
        # Sin este flag, IBKR rechaza o deja pendiente (sin ejecutar) una
        # orden colocada fuera de la sesion regular (9:30-16:00 ET).
        orden.outsideRth = True
    return orden


def crear_orden_limitada_cash(accion, importe_efectivo, precio_limite, fuera_horario_regular=False):
    """Version 'cash quantity' de crear_orden_limitada, para cantidades
    fraccionarias (vease crear_orden_mercado_cash)."""
    orden = LimitOrder(accion, 0, precio_limite)
    orden.cashQty = round(importe_efectivo, 2)
    orden = _sin_flags_legacy(orden)
    if fuera_horario_regular:
        orden.outsideRth = True
    return orden


def crear_orden_limitada_cripto(accion, cantidad, precio_limite):
    """Orden LIMITADA para criptomonedas (PAXOS/ZEROHASH, ver EXCHANGE_CRYPTO). A diferencia de las
    acciones -donde una cantidad fraccionaria puesta directamente via API
    es rechazada con el error 10243, y hay que recurrir al truco del importe
    en efectivo (cashQty)-, en cripto las ordenes LMT SI admiten
    `totalQuantity` fraccionario de forma directa y nativa (confirmado en la
    documentacion oficial de IBKR: las ordenes LMT de cripto usan
    quantity/totalQuantity; solo las ordenes MKT de cripto usan cashQty, y
    aqui no se usan ordenes a mercado para cripto). No hace falta ningun
    Plan B/C como con las acciones.

    Bug real de produccion (sept. 2026), en DOS pasos:
    1) LimitOrder() de ib_async deja `tif` vacio ('') por defecto. Para
       acciones esto funciona (IBKR lo trata como 'DAY' implicito), pero el
       exchange de cripto de esta cuenta (ZEROHASHE) lo RECHAZA
       explicitamente con el error 10052 "Invalid time in force" -ninguna
       compra ni venta de cripto llegaba a colocarse-.
    2) Se probo `tif='GTC'` (la documentacion general de IBKR dice que LMT
       admite DAY/GTC/IOC en cripto), pero esta cuenta lo rechazo con el
       error 201 "The crypto buy order must be Minutes or IOC" -GTC
       tampoco vale para comprar, pese a lo que dice la documentacion
       general-. Corregido a `tif='IOC'` (Immediate-or-Cancel): encaja bien
       ademas con como ya funciona el bot (coloca la orden al precio actual
       y comprueba el resultado al momento vía esperar_estado_final_orden,
       sin depender de que una orden se quede "viva" esperando)."""
    return _sin_flags_legacy(LimitOrder(accion, cantidad, precio_limite, tif='IOC'))


def estimar_comision_cripto(valor_operacion):
    """Comision de IBKR en cripto: 0.18% del valor operado, minimo 1.75 USD,
    con tope del 1% del valor operado (evita que el minimo fijo se coma un
    % desproporcionado en operaciones pequeñas)."""
    if valor_operacion <= 0:
        return 0.0
    comision = max(valor_operacion * COMISION_CRIPTO_PCT, COMISION_CRIPTO_MINIMA_USD)
    return min(comision, valor_operacion * COMISION_CRIPTO_MAX_PCT)


def orden_rechazada_por_codigo(trade, codigos_error):
    """True si el registro de la orden (trade.log) contiene alguno de los
    codigos de error de IBKR indicados. Se usa para detectar rechazos
    concretos (p.ej. 10244: "cash quantity no admitida en esta orden" -no
    todos los valores tienen habilitadas las fracciones via API en IBKR,
    aunque el mercado en general si las soporte-) y reaccionar de forma
    distinta a un fallo generico."""
    return any(getattr(entry, 'errorCode', None) in codigos_error for entry in getattr(trade, 'log', []))


def atajo_cripto_alcista(ib, contrato, un_minuto_alcista):
    """Atajo de compra EXCLUSIVO de cripto (peticion del usuario, sept.
    2026): ademas del atajo general de 4 temporalidades cortas (1/5/15/30
    min, ver cuatro_cortas_alcistas en analizar_activo), cripto comprueba
    TAMBIEN 1/3/10/20 min - si las 4 salen alcistas, compra directamente.

    `un_minuto_alcista` es el resultado YA calculado para "1 minuto" en el
    analisis general (mismo barSize, mismo calculo de MACD) - se reutiliza
    en vez de pedirlo otra vez a IBKR. Si ya es None o False, no hace falta
    ni mirar las otras 3 (todas tienen que ser alcistas para que cuente).

    Devuelve True/False, o None si faltan datos en alguna temporalidad (no
    se puede decidir con seguridad)."""
    if not un_minuto_alcista:
        return un_minuto_alcista  # None o False, tal cual

    for tf in TEMPORALIDADES_CRIPTO_ATAJO_EXTRA:
        velas = pedir_velas(ib, contrato, tf["duration"], tf["barSize"])
        if len(velas) < 35:
            return None
        cierres = pd.Series([v.close for v in velas])
        macd, linea_senal, _ = calcular_macd(cierres)
        if not bool(macd.iloc[-1] > linea_senal.iloc[-1]):
            return False
    return True


# Cache de las temporalidades LARGAS (dia/semana): peticion del usuario,
# sept. 2026. Con un horizonte de trading de HORAS, la tendencia diaria y
# semanal se usa como filtro de fondo (evitar comprar contra la tendencia
# dominante), no como señal de entrada -para eso ya estan las temporalidades
# cortas (1min-1h), que si se piden en cada ciclo-. Como una vela diaria o
# semanal solo cambia de verdad cuando CIERRA (una vez al dia / una vez a la
# semana), pedirla de nuevo cada pocos minutos no aporta nada y consume
# cuota de peticiones a IBKR sin necesidad (ver NOTES.md).
#
# Matiz (peticion del usuario): la vela de HOY/ESTA SEMANA (todavia en
# formacion) SI se tiene en cuenta una vez que lleva al menos un
# UMBRAL_FRACCION_VELA_EN_CURSO de su periodo transcurrido -antes de eso, es
# ruido puro (un lunes a las 9:35 la vela diaria lleva 5 minutos de datos).
# Por debajo del umbral se usan solo barras CERRADAS (iloc[-2] vs iloc[-4])
# y se cachea una unica vez al dia (no puede cambiar, son datos cerrados).
# Por encima del umbral se incluye la vela en curso (iloc[-1] vs iloc[-3]),
# pero como esa vela SI cambia con el precio, se refresca cada
# INTERVALO_REFRESCO_VELA_EN_CURSO_SEGUNDOS en vez de en cada ciclo -sigue
# ahorrando peticiones frente a pedirla cada 2-4 minutos, sin quedarse con
# un dato obsoleto durante horas-.
UMBRAL_FRACCION_VELA_EN_CURSO = 0.4
INTERVALO_REFRESCO_VELA_EN_CURSO_SEGUNDOS = 60 * 60  # 1 hora

VENTANA_DIA_POR_MERCADO = {
    "US": (ZONA_NY, HORA_INICIO_US, HORA_CIERRE_EXTENDIDO_US),
    "HK": (ZONA_HK, HORA_INICIO_HK, HORA_CIERRE_HK),
    "KR": (ZONA_KR, HORA_INICIO_KR, HORA_CIERRE_KR),
}


def _fraccion_transcurrida_del_dia(mercado):
    """Fraccion (0.0-1.0) de la sesion de HOY ya transcurrida. CRYPTO no
    tiene sesion (opera 24/7): se usa el dia de calendario UTC completo
    (00:00-24:00) como referencia -no hace falta mas precision que esa para
    decidir si la vela diaria en curso ya tiene suficiente informacion-."""
    if mercado == "CRYPTO":
        ahora_utc = datetime.now(timezone.utc)
        segundos_transcurridos = ahora_utc.hour * 3600 + ahora_utc.minute * 60 + ahora_utc.second
        return segundos_transcurridos / 86400

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


_cache_temporalidades_largas = {}  # ticker -> {"<nombre tf>": {"fecha", "modo", "resultado", "ultima_actualizacion"}}


def _detalle_larga_cacheado(ib, contrato, ticker, tf, mercado):
    hoy = datetime.now().date()
    fraccion = (_fraccion_transcurrida_del_dia(mercado) if tf['nombre'] == "1 dia"
                else _fraccion_transcurrida_de_la_semana(mercado))
    modo = "en_curso" if fraccion >= UMBRAL_FRACCION_VELA_EN_CURSO else "cerrada"

    cache_tf = _cache_temporalidades_largas.get(ticker, {}).get(tf['nombre'])
    if cache_tf is not None and cache_tf["fecha"] == hoy and cache_tf["modo"] == modo:
        if modo == "cerrada":
            return cache_tf["resultado"]
        if time.monotonic() - cache_tf["ultima_actualizacion"] < INTERVALO_REFRESCO_VELA_EN_CURSO_SEGUNDOS:
            return cache_tf["resultado"]

    velas = pedir_velas(ib, contrato, tf['duration'], tf['barSize'])
    if len(velas) < 35:
        resultado = None
    else:
        # modo "cerrada": solo barras YA CERRADAS (la ULTIMA vela de la serie
        # -el dia/semana en curso- sigue formandose en tiempo real y es ruido
        # puro al principio del periodo). modo "en_curso": ya ha pasado
        # suficiente del periodo como para que la vela en formacion aporte
        # señal de verdad, se incluye.
        cierres = pd.Series([v.close for v in velas])
        _, _, histograma = calcular_macd(cierres)
        if modo == "cerrada":
            resultado = bool(histograma.iloc[-2] > histograma.iloc[-4])
        else:
            resultado = bool(histograma.iloc[-1] > histograma.iloc[-3])

    _cache_temporalidades_largas.setdefault(ticker, {})[tf['nombre']] = {
        "fecha": hoy, "modo": modo, "resultado": resultado, "ultima_actualizacion": time.monotonic(),
    }
    return resultado


def analizar_activo(ib, activo):
    contrato = crear_contrato(ib, activo)
    ib.qualifyContracts(contrato)

    if not contrato.conId:
        # El contrato ni siquiera se ha podido resolver (simbolo/exchange
        # incorrecto, o falta de permisos en ese mercado). No tiene sentido
        # gastar reintentos en pedir datos historicos de un contrato invalido.
        return contrato, "SIMBOLO_NO_RESUELTO"

    ticker = activo["ticker"]
    detalle = {}
    for tf in TEMPORALIDADES:
        if tf['tipo'] == 'larga':
            detalle[tf['nombre']] = _detalle_larga_cacheado(ib, contrato, ticker, tf, activo["mercado"])
            continue

        velas = pedir_velas(ib, contrato, tf['duration'], tf['barSize'])
        if len(velas) < 35:
            detalle[tf['nombre']] = None
            continue

        cierres = pd.Series([v.close for v in velas])
        macd, linea_senal, _ = calcular_macd(cierres)
        detalle[tf['nombre']] = bool(macd.iloc[-1] > linea_senal.iloc[-1])

    # Atajo: si las 4 temporalidades mas cortas (1min, 5min, 15min, 30min)
    # estan todas alcistas, se compra directamente, sin mirar 1h ni la regla
    # de "maximo 1 de 7 en contra" de mas abajo. SI se sigue respetando que
    # ninguna larga (dia/semana) este en contra (peticion del usuario, sept.
    # 2026): antes el atajo ignoraba dia/semana por completo, así que
    # bastaba con las 4 cortas alcistas para comprar aunque la tendencia
    # diaria o semanal fuera claramente bajista -la peor categoria de
    # entrada, comprar contra la tendencia dominante-. Bug real corregido
    # aqui: la vieja regla de "1 de 7 en contra" de mas abajo NUNCA llegaba
    # a aplicarse en este caso, porque el atajo la adelantaba siempre que
    # las 4 cortas estuvieran alcistas (lo mas habitual).
    largas_en_contra = any(detalle[tf['nombre']] is False for tf in TEMPORALIDADES if tf['tipo'] == 'larga')
    cuatro_cortas_alcistas = (
        all(detalle[n] is not None for n in NOMBRES_4_CORTAS)
        and all(detalle[n] for n in NOMBRES_4_CORTAS)
        and not largas_en_contra
    )

    # Atajo EXCLUSIVO de cripto: 1/3/10/20 min, independiente del atajo
    # general de arriba -ver atajo_cripto_alcista().
    atajo_cripto = None
    if activo["mercado"] == "CRYPTO":
        atajo_cripto = atajo_cripto_alcista(ib, contrato, detalle.get("1 minuto"))

    faltan_datos = any(detalle[tf['nombre']] is None for tf in TEMPORALIDADES)
    total_false = sum(1 for tf in TEMPORALIDADES if detalle[tf['nombre']] is False)
    cortas_ok = all(detalle[tf['nombre']] for tf in TEMPORALIDADES
                     if tf['tipo'] == 'corta' and detalle[tf['nombre']] is not None)
    largas_ok = all(detalle[tf['nombre']] for tf in TEMPORALIDADES
                     if tf['tipo'] == 'larga' and detalle[tf['nombre']] is not None)
    # La UNICA temporalidad en contra (si hay exactamente 1) - usado para
    # distinguir si es una corta (retroceso normal, sigue siendo buena
    # entrada) o una larga (contra la tendencia dominante, mala entrada).
    tf_en_contra = next((tf for tf in TEMPORALIDADES if detalle[tf['nombre']] is False), None)

    if cuatro_cortas_alcistas or atajo_cripto:
        decision = "COMPRA"
    elif faltan_datos:
        decision = "SIN_DATOS"
    elif total_false == 0:
        decision = "COMPRA"
    elif total_false == 1:
        # Peticion del usuario (sept. 2026): la excepcion de "1 de 7 en
        # contra" solo vale si la discrepancia es en una temporalidad CORTA
        # (1min-1h) -un retroceso normal, a menudo mejor precio de entrada-.
        # Si la unica en contra es la diaria o semanal, NO se compra: seria
        # entrar contra la tendencia dominante, la peor categoria de
        # entrada (antes esto se trataba igual que un "1 minuto en contra",
        # bug real corregido aqui).
        if tf_en_contra is not None and tf_en_contra['tipo'] == 'corta':
            decision = "COMPRA"
        else:
            decision = "BLOQUEADO_TF_LARGA"
    elif cortas_ok and not largas_ok:
        decision = "BLOQUEADO"
    else:
        decision = "SIN_SENAL"

    return contrato, decision


def macd_5min_bajista(ib, contrato):
    velas = pedir_velas(ib, contrato, '2 D', '5 mins')
    if len(velas) < 35:
        return None
    cierres = pd.Series([v.close for v in velas])
    macd, linea_senal, _ = calcular_macd(cierres)
    return bool(macd.iloc[-1] < linea_senal.iloc[-1])


# --- Criterio de venta: trailing stop + refuerzo de 2 velas + salida
# parcial (peticion del usuario, sept. 2026). Al principio solo se aplico a
# ACCIONES; luego se extendio a CRYPTO tambien (mismo mecanismo, cada
# mercado usa su propio umbral de entrada: UMBRAL_BENEFICIO_PCT para
# acciones, UMBRAL_BENEFICIO_CRYPTO_PCT para cripto).
TRAILING_STOP_VENTA_PCT = 0.3  # puntos de retroceso desde el maximo neto alcanzado

# Bug real de produccion (sept. 2026): el maximo trackeado vivia SOLO en
# memoria (un dict normal). Cada reinicio del proceso (un despliegue, una
# caida, una reconexion con IB Gateway que reinicia el bot) lo borraba por
# completo, asi que el bot "olvidaba" que una posicion habia llegado a un
# beneficio alto y trackeaba desde el valor que tuviera justo al arrancar
# -un retroceso real desde un maximo anterior al reinicio nunca disparaba
# el trailing stop, porque el bot nunca "vio" ese maximo-. Ahora se
# persiste en disco (ARCHIVO_ESTADO_VENTA) y se recarga al arrancar, igual
# que el historial de operaciones.
ARCHIVO_ESTADO_VENTA = "estado_venta_bot_completo.json"
_maximo_beneficio_neto_por_posicion = {}  # clave_historial(mercado, ticker) -> % neto maximo visto

# Salida parcial (peticion del usuario, sept. 2026): en cuanto una posicion
# alcanza por primera vez su umbral minimo, se vende PORCENTAJE_SCALE_OUT de
# la posicion para asegurar beneficio ya, dejando el resto corriendo con el
# trailing stop -mejora el beneficio medio por operacion sin cambiar el
# perfil de riesgo, en vez del viejo "todo o nada"-. Solo se hace UNA vez
# por posicion (_scale_out_realizado), no en cada ciclo tras la primera vez.
PORCENTAJE_SCALE_OUT = 0.5
_scale_out_realizado = set()  # claves ya con su venta parcial hecha (mismo formato que el dict de arriba)


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


def macd_5min_bajista_2_velas(ib, contrato):
    """Version 'reforzada' de macd_5min_bajista(): exige que las DOS
    ultimas velas de 5 min (no solo la ultima) tengan MACD por debajo de su
    linea de señal, para filtrar el ruido de una vela bajista suelta que
    resulta ser solo una pausa dentro de una subida mas larga. Se usa tanto
    para acciones como para cripto -misma llamada a reqHistoricalData, sin
    diferencias entre mercados-."""
    velas = pedir_velas(ib, contrato, '2 D', '5 mins')
    if len(velas) < 36:
        return None
    cierres = pd.Series([v.close for v in velas])
    macd, linea_senal, _ = calcular_macd(cierres)
    return bool(macd.iloc[-1] < linea_senal.iloc[-1] and macd.iloc[-2] < linea_senal.iloc[-2])


def decidir_accion_venta(clave, beneficio_pct, umbral):
    """Logica compartida de venta (trailing stop + salida parcial),
    independiente del mercado -se llama con la misma `clave` que usa
    _maximo_beneficio_neto_por_posicion/_scale_out_realizado (ver
    clave_historial()), y el `umbral` propio de ese mercado (acciones o
    cripto). Devuelve (accion, motivo), accion en
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
    posicion (llamar tras confirmar una venta TOTAL, para que si se vuelve
    a comprar el mismo valor mas adelante, el trailing empiece de cero)."""
    _maximo_beneficio_neto_por_posicion.pop(clave, None)
    _scale_out_realizado.discard(clave)
    _guardar_estado_venta()


CONTRATO_EUR_USD = Forex('EURUSD')  # contrato de forex para el tipo de cambio real (ver mas abajo)
INTERVALO_ACTUALIZACION_TIPO_CAMBIO_SEGUNDOS = 30 * 60  # 30 min: de sobra para un tipo de cambio
                                                          # que no varia bruscamente de un momento a otro
_ultima_actualizacion_tipo_cambio = 0.0


def actualizar_tipo_cambio_eur_usd(ib):
    """Refresca TIPO_CAMBIO_EUR_USD con el precio real de mercado (Forex
    EUR.USD via IBKR, precio MIDPOINT: el punto medio entre bid y ask,
    estandar para FX) en vez de dejarlo fijo a un valor que hay que
    actualizar a mano (peticion del usuario, sept. 2026). Throttlada a lo
    sumo cada INTERVALO_ACTUALIZACION_TIPO_CAMBIO_SEGUNDOS -se puede llamar
    en cada vuelta del bucle principal sin miedo a pedir datos de mas-.

    Si falla (sin permisos de datos de forex, sin conexion, etc.) se deja
    el valor anterior tal cual y solo se registra un aviso: esto NUNCA
    afecta al dinero real operado (el tamaño de cada operacion se calcula
    siempre en USD nativo desde el valor de la cuenta, ver
    LIMITE_EXPOSICION_PCT) -solo afecta a las conversiones para MOSTRAR
    importes en EUR (Telegram, cartera_ibkr.py) y al techo de seguridad
    IMPORTE_EUROS, que en la practica casi nunca llega a aplicar."""
    global TIPO_CAMBIO_EUR_USD, _ultima_actualizacion_tipo_cambio
    ahora = time.monotonic()
    if ahora - _ultima_actualizacion_tipo_cambio < INTERVALO_ACTUALIZACION_TIPO_CAMBIO_SEGUNDOS:
        return
    _ultima_actualizacion_tipo_cambio = ahora
    try:
        if not CONTRATO_EUR_USD.conId:
            ib.qualifyContracts(CONTRATO_EUR_USD)
        velas = pedir_velas(ib, CONTRATO_EUR_USD, '1 D', '5 mins')
        if velas:
            TIPO_CAMBIO_EUR_USD = velas[-1].close
            log(f"Tipo de cambio EUR/USD actualizado al precio real de mercado: {TIPO_CAMBIO_EUR_USD:.4f}")
        else:
            log(f"No se pudo obtener el tipo de cambio EUR/USD real, se mantiene el valor "
                f"anterior ({TIPO_CAMBIO_EUR_USD:.4f}).")
    except Exception as e:
        log(f"Error al actualizar el tipo de cambio EUR/USD, se mantiene el valor anterior "
            f"({TIPO_CAMBIO_EUR_USD:.4f}): {type(e).__name__}: {e}")


def valor_en_usd(valor, currency):
    """Convierte un valor a USD segun la divisa (USD, EUR, HKD, KRW)."""
    if currency == "USD":
        return valor
    if currency == "EUR":
        return valor * TIPO_CAMBIO_EUR_USD
    if currency == "HKD":
        return valor / TIPO_CAMBIO_USD_HKD
    if currency == "KRW":
        return valor / TIPO_CAMBIO_USD_KRW
    return valor  # fallback, no deberia ocurrir con esta lista de activos


# Tarifas reales de IBKR, plan de comisiones "por niveles" (tiered), Nivel I
# -el que aplica con el volumen mensual de esta cuenta-, consultadas en
# interactivebrokers.ie en agosto de 2026. IMPORTANTE: estas cifras son SOLO
# la comision de IBKR; no incluyen "comisiones de terceros" (tasas de
# bolsa/compensacion/normativas, p.ej. el impuesto de timbre de HK) que IBKR
# repercute aparte y que no estan cuantificadas aqui -el beneficio neto
# calculado por el bot sigue siendo una aproximacion, algo optimista, no una
# cifra exacta al centimo-.
COMISION_US_POR_ACCION = 0.0035    # USD/accion, ordenes de acciones ENTERAS
COMISION_US_MINIMA = 0.35          # USD minimo por orden, acciones enteras
COMISION_US_FRACCION_PCT = 0.01    # 1% del valor, ordenes FRACCIONARIAS
COMISION_US_FRACCION_MINIMA = 0.01  # USD minimo por orden fraccionaria
COMISION_MAX_PCT = 0.01            # tope maximo: 1% del valor negociado (US)

COMISION_HK_PCT = 0.0005           # 0.05% del valor negociado
COMISION_HK_MINIMA_USD = 2.25      # minimo por orden, en USD equivalente

COMISION_KR_PCT = 0.0006           # 0.06% del valor negociado
COMISION_KR_MINIMA_KRW = 4000      # minimo por orden, en KRW (KR no tiene
                                    # plan de comisiones fijas, solo tiered)

# Fallback generico para divisas sin tarifa especifica arriba (p.ej. EUR,
# mercado actualmente desactivado): estimacion previa, conservadora.
COMISION_PCT = 0.0007        # 0.07% por operacion (compra o venta)
MINIMO_COMISION_EUR = 1.0    # minimo 1 EUR por operacion, convertido a la divisa local


def minimo_comision_en_moneda(currency):
    """Convierte el minimo de 1 EUR a la divisa indicada."""
    if currency == "EUR":
        return MINIMO_COMISION_EUR
    if currency == "USD":
        return MINIMO_COMISION_EUR * TIPO_CAMBIO_EUR_USD
    if currency == "HKD":
        return MINIMO_COMISION_EUR * TIPO_CAMBIO_EUR_USD * TIPO_CAMBIO_USD_HKD
    if currency == "KRW":
        return MINIMO_COMISION_EUR * TIPO_CAMBIO_EUR_USD * TIPO_CAMBIO_USD_KRW
    return MINIMO_COMISION_EUR


def estimar_comision(valor_operacion, currency, cantidad=None):
    """Comision estimada de UNA sola operacion (compra O venta -para el
    coste de ida y vuelta hay que sumar dos llamadas, una por cada lado-),
    con las tarifas reales de IBKR (tiered, Nivel I) por mercado:
      - US, acciones ENTERAS: 0.0035 USD/accion, minimo 0.35 USD/orden,
        tope maximo 1% del valor negociado.
      - US, acciones FRACCIONARIAS: 1% del valor negociado, minimo 0.01 USD.
      - HK: 0.05% del valor negociado, minimo ~2.25 USD equivalente/orden.
      - KR: 0.06% del valor negociado, minimo 4000 KRW/orden.
    `cantidad` (numero de acciones) hace falta en US para distinguir acciones
    enteras de fraccionarias; si no se indica, se asume fraccionaria (el
    caso mas habitual de este bot en US)."""
    if currency == "USD":
        if cantidad is not None and not es_cantidad_fraccionaria(cantidad):
            comision = max(cantidad * COMISION_US_POR_ACCION, COMISION_US_MINIMA)
            return min(comision, valor_operacion * COMISION_MAX_PCT)
        return max(valor_operacion * COMISION_US_FRACCION_PCT, COMISION_US_FRACCION_MINIMA)
    if currency == "HKD":
        minimo_hkd = COMISION_HK_MINIMA_USD * TIPO_CAMBIO_USD_HKD
        return max(valor_operacion * COMISION_HK_PCT, minimo_hkd)
    if currency == "KRW":
        return max(valor_operacion * COMISION_KR_PCT, COMISION_KR_MINIMA_KRW)
    return max(valor_operacion * COMISION_PCT, minimo_comision_en_moneda(currency))


# --- Historial persistente de fecha de compra inicial ---
# reqExecutions() de IBKR solo devuelve las ejecuciones del dia actual, asi
# que no sirve para saber cuando se compro por primera vez un valor que ya
# se tenia de dias anteriores (se veia como "?" en el resumen de cierre).
# Este archivo guarda, para cada valor, la fecha de la compra que abrio la
# posicion desde cero. Si despues se hacen compras adicionales para
# promediar a la baja, esa fecha NO se toca -sigue siendo la apertura
# original-; solo se actualiza cuando el valor se vende del todo y se
# vuelve a comprar de cero mas adelante.
ARCHIVO_HISTORIAL_COMPRAS = "historial_compras.json"


def clave_historial(mercado, ticker):
    return f"{mercado}:{ticker}"


def cargar_historial_compras():
    try:
        with open(ARCHIVO_HISTORIAL_COMPRAS, "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def guardar_historial_compras(historial):
    try:
        with open(ARCHIVO_HISTORIAL_COMPRAS, "w", encoding="utf-8") as f:
            json.dump(historial, f, indent=2, sort_keys=True)
    except OSError as e:
        log(f"No se pudo guardar el historial de compras ({ARCHIVO_HISTORIAL_COMPRAS}): {type(e).__name__}: {e}")


# --- Historial persistente de operaciones ejecutadas (compras y ventas) ---
# A diferencia de reqExecutions() de IBKR (solo devuelve el dia actual), este
# archivo acumula CADA operacion ejecutada con exito desde que existe esta
# funcionalidad, para poder consultar el estado de cartera y las operaciones
# cerradas de cualquier rango de fechas con cartera_ibkr.py. Solo registro,
# no participa en ninguna decision de trading.
ARCHIVO_HISTORIAL_OPERACIONES = "historial_operaciones_ibkr.json"


def cargar_historial_operaciones():
    try:
        with open(ARCHIVO_HISTORIAL_OPERACIONES, "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return []


def registrar_operacion_historial(mercado, ticker, lado, cantidad, precio, comision, currency,
                                   coste_medio=None, beneficio_pct=None):
    registro = {
        "fecha_hora": datetime.now().isoformat(timespec="seconds"),
        "mercado": mercado,
        "ticker": ticker,
        "lado": lado,  # "COMPRA" o "VENTA"
        "cantidad": cantidad,
        "precio": precio,
        "comision": comision,
        "currency": currency,
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


def registrar_apertura_de_posicion(mercado, ticker):
    """Marca AHORA como la fecha de apertura de una posicion nueva desde
    cero (solo debe llamarse cuando se confirma que la compra se ejecuto de
    verdad y no se tenia ninguna cantidad de ese valor antes)."""
    historial = cargar_historial_compras()
    historial[clave_historial(mercado, ticker)] = datetime.now().isoformat(timespec="seconds")
    guardar_historial_compras(historial)


def obtener_apertura_registrada(mercado, ticker):
    """Devuelve el datetime de apertura registrado para ese valor, o None
    si no hay ningun registro (p.ej. una posicion abierta antes de que
    existiera este historial)."""
    historial = cargar_historial_compras()
    valor = historial.get(clave_historial(mercado, ticker))
    if valor is None:
        return None
    try:
        return datetime.fromisoformat(valor)
    except ValueError:
        return None


def obtener_cantidad_posicion_real(ib, ticker, currency):
    """Consulta a IBKR (en vivo, sin cache) la cantidad actual de la
    posicion de un valor concreto. 0.0 si no se tiene ninguna."""
    ib.reqPositions()
    ib.sleep(1)
    for pos in ib.positions():
        if pos.contract.symbol == ticker and pos.contract.currency == currency and pos.position > 0:
            return pos.position
    return 0.0


def verificar_posicion_tras_orden_no_confirmada(ib, contrato, cantidad_antes, prefijo_log):
    """Cuando una orden no termina en estado 'Filled', el objeto Trade que
    seguimos puede no reflejar lo que realmente paso: se ha visto en
    produccion que IBKR a veces cancela la orden original y la reenvia
    corregida por dentro (p.ej. tras el aviso "TIF ajustado segun preset"),
    y esa orden de reemplazo se ejecuta sin que nuestro codigo la vea, pese
    a que el objeto Trade original quedo marcado como 'Cancelled'. Para no
    quedarnos con una conclusion equivocada, se vuelve a consultar la
    posicion real en IBKR unos segundos despues y se compara con la
    cantidad que habia antes de intentar la orden."""
    ib.sleep(2)
    cantidad_ahora = obtener_cantidad_posicion_real(ib, contrato.symbol, contrato.currency)
    if abs(cantidad_ahora - cantidad_antes) > 1e-6:
        log(f"{prefijo_log} - la posicion SI cambio de verdad ({cantidad_antes:g} -> {cantidad_ahora:g} "
            f"acciones) pese al estado no confirmado como 'Filled' (probable reenvio automatico de IBKR).")
    else:
        log(f"{prefijo_log} - la posicion NO ha cambiado ({cantidad_ahora:g} acciones): "
            f"confirmado que la orden no se ejecuto.")
    return cantidad_ahora


def mercado_de_posicion(pos):
    """Como CURRENCY_A_MERCADO no puede distinguir cripto (tambien en USD)
    de acciones US, se comprueba primero el tipo de contrato (secType).
    getattr con default None: los contratos de acciones normales (Stock)
    siempre tienen secType='STK', pero se usa getattr por robustez ante
    cualquier objeto que no lo tenga (p.ej. dobles de prueba)."""
    if getattr(pos.contract, "secType", None) == "CRYPTO":
        return "CRYPTO"
    return CURRENCY_A_MERCADO.get(pos.contract.currency, "?")


def contrato_pertenece_a_mercado(contrato, mercado):
    """Version de mercado_de_posicion() para un Contract suelto (no un
    Position), usada al filtrar ejecuciones/posiciones por mercado en
    generar_resumen_cierre_mercado()."""
    es_cripto = getattr(contrato, "secType", None) == "CRYPTO"
    if mercado == "CRYPTO":
        return es_cripto
    if es_cripto:
        return False  # nunca colar cripto en el resumen de otro mercado (misma divisa, USD)
    return CURRENCY_A_MERCADO.get(contrato.currency) == mercado


def revisar_ventas(ib, mercados=None):
    """Si `mercados` es None (por defecto), revisa TODOS los mercados con
    posiciones abiertas, igual que siempre. Si se pasa un conjunto (p.ej.
    {"CRYPTO"}), solo revisa posiciones de esos mercados -usado para poder
    correr cripto en su propia cadencia mas rapida, independiente de
    US/HK/KR (ver CRYPTO_INTERVALO_SEGUNDOS y ciclo_completo())."""
    ib.reqPositions()
    ib.sleep(1)  # da tiempo a que la respuesta llegue antes de leer ib.positions()
    posiciones = ib.positions()

    if not posiciones:
        log("VENTAS: no hay posiciones abiertas.")
        return

    posiciones_con_mercado = [
        (mercado_de_posicion(pos), pos)
        for pos in posiciones if pos.position > 0
    ]
    if mercados is not None:
        posiciones_con_mercado = [(m, p) for m, p in posiciones_con_mercado if m in mercados]
        if not posiciones_con_mercado:
            return
    posiciones_con_mercado.sort(key=lambda x: x[0])

    # Poda del maximo de beneficio neto y de la marca de salida parcial
    # trackeados por posicion (ver TRAILING_STOP_VENTA_PCT/PORCENTAJE_SCALE_OUT
    # mas arriba): si un ticker ya no esta entre las posiciones abiertas (se
    # vendio del todo, dentro o fuera del bot), se olvidan -si se vuelve a
    # comprar mas adelante, empieza de cero-.
    claves_vivas = {clave_historial(mercado_de_posicion(pos), pos.contract.symbol) for pos in posiciones if pos.position > 0}
    for clave_vieja in list(_maximo_beneficio_neto_por_posicion.keys()):
        if clave_vieja not in claves_vivas:
            cerrar_seguimiento_venta(clave_vieja)

    mercado_actual = None
    mercados_cerrados_avisados = set()
    for mercado, pos in posiciones_con_mercado:
        if mercado != mercado_actual:
            log(f"\n########## VENTAS - MERCADO {mercado} ##########")
            mercado_actual = mercado

        # Si el mercado de esta posicion no esta en horario operativo ahora
        # mismo, NO se intenta vender: IBKR rechaza/cancela las ordenes
        # fuera de la sesion de negociacion (visto en produccion con HK:
        # "Error 10349... Cancelled", y el bot llegaba a registrar
        # enganosamente "estado: PreSubmitted" como si la orden siguiera
        # viva). Se sigue mostrando el beneficio/perdida como informacion,
        # pero sin intentar operar. CRYPTO no esta en CIERRE_POR_MERCADO (no
        # tiene un unico cierre diario) pero tambien necesita este chequeo
        # con su propio horario (es_horario_operativo_cripto()).
        if (mercado in CIERRE_POR_MERCADO or mercado == "CRYPTO") and not es_horario_operativo(mercado):
            if mercado not in mercados_cerrados_avisados:
                log(f"VENTAS: mercado {mercado} fuera de horario operativo, no se intenta vender "
                    f"ninguna posicion de este mercado en este ciclo.")
                mercados_cerrados_avisados.add(mercado)
            continue

        contrato = pos.contract
        cantidad = pos.position
        coste_medio = pos.avgCost

        # Bug real de produccion (sept. 2026): para posiciones CRYPTO, el
        # contrato que devuelve ib.positions() viene con el campo `exchange`
        # vacio (a diferencia de las acciones), y reqHistoricalData lo
        # rechaza con el error 321 "Please enter exchange". La primera
        # correccion (rellenarlo a mano con EXCHANGE_CRYPTO) resulto ser
        # incorrecta: el contrato YA trae un conId real (el de la posicion
        # concreta que tiene el usuario), y forzar un `exchange` que no
        # corresponde a ese conId da el error 200 "No security definition
        # has been found for the request" (visto en produccion). La forma
        # segura de completar el contrato es dejar que IBKR lo resuelva el
        # mismo a partir de su propio conId, sin adivinar el nombre del
        # exchange (evita el problema de fondo PAXOS-vs-ZEROHASH: el conId
        # ya identifica sin ambiguedad el proveedor real de esa posicion).
        if mercado == "CRYPTO" and not contrato.exchange:
            ib.qualifyContracts(contrato)

        try:
            # Salvaguarda explicita: nunca vender mas acciones de las que
            # realmente hay en cartera (sin apalancamiento, sin venta en corto).
            # Esto ya esta garantizado por construccion (cantidad = pos.position,
            # y solo se procesan posiciones con position > 0), pero se deja esta
            # comprobacion como defensa adicional ante cualquier cambio futuro.
            if cantidad <= 0:
                continue

            if coste_medio <= 0:
                log(f"VENTAS: {contrato.symbol} - precio medio invalido ({coste_medio}), se omite por seguridad.")
                continue

            # Nota: el contrato que llega de ib.positions() ya viene calificado
            # (trae conId), asi que no hace falta volver a llamar a qualifyContracts.
            velas_precio = pedir_velas(ib, contrato, '1 D', '1 min')
            if not velas_precio:
                log(f"VENTAS: {contrato.symbol} - no se pudo obtener precio actual, se omite.")
                continue

            precio_actual = velas_precio[-1].close
            beneficio_pct_bruto = (precio_actual - coste_medio) / coste_medio * 100

            # Comision total estimada para la operacion de ida y vuelta: la
            # compra y la venta se calculan por SEPARADO (cada orden real
            # paga su propio minimo en IBKR, no un unico minimo compartido)
            # y se suman.
            valor_compra = cantidad * coste_medio
            valor_venta = cantidad * precio_actual
            if mercado == "CRYPTO":
                comision_compra = estimar_comision_cripto(valor_compra)
                comision_venta = estimar_comision_cripto(valor_venta)
            else:
                comision_compra = estimar_comision(valor_compra, contrato.currency, cantidad)
                comision_venta = estimar_comision(valor_venta, contrato.currency, cantidad)
            comision_total = comision_compra + comision_venta
            comision_total_pct = comision_total / valor_compra * 100
            beneficio_pct = beneficio_pct_bruto - comision_total_pct

            # Prefijo comun con los datos base de la posicion, reutilizado en
            # todas las lineas de log de esta operacion.
            info_posicion = (f"{cantidad:g} acciones, precio medio {coste_medio:.4f} {contrato.currency}, "
                              f"comision estimada {comision_total:.2f} {contrato.currency}")

            if mercado == "CRYPTO":
                # Cripto no tiene "cierre diario" -> nunca hay venta forzada
                # (en_ventana_venta_forzada siempre da False al no estar en
                # CIERRE_POR_MERCADO). Mismo criterio de venta que acciones
                # -trailing stop + refuerzo de 2 velas + salida parcial,
                # peticion del usuario, sept. 2026-, pero con el umbral y la
                # comision propios de cripto, y su propia construccion de
                # orden (LMT + totalQuantity fraccionario nativo, sin cashQty
                # ni Plan B/C -ver crear_orden_limitada_cripto()-).
                clave_posicion_cripto = clave_historial(mercado, contrato.symbol)
                accion_cripto, motivo_cripto = decidir_accion_venta(
                    clave_posicion_cripto, beneficio_pct, UMBRAL_BENEFICIO_CRYPTO_PCT)

                if accion_cripto == "MANTENER" and beneficio_pct >= UMBRAL_BENEFICIO_CRYPTO_PCT:
                    if macd_5min_bajista_2_velas(ib, contrato):
                        accion_cripto, motivo_cripto = "VENTA_TOTAL", "2 velas de 5min bajistas seguidas"

                if accion_cripto == "MANTENER":
                    log(f"VENTAS: {contrato.symbol} - {info_posicion} - beneficio neto {beneficio_pct:.2f}% "
                        f"(bruto {beneficio_pct_bruto:.2f}%) {motivo_cripto} -> se mantiene.")
                    continue

                min_size_cripto, incremento_cripto, tick_cripto_venta = obtener_detalles_cripto(ib, contrato)
                cantidad_a_vender_cripto = cantidad
                if accion_cripto == "VENTA_PARCIAL":
                    parcial_cripto = redondear_a_incremento(cantidad * PORCENTAJE_SCALE_OUT, incremento_cripto)
                    if parcial_cripto <= 0 or (min_size_cripto > 0 and parcial_cripto < min_size_cripto) \
                            or parcial_cripto >= cantidad:
                        # Cantidad demasiado pequeña para dividir respetando
                        # el incremento/minimo del exchange: se vende todo.
                        accion_cripto, cantidad_a_vender_cripto = "VENTA_TOTAL", cantidad
                    else:
                        cantidad_a_vender_cripto = parcial_cripto

                etiqueta_cripto = "VENTA PARCIAL" if accion_cripto == "VENTA_PARCIAL" else "VENTA"
                log(f"VENTAS: {contrato.symbol} - {info_posicion} - beneficio neto {beneficio_pct:.2f}% "
                    f"(bruto {beneficio_pct_bruto:.2f}%), {motivo_cripto} -> {etiqueta_cripto} de "
                    f"{cantidad_a_vender_cripto:g} (orden limitada).")
                precio_limite_cripto = calcular_precio_limite_venta(precio_actual, contrato.currency)
                precio_limite_cripto = redondear_precio_a_tick(precio_limite_cripto, tick_cripto_venta)
                orden = crear_orden_limitada_cripto('SELL', cantidad_a_vender_cripto, precio_limite_cripto)
                trade = ib.placeOrder(contrato, orden)
                estado = esperar_estado_final_orden(ib, trade)
                log(f"VENTAS: {contrato.symbol} - orden limitada a {precio_limite_cripto} USD, estado: {estado}")
                if estado == 'Filled':
                    precio_ejecucion = getattr(trade.orderStatus, "avgFillPrice", None) or precio_limite_cripto
                    cantidad_ejecutada = getattr(trade.orderStatus, "filled", None) or cantidad_a_vender_cripto
                    registrar_operacion_historial(mercado, contrato.symbol, "VENTA", cantidad_ejecutada,
                                                   precio_ejecucion, comision_total, contrato.currency,
                                                   coste_medio=coste_medio, beneficio_pct=beneficio_pct)
                    notificar_telegram(f"🔴 {etiqueta_cripto} <b>{contrato.symbol}</b> ({mercado}): "
                                       f"{formato_es(cantidad_ejecutada, 6)} a {formato_es(precio_ejecucion, 4)} "
                                       f"{contrato.currency} ({formato_es(beneficio_pct, signo=True)}%)")
                    if accion_cripto == "VENTA_TOTAL":
                        cerrar_seguimiento_venta(clave_posicion_cripto)
                else:
                    verificar_posicion_tras_orden_no_confirmada(ib, contrato, cantidad_a_vender_cripto, f"VENTAS: {contrato.symbol}")
                continue

            if (mercado != "?" and en_ventana_venta_forzada(mercado)
                    and UMBRAL_BENEFICIO_PCT <= beneficio_pct <= BENEFICIO_MAX_VENTA_FORZADA_PCT):
                log(f"VENTAS: {contrato.symbol} - {info_posicion} - beneficio neto {beneficio_pct:.2f}% "
                    f"(bruto {beneficio_pct_bruto:.2f}%), dentro de los ultimos "
                    f"{MINUTOS_VENTA_FORZADA_ANTES_CIERRE} min antes del cierre de {mercado} -> "
                    f"VENTA FORZADA (orden a mercado).")
                # A mercado, no limitada (peticion del usuario, sept. 2026):
                # el objetivo de la venta forzada es GARANTIZAR la salida
                # antes del cierre, y una orden limitada corria el riesgo de
                # no ejecutarse a tiempo si el precio se alejaba del limite
                # -se prioriza la ejecucion segura sobre el pequeño margen
                # de precio que daba el limite anterior (0.2%)-.
                if es_cantidad_fraccionaria(cantidad):
                    # Igual que en las compras: algunas cuentas/valores
                    # rechazan el importe en efectivo (cashQty) con el error
                    # 10244, asi que se manda por importe en efectivo primero
                    # y, si lo rechaza, se reintenta con la cantidad
                    # fraccionaria puesta DIRECTAMENTE (confirmado a mano en
                    # cuenta real que esto SI funciona, vease Plan B de
                    # revisar_compras para el detalle completo).
                    orden = crear_orden_mercado_cash('SELL', cantidad * precio_actual)
                else:
                    orden = crear_orden_mercado('SELL', cantidad)
                trade = ib.placeOrder(contrato, orden)
                estado = esperar_estado_final_orden(ib, trade)
                log(f"VENTAS: {contrato.symbol} - orden a mercado, estado: {estado}")
                if (es_cantidad_fraccionaria(cantidad) and estado != 'Filled'
                        and orden_rechazada_por_codigo(trade, {10244})):
                    log(f"VENTAS: {contrato.symbol} - no admite el importe en efectivo (cashQty) via API "
                        f"(error 10244); reintentando con la cantidad fraccionaria puesta directamente.")
                    orden = crear_orden_mercado('SELL', cantidad)
                    trade = ib.placeOrder(contrato, orden)
                    estado = esperar_estado_final_orden(ib, trade)
                    log(f"VENTAS: {contrato.symbol} - orden a mercado (cantidad fraccionaria directa), "
                        f"estado: {estado}")
                if estado == 'Filled':
                    precio_ejecucion = getattr(trade.orderStatus, "avgFillPrice", None) or precio_actual
                    cantidad_ejecutada = getattr(trade.orderStatus, "filled", None) or cantidad
                    registrar_operacion_historial(mercado, contrato.symbol, "VENTA", cantidad_ejecutada,
                                                   precio_ejecucion, comision_total, contrato.currency,
                                                   coste_medio=coste_medio, beneficio_pct=beneficio_pct)
                    notificar_telegram(f"🔴 VENTA FORZADA <b>{contrato.symbol}</b> ({mercado}): "
                                       f"{formato_es(cantidad_ejecutada, 4)} a {formato_es(precio_ejecucion, 4)} "
                                       f"{contrato.currency} ({formato_es(beneficio_pct, signo=True)}%)")
                    cerrar_seguimiento_venta(clave_historial(mercado, contrato.symbol))
                else:
                    verificar_posicion_tras_orden_no_confirmada(ib, contrato, cantidad, f"VENTAS: {contrato.symbol}")
                continue

            # Criterio de venta: trailing stop (principal) + 2 velas de 5min
            # bajistas seguidas (refuerzo) + salida parcial al alcanzar el
            # objetivo (peticion del usuario, sept. 2026) -ver
            # decidir_accion_venta()/PORCENTAJE_SCALE_OUT mas arriba-. El
            # umbral minimo (UMBRAL_BENEFICIO_PCT) ya esta en NETO (descontada
            # la comision de compra+venta, ver mas arriba).
            clave_posicion = clave_historial(mercado, contrato.symbol)
            accion, motivo = decidir_accion_venta(clave_posicion, beneficio_pct, UMBRAL_BENEFICIO_PCT)

            if accion == "MANTENER" and beneficio_pct >= UMBRAL_BENEFICIO_PCT:
                if macd_5min_bajista_2_velas(ib, contrato):
                    accion, motivo = "VENTA_TOTAL", "2 velas de 5min bajistas seguidas"

            if accion == "MANTENER":
                log(f"VENTAS: {contrato.symbol} - {info_posicion} - beneficio neto {beneficio_pct:.2f}% "
                    f"(bruto {beneficio_pct_bruto:.2f}%) {motivo} -> se mantiene.")
                continue

            cantidad_a_vender = cantidad
            if accion == "VENTA_PARCIAL":
                parcial = cantidad * PORCENTAJE_SCALE_OUT
                parcial = round(parcial, DECIMALES_FRACCION) if FRACCIONABLE_POR_MERCADO.get(mercado, False) else int(parcial)
                if parcial <= 0 or parcial >= cantidad:
                    # Posicion demasiado pequeña para dividir con sentido:
                    # se vende todo de una vez en su lugar.
                    accion, cantidad_a_vender = "VENTA_TOTAL", cantidad
                else:
                    cantidad_a_vender = parcial

            # Pre o postmercado de US: liquidez mucho menor que en sesion
            # regular, se usa orden LIMITADA al precio exacto (con
            # outsideRth) en vez de orden a mercado.
            usar_limite_fuera_horario = mercado == "US" and fuera_de_sesion_regular_us()
            tipo_orden_texto = "limitada al precio exacto (fuera de sesion regular)" if usar_limite_fuera_horario else "a mercado"
            etiqueta_accion = "VENTA PARCIAL" if accion == "VENTA_PARCIAL" else "VENTA"
            log(f"VENTAS: {contrato.symbol} - {info_posicion} - beneficio neto {beneficio_pct:.2f}% "
                f"(bruto {beneficio_pct_bruto:.2f}%), {motivo} -> {etiqueta_accion} de {cantidad_a_vender:g} "
                f"(orden {tipo_orden_texto}).")

            def _orden_venta_cash(importe):
                if usar_limite_fuera_horario:
                    return crear_orden_limitada_cash('SELL', importe, precio_actual, fuera_horario_regular=True)
                return crear_orden_mercado_cash('SELL', importe)

            def _orden_venta(cant):
                if usar_limite_fuera_horario:
                    return crear_orden_limitada('SELL', cant, precio_actual, fuera_horario_regular=True)
                return crear_orden_mercado('SELL', cant)

            if es_cantidad_fraccionaria(cantidad_a_vender):
                orden = _orden_venta_cash(cantidad_a_vender * precio_actual)
            else:
                orden = _orden_venta(cantidad_a_vender)
            trade = ib.placeOrder(contrato, orden)
            estado = esperar_estado_final_orden(ib, trade)
            log(f"VENTAS: {contrato.symbol} - orden {tipo_orden_texto}, "
                f"estado: {estado}")
            if (es_cantidad_fraccionaria(cantidad_a_vender) and estado != 'Filled'
                    and orden_rechazada_por_codigo(trade, {10244})):
                # Ver Plan B en revisar_compras: la cuenta puede rechazar
                # cashQty (10244) pero SI admitir la cantidad fraccionaria
                # puesta directamente (confirmado a mano en cuenta real).
                log(f"VENTAS: {contrato.symbol} - no admite el importe en efectivo (cashQty) via API "
                    f"(error 10244); reintentando con la cantidad fraccionaria puesta directamente.")
                orden = _orden_venta(cantidad_a_vender)
                trade = ib.placeOrder(contrato, orden)
                estado = esperar_estado_final_orden(ib, trade)
                log(f"VENTAS: {contrato.symbol} - orden {tipo_orden_texto} (cantidad fraccionaria directa), "
                    f"estado: {estado}")
            if estado == 'Filled':
                precio_ejecucion = getattr(trade.orderStatus, "avgFillPrice", None) or precio_actual
                cantidad_ejecutada = getattr(trade.orderStatus, "filled", None) or cantidad_a_vender
                registrar_operacion_historial(mercado, contrato.symbol, "VENTA", cantidad_ejecutada,
                                               precio_ejecucion, comision_total, contrato.currency,
                                               coste_medio=coste_medio, beneficio_pct=beneficio_pct)
                notificar_telegram(f"🔴 {etiqueta_accion} <b>{contrato.symbol}</b> ({mercado}): "
                                   f"{formato_es(cantidad_ejecutada, 4)} a {formato_es(precio_ejecucion, 4)} "
                                   f"{contrato.currency} ({formato_es(beneficio_pct, signo=True)}%)")
                if accion == "VENTA_TOTAL":
                    cerrar_seguimiento_venta(clave_posicion)
            else:
                verificar_posicion_tras_orden_no_confirmada(ib, contrato, cantidad_a_vender, f"VENTAS: {contrato.symbol}")
        except Exception as e:
            # Un fallo al procesar UNA posicion (p.ej. dato raro, error de red al
            # colocar la orden) no debe abortar la revision de las demas
            # posiciones abiertas ni saltarse por completo el escaneo de compras
            # de este ciclo.
            log(f"VENTAS: {contrato.symbol} - ERROR inesperado al procesar la posicion: {type(e).__name__}: {e}. Se omite.")


def obtener_valor_total_cartera_usd(ib):
    """Devuelve el NetLiquidation total de la cuenta en USD."""
    try:
        resumen = ib.accountSummary()
    except Exception as e:
        log(f"No se pudo obtener el resumen de la cuenta: {type(e).__name__}: {e}")
        return None
    for item in resumen:
        if item.tag == 'NetLiquidation' and item.currency == 'USD':
            return float(item.value)
    for item in resumen:
        if item.tag == 'NetLiquidation':
            return float(item.value)
    return None


def obtener_fondos_disponibles_usd(ib):
    """Devuelve el AvailableFunds (efectivo/margen realmente disponible para
    nuevas compras, no el valor total de la cartera) en USD. Se usa en
    revisar_compras() para no intentar comprar mas de lo que la cuenta
    puede permitirse de verdad, ademas de los limites por % ya existentes
    (peticion del usuario, sept. 2026)."""
    try:
        resumen = ib.accountSummary()
    except Exception as e:
        log(f"No se pudo obtener AvailableFunds de la cuenta: {type(e).__name__}: {e}")
        return None
    for item in resumen:
        if item.tag == 'AvailableFunds' and item.currency == 'USD':
            return float(item.value)
    for item in resumen:
        if item.tag == 'AvailableFunds':
            return float(item.value)
    return None


def obtener_valor_posicion_actual_usd(posiciones, ticker, precio_actual, currency):
    """Devuelve el valor en USD de la posicion actual de un ticker (0 si no hay)."""
    for pos in posiciones:
        if pos.contract.symbol == ticker and pos.position > 0:
            return valor_en_usd(pos.position * precio_actual, currency)
    return 0.0


def calcular_exposicion_total_cripto_usd(posiciones):
    """Suma el valor en USD de TODAS las posiciones de CRYPTO abiertas, a
    COSTE (avgCost * cantidad), no al precio actual -aproximacion razonable
    para comprobar el limite de exposicion total de IBKR (ver
    LIMITE_EXPOSICION_CRYPTO_TOTAL_PCT) sin tener que pedir el precio actual
    de cada criptomoneda por separado solo para esta comprobacion."""
    total = 0.0
    for pos in posiciones:
        if pos.position > 0 and mercado_de_posicion(pos) == "CRYPTO":
            total += valor_en_usd(pos.position * pos.avgCost, pos.contract.currency)
    return total


def tick_size_krx(precio):
    """Devuelve el incremento de precio minimo (tick) que exige KRX segun
    el rango de precio del valor. Tabla oficial de Korea Exchange."""
    if precio < 2000:
        return 1
    elif precio < 5000:
        return 5
    elif precio < 20000:
        return 10
    elif precio < 50000:
        return 50
    elif precio < 200000:
        return 100
    elif precio < 500000:
        return 500
    else:
        return 1000


def calcular_precio_limite_venta(precio_actual, currency):
    """Calcula el precio limite para una orden de venta: un margen pequeno
    por debajo del precio actual, para seguir siendo ejecutable rapido pero
    con proteccion frente a deslizamientos grandes."""
    precio_limite = precio_actual * (1 - MARGEN_ORDEN_LIMITADA_VENTA_PCT / 100)
    if currency == "KRW":
        # KRX exige que el precio caiga en un "escalon" (tick) concreto
        # segun el rango de precio; redondeamos hacia abajo al escalon
        # valido mas cercano (mas favorable para que la venta se ejecute).
        tick = tick_size_krx(precio_limite)
        return int((precio_limite // tick) * tick)
    return round(precio_limite, 2)


def obtener_incremento_lote(ib, contrato):
    """Consulta a IBKR el tamano minimo de lote/incremento para un contrato
    (relevante sobre todo en Hong Kong, donde las acciones se compran en
    lotes fijos, no en unidades sueltas). Devuelve (minSize, sizeIncrement)
    como ENTEROS limpios (los lotes de bolsa siempre son numeros enteros de
    acciones; IBKR a veces devuelve estos valores con ruido de coma flotante,
    p.ej. 0.999999999 en vez de 1.0, que hay que corregir aqui)."""
    try:
        detalles = ib.reqContractDetails(contrato)
        if detalles:
            cd = detalles[0]
            min_size = round(float(getattr(cd, 'minSize', 0) or 1))
            incremento = round(float(getattr(cd, 'sizeIncrement', 0) or 1))
            if min_size <= 0:
                min_size = 1
            if incremento <= 0:
                incremento = 1
            return min_size, incremento
    except Exception:
        pass
    return 1, 1


def obtener_detalles_cripto(ib, contrato):
    """Version de obtener_incremento_lote() para CRYPTO: a diferencia de las
    acciones (lotes siempre enteros), en cripto minSize/sizeIncrement/minTick
    son FRACCIONARIOS (p.ej. 0.00001 BTC, 0.001 LTC...) y no se pueden
    redondear a enteros. Devuelve (minSize, sizeIncrement, minTick) como
    floats tal cual los da IBKR; 0.0 en cualquiera de los tres si no se pudo
    consultar -en ese caso, quien llame debe decidir un fallback (ver su uso
    en revisar_compras/revisar_ventas).

    Bug real de produccion (sept. 2026), en dos partes:
    1) La CANTIDAD a comprar se redondeaba a un numero fijo de decimales
       (DECIMALES_FRACCION_CRIPTO) igual para todas las criptomonedas, sin
       respetar el incremento minimo real de cada una -para
       LTC/BCH/SOL/LINK, IBKR rechazaba la orden con "Error 202: Order
       Canceled - reason: Invalid order". Se probo a redondear la cantidad
       a `sizeIncrement`, pero el error PERSISTIO igual tras ese cambio.
    2) La causa real resulto ser el PRECIO, no la cantidad: `precio_actual`
       (el cierre de la ultima vela, con la precision que sea que traiga el
       feed de datos) se pasaba tal cual como precio limite, sin redondear
       al "tick" de precio valido para ese contrato -que si estaba mal,
       tambien da el mismo "Error 202: Invalid order", indistinguible del
       problema de cantidad sin mirar el campo minTick-. Se redondea ahora
       tambien el precio a minTick (ver redondear_precio_a_tick())."""
    try:
        detalles = ib.reqContractDetails(contrato)
        if detalles:
            cd = detalles[0]
            min_size = float(getattr(cd, 'minSize', 0) or 0)
            incremento = float(getattr(cd, 'sizeIncrement', 0) or 0)
            min_tick = float(getattr(cd, 'minTick', 0) or 0)
            return min_size, incremento, min_tick
    except Exception:
        pass
    return 0.0, 0.0, 0.0


def redondear_a_incremento(cantidad, incremento):
    """Redondea `cantidad` HACIA ABAJO al multiplo valido mas cercano de
    `incremento` (nunca hacia arriba: no se debe comprar mas de lo que el
    presupuesto calculado permite). Si `incremento` es 0 o invalido,
    devuelve `cantidad` sin tocar -no se pudo consultar el incremento real,
    ver obtener_detalles_cripto()-."""
    if incremento <= 0:
        return cantidad
    # round() limpia el ruido de coma flotante que suele dejar la division
    # (p.ej. 0.7999999999999999 en vez de 0.8) antes de multiplicar nada.
    pasos = math.floor(round(cantidad / incremento, 10))
    return round(pasos * incremento, 10)


def redondear_precio_a_tick(precio, tick):
    """Redondea `precio` al multiplo valido MAS CERCANO de `tick` (a
    diferencia de redondear_a_incremento, aqui no importa la direccion: lo
    unico que exige el exchange es que el precio caiga en un escalon
    valido, no proteger un presupuesto). Si `tick` es 0 o invalido, devuelve
    `precio` sin tocar -no se pudo consultar el tick real, ver
    obtener_detalles_cripto()-."""
    if tick <= 0:
        return precio
    pasos = round(precio / tick)
    return round(pasos * tick, 10)


def revisar_compras(ib, mercados=None):
    """Ver revisar_ventas() para el significado de `mercados`."""
    valor_total_cartera_usd = obtener_valor_total_cartera_usd(ib)
    if valor_total_cartera_usd is None:
        log("COMPRAS: no se pudo obtener el valor total de la cartera, se omite este ciclo de compras.")
        return

    limite_por_valor_usd = valor_total_cartera_usd * (LIMITE_EXPOSICION_PCT / 100)

    ib.reqPositions()
    ib.sleep(1)  # da tiempo a que la respuesta llegue antes de leer ib.positions()
    posiciones_actuales = ib.positions()

    # Tope de posiciones simultaneas y caja disponible (ver
    # MAX_POSICIONES_ABIERTAS): se calculan UNA vez al principio del ciclo y
    # se van reservando de forma optimista segun se decide cada compra (no
    # hace falta esperar a que la orden se confirme como filled), para que
    # varias señales dentro del MISMO ciclo no se salten el limite entre
    # ellas.
    posiciones_abiertas_tickers = {p.contract.symbol for p in posiciones_actuales if p.position > 0}
    fondos_disponibles_usd = obtener_fondos_disponibles_usd(ib)
    if fondos_disponibles_usd is None:
        log("COMPRAS: no se pudo obtener AvailableFunds de la cuenta; no se aplicara el limite de "
            "caja disponible este ciclo (el resto de limites de exposicion siguen activos).")

    mercados_ya_avisados = set()
    mercado_actual = None
    contadores = {"analizados": 0, "senales": 0, "errores": 0}

    def imprimir_resumen_mercado():
        if mercado_actual is not None:
            log(f"{mercado_actual}: {contadores['analizados']} analizados, "
                f"{contadores['senales']} señales de compra, {contadores['errores']} errores.")

    for activo in ACTIVOS:
        if mercados is not None and activo["mercado"] not in mercados:
            continue
        if activo["mercado"] != mercado_actual:
            imprimir_resumen_mercado()
            log(f"\n########## COMPRAS - MERCADO {activo['mercado']} ##########")
            mercado_actual = activo["mercado"]
            contadores = {"analizados": 0, "senales": 0, "errores": 0}

        if not es_horario_operativo(activo["mercado"]):
            continue  # este valor esta fuera de horario en su mercado, se omite en este ciclo

        # Postmercado de US (16:00-20:00 ET): decision explicita del usuario
        # de solo VENDER en este tramo, no comprar (liquidez mucho menor que
        # en sesion regular). Las compras de US se reanudan al dia siguiente
        # en premercado/sesion regular.
        if activo["mercado"] == "US" and en_postmercado_us():
            if activo["mercado"] not in mercados_ya_avisados:
                log(f"COMPRAS: mercado {activo['mercado']} en postmercado (16:00-20:00 ET), no se "
                    f"analiza ningun valor en busca de señales de compra en este ciclo "
                    f"(solo se vende en este tramo).")
                mercados_ya_avisados.add(activo["mercado"])
            continue

        contadores["analizados"] += 1
        ticker = activo["ticker"]
        try:
            contrato, decision = analizar_activo(ib, activo)
        except Exception as e:
            log(f"COMPRAS: {ticker} ({activo['mercado']}) - ERROR al analizar: {type(e).__name__}: {e}")
            contadores["errores"] += 1
            continue

        if decision == "SIMBOLO_NO_RESUELTO":
            log(f"COMPRAS: {ticker} ({activo['mercado']}) - simbolo/exchange no reconocido por IBKR "
                f"(revisar permisos de trading en ese mercado o el codigo de simbolo), se omite.")
            contadores["errores"] += 1
            continue

        if decision != "COMPRA":
            continue

        contadores["senales"] += 1

        try:
            velas_precio = pedir_velas(ib, contrato, '1 D', '1 min')
            if not velas_precio:
                log(f"COMPRAS: {ticker} - senal de COMPRA pero no se pudo obtener precio, se omite.")
                continue

            precio_actual = velas_precio[-1].close
            currency = activo["currency"]

            # Pre/postmercado de US: liquidez mucho menor que en sesion
            # regular, asi que la compra se manda como orden LIMITADA al
            # precio exacto (con outsideRth) en vez de orden a mercado.
            usar_limite_fuera_horario = activo["mercado"] == "US" and fuera_de_sesion_regular_us()

            def _orden_compra_cash(importe):
                if usar_limite_fuera_horario:
                    return crear_orden_limitada_cash('BUY', importe, precio_actual, fuera_horario_regular=True)
                return crear_orden_mercado_cash('BUY', importe)

            def _orden_compra(cant):
                if usar_limite_fuera_horario:
                    return crear_orden_limitada('BUY', cant, precio_actual, fuera_horario_regular=True)
                return crear_orden_mercado('BUY', cant)

            if en_ventana_sin_compra(activo["mercado"]):
                # Dentro de los ultimos MINUTOS_SIN_COMPRAR_ANTES_CIERRE minutos:
                # solo se compra si ya se tiene el valor Y el precio actual es
                # MENOR que el coste medio (es decir, comprar ahora bajaria el
                # precio medio de adquisicion). En cualquier otro caso, se omite.
                pos_existente = next((p for p in posiciones_actuales
                                       if p.contract.symbol == ticker and p.position > 0), None)

                if pos_existente is None or precio_actual >= pos_existente.avgCost:
                    if activo["mercado"] not in mercados_ya_avisados:
                        log(f"COMPRAS: mercado {activo['mercado']} dentro de la ventana de no-compra "
                            f"(ultimos {MINUTOS_SIN_COMPRAR_ANTES_CIERRE} min antes del cierre) -> solo se compra "
                            f"si promedia a la baja una posicion existente.")
                        mercados_ya_avisados.add(activo["mercado"])
                    continue

                log(f"COMPRAS: {ticker} - dentro de la ventana de no-compra, pero el precio actual "
                    f"({precio_actual} {currency}) es menor que el coste medio existente "
                    f"({pos_existente.avgCost:.4f} {currency}) -> se permite comprar para promediar a la baja.")

            valor_posicion_actual_usd = obtener_valor_posicion_actual_usd(posiciones_actuales, ticker, precio_actual, currency)
            margen_disponible_usd = limite_por_valor_usd - valor_posicion_actual_usd

            if margen_disponible_usd <= 0:
                log(f"COMPRAS: {ticker} - senal de COMPRA pero ya tiene {valor_posicion_actual_usd:.2f} USD "
                    f"({LIMITE_EXPOSICION_PCT}% del limite = {limite_por_valor_usd:.2f} USD alcanzado) -> se omite.")
                continue

            # Presupuesto maximo por operacion: equivalente a IMPORTE_EUROS, en la
            # divisa del propio valor, capado ademas por el margen disponible del
            # limite del 15% (convertido a esa divisa).
            if currency == "USD":
                presupuesto_operacion = IMPORTE_EUROS * TIPO_CAMBIO_EUR_USD
                margen_disponible_moneda = margen_disponible_usd
            elif currency == "EUR":
                presupuesto_operacion = IMPORTE_EUROS
                margen_disponible_moneda = margen_disponible_usd / TIPO_CAMBIO_EUR_USD
            elif currency == "HKD":
                presupuesto_operacion = IMPORTE_EUROS_HK * TIPO_CAMBIO_EUR_USD * TIPO_CAMBIO_USD_HKD
                margen_disponible_moneda = margen_disponible_usd * TIPO_CAMBIO_USD_HKD
            elif currency == "KRW":
                presupuesto_operacion = IMPORTE_EUROS * TIPO_CAMBIO_EUR_USD * TIPO_CAMBIO_USD_KRW
                margen_disponible_moneda = margen_disponible_usd * TIPO_CAMBIO_USD_KRW
            else:
                presupuesto_operacion = 0
                margen_disponible_moneda = 0

            importe_a_usar = min(presupuesto_operacion, margen_disponible_moneda)

            # Tope de posiciones simultaneas: solo se aplica a valores NUEVOS
            # (si ya se tiene el ticker, esto es promediar/añadir, no abrir
            # una posicion mas) - ver MAX_POSICIONES_ABIERTAS.
            ya_tiene_posicion = valor_posicion_actual_usd > 0
            if not ya_tiene_posicion and len(posiciones_abiertas_tickers) >= MAX_POSICIONES_ABIERTAS:
                log(f"COMPRAS: {ticker} - señal de COMPRA pero ya hay {len(posiciones_abiertas_tickers)} "
                    f"posiciones abiertas (limite MAX_POSICIONES_ABIERTAS={MAX_POSICIONES_ABIERTAS}) y "
                    f"este seria un valor nuevo, se omite.")
                continue

            importe_a_usar_usd = valor_en_usd(importe_a_usar, currency)
            if fondos_disponibles_usd is not None and importe_a_usar_usd > fondos_disponibles_usd:
                log(f"COMPRAS: {ticker} - señal de COMPRA pero el importe ({importe_a_usar_usd:.2f} USD) "
                    f"supera los fondos disponibles restantes en la cuenta ({fondos_disponibles_usd:.2f} "
                    f"USD), se omite.")
                continue

            # Reserva optimista: se descuenta/anota AQUI, no tras confirmar la
            # orden, para que la SIGUIENTE señal de este mismo ciclo ya vea
            # el hueco/caja reducidos (evita que 2+ señales del mismo ciclo
            # se salten el limite entre ellas). Es deliberadamente
            # conservador: si la orden acaba rechazada, se pierde margen
            # para el resto del ciclo, pero nunca se compra de mas.
            posiciones_abiertas_tickers.add(ticker)
            if fondos_disponibles_usd is not None:
                fondos_disponibles_usd -= importe_a_usar_usd

            if activo["mercado"] == "CRYPTO":
                # Cripto siempre admite fracciones nativas via LMT (ver
                # crear_orden_limitada_cripto()): sin cashQty, sin Plan B/C,
                # sin distincion pre/postmercado (opera de forma continua).

                # Limite propio de IBKR (no de este codigo, ver
                # LIMITE_EXPOSICION_CRYPTO_TOTAL_PCT): el CONJUNTO de todas
                # las posiciones de cripto no puede superar el 30% del
                # equity de la cuenta. Se comprueba ANTES de intentar la
                # orden (con margen de seguridad del 25%), para no
                # encadenar rechazos "Error 201: ... would cause your
                # crypto account(s) to exceed..." en cada ciclo.
                exposicion_cripto_actual_usd = calcular_exposicion_total_cripto_usd(posiciones_actuales)
                limite_cripto_total_usd = valor_total_cartera_usd * (LIMITE_EXPOSICION_CRYPTO_TOTAL_PCT / 100)
                if exposicion_cripto_actual_usd + importe_a_usar > limite_cripto_total_usd:
                    log(f"COMPRAS: {ticker} - senal de COMPRA pero comprar {importe_a_usar:.2f} USD mas "
                        f"superaria el limite de exposicion TOTAL en cripto ({LIMITE_EXPOSICION_CRYPTO_TOTAL_PCT}% "
                        f"de la cartera = {limite_cripto_total_usd:.2f} USD; ya invertido en cripto: "
                        f"{exposicion_cripto_actual_usd:.2f} USD) -limite real de IBKR: 30% del equity-, se omite.")
                    continue

                if importe_a_usar < VALOR_MINIMO_OPERACION_CRIPTO_USD:
                    log(f"COMPRAS: {ticker} - senal de COMPRA pero el margen disponible "
                        f"({importe_a_usar:.2f} USD) no llega al minimo de "
                        f"{VALOR_MINIMO_OPERACION_CRIPTO_USD:.2f} USD por operacion, se omite.")
                    continue

                cantidad_cripto = round(importe_a_usar / precio_actual, DECIMALES_FRACCION_CRIPTO)
                min_size_cripto, incremento_cripto, tick_cripto = obtener_detalles_cripto(ib, contrato)
                cantidad_cripto = redondear_a_incremento(cantidad_cripto, incremento_cripto)
                precio_compra_cripto = redondear_precio_a_tick(precio_actual, tick_cripto)
                if cantidad_cripto <= 0 or (min_size_cripto > 0 and cantidad_cripto < min_size_cripto):
                    log(f"COMPRAS: {ticker} - senal de COMPRA pero el importe calculado ({importe_a_usar:.2f} "
                        f"USD) no llega a una cantidad valida al precio actual ({precio_actual} USD) "
                        f"respetando el incremento minimo del exchange ({incremento_cripto:g}), se omite.")
                    continue

                log(f"COMPRAS: {ticker} (CRYPTO) - senal de COMPRA, comprando ~{cantidad_cripto:g} unidades "
                    f"a ~{precio_compra_cripto:g} USD (posicion actual: {valor_posicion_actual_usd:.2f} USD, "
                    f"limite: {limite_por_valor_usd:.2f} USD).")
                cantidad_antes_compra_cripto = next(
                    (p.position for p in posiciones_actuales if p.contract.symbol == ticker and p.position > 0), 0.0)

                orden = crear_orden_limitada_cripto('BUY', cantidad_cripto, precio_compra_cripto)
                trade = ib.placeOrder(contrato, orden)
                estado = esperar_estado_final_orden(ib, trade)
                log(f"COMPRAS: {ticker} - estado de la orden: {estado}")

                compra_confirmada_cripto = estado == 'Filled'
                if not compra_confirmada_cripto:
                    cantidad_tras_compra_cripto = verificar_posicion_tras_orden_no_confirmada(
                        ib, contrato, cantidad_antes_compra_cripto, f"COMPRAS: {ticker}")
                    compra_confirmada_cripto = cantidad_tras_compra_cripto > cantidad_antes_compra_cripto + 1e-6

                if compra_confirmada_cripto:
                    precio_ejecucion = getattr(trade.orderStatus, "avgFillPrice", None) or precio_compra_cripto
                    cantidad_ejecutada = getattr(trade.orderStatus, "filled", None) or cantidad_cripto
                    comision_ejecucion = estimar_comision_cripto(cantidad_ejecutada * precio_ejecucion)
                    registrar_operacion_historial(activo["mercado"], ticker, "COMPRA", cantidad_ejecutada,
                                                   precio_ejecucion, comision_ejecucion, currency)
                    if cantidad_antes_compra_cripto <= 1e-6:
                        registrar_apertura_de_posicion(activo["mercado"], ticker)
                    notificar_telegram(f"🟢 COMPRA <b>{ticker}</b> ({activo['mercado']}): "
                                       f"{formato_es(cantidad_ejecutada, 6)} a {formato_es(precio_ejecucion, 4)} {currency}")
                continue

            fraccionable = FRACCIONABLE_POR_MERCADO.get(activo["mercado"], False)

            # Visto en produccion (agosto 2026): las fracciones de accion NO
            # funcionan via API fuera de la sesion regular, ni por cashQty ni
            # por cantidad directa (error 10243 "Please use desktop version
            # to place this order" con AMBOS metodos, para varios valores
            # distintos en pre/postmercado). Para no desperdiciar 2 ordenes
            # rechazadas por señal, en ese caso se va directo a acciones
            # ENTERAS (igual que el Plan C de mas abajo).
            fracciones_no_disponibles = fraccionable and usar_limite_fuera_horario

            if fraccionable and not fracciones_no_disponibles:
                # Mercado US: se permite comprar una fraccion de accion, para
                # poder invertir el presupuesto disponible aunque sea menor que
                # el precio de una accion entera (util con carteras pequeñas).
                # La orden se manda por IMPORTE EN EFECTIVO (cash quantity),
                # que es la unica forma que admite la API de IBKR para
                # cantidades fraccionarias (vease crear_orden_mercado_cash).
                cantidad = round(importe_a_usar / precio_actual, DECIMALES_FRACCION)
                if cantidad <= 0 or importe_a_usar < VALOR_MINIMO_OPERACION_FRACCIONARIA_USD:
                    log(f"COMPRAS: {ticker} - senal de COMPRA pero el margen disponible "
                        f"({importe_a_usar:.2f} {currency}) no llega al minimo de "
                        f"{VALOR_MINIMO_OPERACION_FRACCIONARIA_USD:.2f} {currency} por operacion, se omite.")
                    continue
            elif fracciones_no_disponibles:
                cantidad = int(importe_a_usar // precio_actual)
                if cantidad < 1:
                    log(f"COMPRAS: {ticker} - senal de COMPRA en pre/postmercado, pero las fracciones "
                        f"no funcionan fuera de sesion regular y el presupuesto ({importe_a_usar:.2f} "
                        f"{currency}) no llega ni para 1 accion entera a {precio_actual} {currency}, "
                        f"se omite.")
                    continue
            else:
                cantidad_bruta = int(importe_a_usar // precio_actual)
                min_size, incremento = obtener_incremento_lote(ib, contrato)
                # Redondeamos hacia abajo al multiplo de lote mas cercano, y si no
                # llega ni a un lote minimo, no se compra (evita el error 388 de
                # IBKR: "tamano de orden menor al minimo requerido").
                cantidad = int((cantidad_bruta // incremento) * incremento)
                if cantidad < min_size:
                    cantidad = 0

                if cantidad == 0:
                    log(f"COMPRAS: {ticker} - senal de COMPRA pero el margen disponible "
                        f"({importe_a_usar:.2f} {currency}) no alcanza para 1 lote minimo "
                        f"({min_size} unidades, incremento {incremento}) al precio actual "
                        f"({precio_actual} {currency}), se omite.")
                    continue

            log(f"COMPRAS: {ticker} ({activo['mercado']}) - senal de COMPRA, comprando {cantidad:g} acciones "
                f"a ~{precio_actual} {currency} (posicion actual: {valor_posicion_actual_usd:.2f} USD, "
                f"limite: {limite_por_valor_usd:.2f} USD)."
                + (" [pre/postmercado: orden limitada al precio exacto]" if usar_limite_fuera_horario else "")
                + (" [fracciones no disponibles fuera de sesion regular: acciones enteras directamente]"
                   if fracciones_no_disponibles else ""))
            cantidad_antes_compra = next(
                (p.position for p in posiciones_actuales if p.contract.symbol == ticker and p.position > 0), 0.0)

            if fraccionable and not fracciones_no_disponibles:
                orden = _orden_compra_cash(importe_a_usar)
            else:
                orden = _orden_compra(cantidad)
            trade = ib.placeOrder(contrato, orden)
            estado = esperar_estado_final_orden(ib, trade)
            log(f"COMPRAS: {ticker} - estado de la orden: {estado}")

            # Plan B: algunas cuentas/valores rechazan el importe en efectivo
            # (cashQty) con el error 10244 "La cantidad de efectivo no puede
            # utilizarse en esta orden" -visto en produccion tanto para
            # valores concretos en cuenta demo, como para TODOS los valores
            # en cuenta real-. Comprobado a mano en la cuenta real (compra de
            # PFE desde la app de IBKR) que la cuenta SI admite fracciones
            # de verdad, simplemente rechaza el mecanismo cashQty: una orden
            # a mercado con la cantidad fraccionaria puesta DIRECTAMENTE
            # (en vez de como importe en efectivo) se ejecuta sin problema.
            # Por eso, antes de rendirse a acciones enteras, se reintenta con
            # la cantidad fraccionaria original puesta directamente. NOTA:
            # este plan se salta por completo si fracciones_no_disponibles
            # (pre/postmercado) -ya se fue directo a acciones enteras arriba-.
            if fraccionable and not fracciones_no_disponibles and estado != 'Filled' and orden_rechazada_por_codigo(trade, {10244}):
                log(f"COMPRAS: {ticker} - este valor no admite el importe en efectivo (cashQty) via API "
                    f"(error 10244); reintentando con la cantidad fraccionaria puesta directamente "
                    f"({cantidad:g} acciones).")
                orden = _orden_compra(cantidad)
                trade = ib.placeOrder(contrato, orden)
                estado = esperar_estado_final_orden(ib, trade)
                log(f"COMPRAS: {ticker} - estado de la orden (cantidad fraccionaria directa): {estado}")

            # Plan C: si tampoco admite la cantidad fraccionaria directa
            # (error 10243, "no se puede introducir la orden de tamano
            # fraccionario a traves de la API"), se reintenta con acciones
            # ENTERAS en vez de rendirse del todo, siempre que el presupuesto
            # llegue para al menos 1. NOTA: no aplica si fracciones_no_disponibles,
            # porque en ese caso ya se compro directamente en acciones enteras.
            if (fraccionable and not fracciones_no_disponibles and estado != 'Filled'
                    and orden_rechazada_por_codigo(trade, {10243, 10244})):
                cantidad_entera_fallback = int(importe_a_usar // precio_actual)
                if cantidad_entera_fallback >= 1:
                    log(f"COMPRAS: {ticker} - este valor no admite fracciones via API (ni cashQty ni "
                        f"cantidad directa); reintentando con {cantidad_entera_fallback} acciones enteras.")
                    orden = _orden_compra(cantidad_entera_fallback)
                    trade = ib.placeOrder(contrato, orden)
                    estado = esperar_estado_final_orden(ib, trade)
                    log(f"COMPRAS: {ticker} - estado de la orden (acciones enteras): {estado}")
                else:
                    log(f"COMPRAS: {ticker} - este valor no admite fracciones via API (ni cashQty ni "
                        f"cantidad directa) y el presupuesto ({importe_a_usar:.2f} {currency}) no llega "
                        f"ni para 1 accion entera a {precio_actual} {currency}, se omite.")

            compra_confirmada = estado == 'Filled'
            if not compra_confirmada:
                cantidad_tras_compra = verificar_posicion_tras_orden_no_confirmada(
                    ib, contrato, cantidad_antes_compra, f"COMPRAS: {ticker}")
                compra_confirmada = cantidad_tras_compra > cantidad_antes_compra + 1e-6

            if compra_confirmada:
                precio_ejecucion = getattr(trade.orderStatus, "avgFillPrice", None) or precio_actual
                cantidad_ejecutada = getattr(trade.orderStatus, "filled", None) or cantidad
                comision_ejecucion = estimar_comision(cantidad_ejecutada * precio_ejecucion, currency, cantidad_ejecutada)
                registrar_operacion_historial(activo["mercado"], ticker, "COMPRA", cantidad_ejecutada,
                                               precio_ejecucion, comision_ejecucion, currency)
                if cantidad_antes_compra <= 1e-6:
                    # Posicion nueva desde cero (no una ampliacion para promediar
                    # a la baja): se registra AHORA como fecha de apertura, para
                    # que el resumen de cierre de mercado la muestre aunque
                    # reqExecutions() ya no la tenga en dias posteriores.
                    registrar_apertura_de_posicion(activo["mercado"], ticker)
                notificar_telegram(f"🟢 COMPRA <b>{ticker}</b> ({activo['mercado']}): "
                                   f"{formato_es(cantidad_ejecutada, 4)} a {formato_es(precio_ejecucion, 4)} {currency}")
        except Exception as e:
            # Un fallo al procesar UNA señal de compra (precio raro, error de
            # red al colocar la orden, etc.) no debe abortar el escaneo del
            # resto de valores de la lista.
            log(f"COMPRAS: {ticker} - ERROR inesperado al procesar la señal de compra: {type(e).__name__}: {e}. Se omite.")
            contadores["errores"] += 1

    imprimir_resumen_mercado()  # resumen del ultimo mercado procesado en el bucle


def justo_cerro_mercado(mercado):
    """True si el mercado ha cerrado en las ultimas horas (con un margen
    amplio para no perder el resumen si el bot estuvo desconectado o
    congelado justo en el momento del cierre). El control de duplicados
    (resumenes_enviados_hoy) evita que se dispare mas de una vez al dia."""
    if mercado not in CIERRE_POR_MERCADO:
        return False
    zona, hora_cierre = CIERRE_POR_MERCADO[mercado]
    ahora = datetime.now(zona)
    if ahora.weekday() >= 5 or (mercado == "US" and es_festivo_us(ahora.date())):
        return False
    cierre_hoy = ahora.replace(hour=hora_cierre.hour, minute=hora_cierre.minute,
                                second=0, microsecond=0)
    minutos_desde_cierre = (ahora - cierre_hoy).total_seconds() / 60
    margen = 240  # 4 horas: amplio margen de seguridad frente a caidas/congelaciones
    return 0 <= minutos_desde_cierre <= margen


HORA_RESUMEN_DIARIO_CRYPTO = dt_time(23, 55)  # ET, igual que el resto de horarios del bot


def justo_hora_resumen_cripto():
    """Cripto no tiene un cierre de mercado real (24/7), asi que
    justo_cerro_mercado() nunca da True para 'CRYPTO' (no esta en
    CIERRE_POR_MERCADO). Se dispara en su lugar a una hora fija del dia, con
    el mismo margen amplio de justo_cerro_mercado() por si el bot estuvo
    desconectado justo a esa hora. A diferencia de justo_cerro_mercado()
    (cuyos cierres son todos de tarde, lejos de medianoche), aqui la hora
    fijada (23:55 ET) esta pegada a medianoche, asi que la ventana de margen
    (+4h) cae en el dia SIGUIENTE: se calcula la ultima ocurrencia PASADA de
    la hora fijada (la de hoy si ya paso, si no la de ayer) en vez de asumir
    siempre la fecha de hoy."""
    ahora = datetime.now(ZONA_NY)
    candidato_hoy = ahora.replace(hour=HORA_RESUMEN_DIARIO_CRYPTO.hour, minute=HORA_RESUMEN_DIARIO_CRYPTO.minute,
                                   second=0, microsecond=0)
    ultima_ocurrencia = candidato_hoy if ahora >= candidato_hoy else candidato_hoy - timedelta(days=1)
    minutos_desde_resumen = (ahora - ultima_ocurrencia).total_seconds() / 60
    margen = 240  # 4 horas, igual que justo_cerro_mercado()
    return 0 <= minutos_desde_resumen <= margen


def valor_en_eur(valor, currency):
    """Convierte un valor a EUR, pasando por USD como paso intermedio."""
    return valor_en_usd(valor, currency) / TIPO_CAMBIO_EUR_USD


def generar_resumen_cierre_mercado(ib, mercado):
    """Genera y muestra una tabla con las posiciones abiertas (desde cuando,
    cantidad, coste medio con comision estimada) y las operaciones cerradas
    hoy (abierta desde, cerrada a las, beneficio % aprox., ganancia), para
    los valores del mercado indicado. Los importes se muestran en moneda
    local del mercado y tambien su equivalente en EUR."""
    def clave_orden(symbol):
        """Orden numerico si el simbolo son solo digitos (HK/Corea), alfabetico si no (US/CRYPTO)."""
        return (0, int(symbol)) if symbol.isdigit() else (1, symbol)

    posiciones = sorted(
        [p for p in ib.positions()
         if p.position > 0 and contrato_pertenece_a_mercado(p.contract, mercado)],
        key=lambda p: clave_orden(p.contract.symbol)
    )

    # Nota de diagnostico: reqExecutions() es una llamada de red bloqueante
    # a IBKR. Se deja un log justo antes, para que si algun dia se queda
    # colgada (visto en produccion un episodio de ~38 minutos sin ninguna
    # linea de log, aqui es el sospechoso principal) quede claro en el log
    # DONDE se quedo parado el bot, en vez de un silencio sin pistas.
    log(f"RESUMEN {mercado}: pidiendo historial de ejecuciones a IBKR...")
    try:
        ejecuciones = ib.reqExecutions(ExecutionFilter())
    except Exception as e:
        log(f"RESUMEN {mercado}: no se pudieron obtener las ejecuciones ({type(e).__name__}: {e}), se omite el resumen.")
        return

    # Agrupamos las ejecuciones por simbolo UNA SOLA VEZ (en vez de recorrer
    # la lista completa por cada posicion/simbolo), para que esto no se
    # vuelva mas lento cada dia segun crece el historial de la cuenta.
    compras_por_simbolo = defaultdict(list)
    ventas_por_simbolo = defaultdict(list)
    for e in ejecuciones:
        if not contrato_pertenece_a_mercado(e.contract, mercado):
            continue
        if e.execution.side == 'BOT':
            compras_por_simbolo[e.contract.symbol].append(e)
        elif e.execution.side == 'SLD':
            ventas_por_simbolo[e.contract.symbol].append(e)

    hoy = datetime.now().date()

    log(f"\n========== RESUMEN DE CIERRE - MERCADO {mercado} ==========")

    # --- Posiciones abiertas ---
    log("--- Posiciones abiertas ---")
    log(f"{'Simbolo':<10}{'Abierta desde':<20}{'Cantidad':>10}{'Coste medio c/com.':>20}"
        f"{'Invertido (local)':>20}{'Invertido (EUR)':>18}{'Beneficio %':>13}")
    if not posiciones:
        log("(ninguna)")

    total_invertido_eur_acumulado = 0.0
    total_valor_actual_eur_acumulado = 0.0
    total_invertido_usd_acumulado = 0.0
    total_valor_actual_usd_acumulado = 0.0
    # Datos minimos de cada posicion/venta, recolectados aqui para poder
    # mandar el resumen de CRYPTO tambien por Telegram al final de la
    # funcion (ver mas abajo) sin repetir toda la logica de arriba.
    resumen_posiciones_telegram = []
    resumen_ventas_telegram = []

    for pos in posiciones:
        symbol = pos.contract.symbol
        # Preferimos la fecha de apertura registrada en nuestro propio
        # historial (persiste entre dias), ya que reqExecutions() de IBKR
        # solo cubre el dia actual y no sirve para posiciones abiertas en
        # dias anteriores.
        abierta_desde_registrada = obtener_apertura_registrada(mercado, symbol)
        if abierta_desde_registrada:
            abierta_desde_str = abierta_desde_registrada.strftime("%Y-%m-%d %H:%M")
        else:
            compras = compras_por_simbolo.get(symbol, [])
            abierta_desde = min((e.execution.time for e in compras), default=None)
            abierta_desde_str = abierta_desde.strftime("%Y-%m-%d %H:%M") if abierta_desde else "?"

        if mercado == "CRYPTO":
            comision_estimada = estimar_comision_cripto(pos.position * pos.avgCost)
        else:
            comision_estimada = estimar_comision(pos.position * pos.avgCost, pos.contract.currency, pos.position)
        coste_medio_con_comision = pos.avgCost + (comision_estimada / pos.position)
        total_invertido = pos.position * coste_medio_con_comision
        total_invertido_eur = valor_en_eur(total_invertido, pos.contract.currency)
        total_invertido_usd = valor_en_usd(total_invertido, pos.contract.currency)
        total_invertido_eur_acumulado += total_invertido_eur
        total_invertido_usd_acumulado += total_invertido_usd

        # Precio actual, para calcular el beneficio/perdida no realizado de
        # cada posicion abierta.
        velas_precio = pedir_velas(ib, pos.contract, '1 D', '1 min')
        if velas_precio:
            precio_actual = velas_precio[-1].close
            valor_actual = pos.position * precio_actual
            valor_actual_eur = valor_en_eur(valor_actual, pos.contract.currency)
            valor_actual_usd = valor_en_usd(valor_actual, pos.contract.currency)
            total_valor_actual_eur_acumulado += valor_actual_eur
            total_valor_actual_usd_acumulado += valor_actual_usd
            beneficio_pct_pos = (valor_actual - total_invertido) / total_invertido * 100
            beneficio_str = f"{beneficio_pct_pos:.2f}%"
        else:
            total_valor_actual_eur_acumulado += total_invertido_eur  # fallback: asumimos sin cambio
            total_valor_actual_usd_acumulado += total_invertido_usd
            beneficio_str = "N/D"
            beneficio_pct_pos = None

        resumen_posiciones_telegram.append((symbol, pos.position, beneficio_pct_pos, total_invertido_eur))
        log(f"{symbol:<10}{abierta_desde_str:<20}{pos.position:>10.4f}{coste_medio_con_comision:>20.4f}"
            f"{total_invertido:>20.2f}{total_invertido_eur:>18.2f}{beneficio_str:>13}")

    if posiciones:
        beneficio_pct_total = ((total_valor_actual_eur_acumulado - total_invertido_eur_acumulado)
                                / total_invertido_eur_acumulado * 100) if total_invertido_eur_acumulado else 0.0
        log("-" * 90)
        log(f"{'TOTAL':<10}{'':<20}{'':<10}{'':<20}{'':<20}{total_invertido_eur_acumulado:>18.2f}{beneficio_pct_total:>12.2f}%")
        log(f"(Total invertido: {total_invertido_usd_acumulado:.2f} USD / {total_invertido_eur_acumulado:.2f} EUR. "
            f"Beneficio/perdida no realizado global de todas las posiciones abiertas.)")

    # --- Operaciones cerradas hoy ---
    log("--- Operaciones cerradas hoy ---")
    log(f"{'Simbolo':<10}{'Abierta desde':<18}{'Cerrada a las':<18}{'Cantidad':>10}"
        f"{'Coste medio':>14}{'Invertido':>14}{'Beneficio %':>13}{'Ganancia':>12}")

    simbolos_con_venta_hoy = sorted(
        (symbol for symbol, ventas in ventas_por_simbolo.items()
         if any(e.execution.time.date() == hoy for e in ventas)),
        key=clave_orden
    )

    if not simbolos_con_venta_hoy:
        log("(ninguna)")

    total_ganancia_usd_acumulada = 0.0
    total_ganancia_eur_acumulada = 0.0

    for symbol in simbolos_con_venta_hoy:
        ventas_hoy = [e for e in ventas_por_simbolo.get(symbol, [])
                      if e.execution.time.date() == hoy]
        cerrada_a_las = max(e.execution.time for e in ventas_hoy)

        # Solo contamos compras ANTERIORES a la ultima venta del dia: si el
        # bot volvio a comprar este valor DESPUES de venderlo (una ronda
        # nueva, quiza aun abierta), esas compras no deben mezclarse con el
        # calculo de beneficio de la ronda ya cerrada.
        compras = [e for e in compras_por_simbolo.get(symbol, [])
                   if e.execution.time <= cerrada_a_las]

        abierta_desde = min((e.execution.time for e in compras), default=None)

        valor_base = sum(abs(e.execution.shares) * e.execution.avgPrice for e in ventas_hoy)
        cantidad_vendida = sum(abs(e.execution.shares) for e in ventas_hoy)
        total_comprado_acciones = sum(e.execution.shares for e in compras)
        total_comprado_valor = sum(e.execution.shares * e.execution.avgPrice for e in compras)

        # A diferencia de la tabla de posiciones abiertas, aqui preferimos la
        # fecha calculada a partir de reqExecutions() (ya filtrada a compras
        # ANTERIORES a esta venta) sobre la registrada en el historial: el
        # historial guarda la apertura de la posicion ACTUAL/mas reciente de
        # ese simbolo, que si el valor se volvio a comprar el mismo dia
        # DESPUES de cerrar esta ronda, es una fecha posterior a esta venta
        # (se vio en produccion: "Abierta desde" con hora posterior a
        # "Cerrada a las" para QCOM). Solo caemos al historial cuando no hay
        # compras de hoy que expliquen esta venta (posicion arrastrada de un
        # dia anterior).
        if abierta_desde:
            abierta_desde_str = abierta_desde.strftime("%Y-%m-%d %H:%M")
        else:
            abierta_desde_registrada = obtener_apertura_registrada(mercado, symbol)
            abierta_desde_str = abierta_desde_registrada.strftime("%Y-%m-%d %H:%M") if abierta_desde_registrada else "?"
        cerrada_a_las_str = cerrada_a_las.strftime("%Y-%m-%d %H:%M")

        if total_comprado_acciones <= 0:
            # No tenemos en esta sesion el historial de compra de este valor
            # (reqExecutions no siempre trae todo el pasado) -> no podemos
            # calcular el beneficio con fiabilidad, lo marcamos como N/D en
            # vez de mostrar un 0% enganoso.
            log(f"{symbol:<10}{abierta_desde_str:<18}{cerrada_a_las_str:<18}{cantidad_vendida:>10.4f}"
                f"{'N/D':>14}{'N/D':>14}{'N/D':>13}{'N/D':>12}")
            resumen_ventas_telegram.append((symbol, None, None))
            continue

        coste_medio_compra = total_comprado_valor / total_comprado_acciones
        total_invertido = cantidad_vendida * coste_medio_compra

        # Ganancia calculada directamente a partir de precios y cantidades
        # (no depende de commissionReport.realizedPNL, que puede venir vacio
        # tras una reconexion): venta - coste de compra - comision estimada
        # de AMBOS lados (compra y venta por separado, cada uno con su
        # propio minimo real de IBKR).
        ganancia_bruta = valor_base - total_invertido
        currency_op = ventas_hoy[0].contract.currency
        if mercado == "CRYPTO":
            comision_compra = estimar_comision_cripto(total_invertido)
            comision_venta = estimar_comision_cripto(valor_base)
        else:
            comision_compra = estimar_comision(total_invertido, currency_op, cantidad_vendida)
            comision_venta = estimar_comision(valor_base, currency_op, cantidad_vendida)
        comision_estimada = comision_compra + comision_venta
        ganancia = ganancia_bruta - comision_estimada
        beneficio_pct = (ganancia / total_invertido * 100) if total_invertido else 0.0
        total_ganancia_usd_acumulada += valor_en_usd(ganancia, ventas_hoy[0].contract.currency)
        ganancia_eur = valor_en_eur(ganancia, ventas_hoy[0].contract.currency)
        total_ganancia_eur_acumulada += ganancia_eur

        resumen_ventas_telegram.append((symbol, ganancia_eur, beneficio_pct))
        log(f"{symbol:<10}{abierta_desde_str:<18}{cerrada_a_las_str:<18}{cantidad_vendida:>10.4f}"
            f"{coste_medio_compra:>14.4f}{total_invertido:>14.2f}{beneficio_pct:>12.2f}%{ganancia:>12.2f}")

    if simbolos_con_venta_hoy:
        log("-" * 91)
        log(f"TOTAL ganancia hoy ({mercado}): {total_ganancia_usd_acumulada:.2f} USD / "
            f"{total_ganancia_eur_acumulada:.2f} EUR")

    log("=" * 60)
    log("(Beneficio % es aproximado: se calcula sobre el valor de la venta, "
        "no lote a lote, cuando ha habido varias compras/ventas parciales del mismo valor.)")

    # Resumen diario tambien por Telegram, SOLO para CRYPTO (a peticion del
    # usuario): US/HK/KR ya se pueden consultar en cualquier momento desde
    # el movil con /cartera, /hoy, etc. de telegram_bot_ibkr.py, pero cripto
    # no tenia ningun aviso automatico de fin de dia -a diferencia de
    # US/HK/KR, cripto no tiene un cierre real, asi que este resumen se
    # dispara a una hora fija (ver justo_hora_resumen_cripto()), no al
    # cierre del mercado.
    if mercado == "CRYPTO":
        lineas = [f"🪙 <b>RESUMEN DIARIO CRYPTO</b> ({hoy})"]

        lineas.append("\n<b>Posiciones abiertas:</b>")
        if not resumen_posiciones_telegram:
            lineas.append("(ninguna)")
        else:
            for symbol, cantidad, beneficio_pct_pos, invertido_eur in resumen_posiciones_telegram:
                emoji = "⚪" if beneficio_pct_pos is None else ("🟢" if beneficio_pct_pos >= 0 else "🔴")
                beneficio_txt = "N/D" if beneficio_pct_pos is None else f"{formato_es(beneficio_pct_pos, signo=True)}%"
                lineas.append(f"{emoji} {symbol}: {formato_es(cantidad, 6)} "
                              f"(invertido {formato_es(invertido_eur)} EUR, {beneficio_txt})")
            if total_invertido_eur_acumulado:
                beneficio_pct_total = ((total_valor_actual_eur_acumulado - total_invertido_eur_acumulado)
                                        / total_invertido_eur_acumulado * 100)
                lineas.append(f"<b>Total invertido</b>: {formato_es(total_invertido_eur_acumulado)} EUR "
                              f"({formato_es(beneficio_pct_total, signo=True)}%)")

        lineas.append("\n<b>Ventas de hoy:</b>")
        if not resumen_ventas_telegram:
            lineas.append("(ninguna)")
        else:
            for symbol, ganancia_eur, beneficio_pct in resumen_ventas_telegram:
                if ganancia_eur is None:
                    lineas.append(f"⚪ {symbol}: N/D")
                else:
                    emoji = "🟢" if ganancia_eur >= 0 else "🔴"
                    lineas.append(f"{emoji} {symbol}: {formato_es(ganancia_eur, signo=True)} EUR "
                                  f"({formato_es(beneficio_pct, signo=True)}%)")
            lineas.append(f"<b>Ganancia total hoy</b>: {formato_es(total_ganancia_eur_acumulada, signo=True)} EUR")

        notificar_telegram("\n".join(lineas))


def obtener_modo_cuenta(ib):
    """Determina si la cuenta conectada es DEMO/paper o REAL, a partir del
    ID de cuenta que devuelve IBKR. Convencion de IBKR: las cuentas de
    paper trading siempre tienen un ID que empieza por 'DU'; las cuentas
    reales no. Devuelve (es_demo, texto) donde es_demo es True/False/None
    (None si no se pudo determinar con seguridad)."""
    try:
        cuentas = ib.managedAccounts()
    except Exception:
        cuentas = []
    cuentas = [c for c in cuentas if c]  # descarta cadenas vacias
    if not cuentas:
        return None, "DESCONOCIDO (no se pudo obtener el ID de cuenta)"
    if all(c.startswith('DU') for c in cuentas):
        return True, f"DEMO / PAPER TRADING (cuenta {', '.join(cuentas)})"
    if any(c.startswith('DU') for c in cuentas):
        return None, f"MIXTO: hay cuentas demo y reales a la vez ({', '.join(cuentas)}), revisa manualmente"
    return False, f"REAL - DINERO REAL (cuenta {', '.join(cuentas)})"


def avisar_modo_cuenta(ib):
    """Imprime un aviso bien visible del modo de cuenta (demo/real). Se
    llama al conectar y tras cada reconexion, por si el usuario cambiara de
    cuenta o de puerto entre una sesion y otra."""
    es_demo, modo_texto = obtener_modo_cuenta(ib)
    log("#" * 60)
    log(f"MODO DE CUENTA: {modo_texto}")
    if es_demo is False:
        log("ATENCION: esta es una cuenta REAL. Las ordenes de este bot son DINERO REAL, no una simulacion.")
    log("#" * 60)
    return modo_texto


def ciclo_completo(ib, modo_texto="?", mercados=None):
    """Ver revisar_ventas()/revisar_compras() para el significado de
    `mercados`. Cripto corre en su propia cadencia (CRYPTO_INTERVALO_SEGUNDOS,
    mas rapida) independiente de US/HK/KR (INTERVALO_SEGUNDOS) -ver main()-,
    asi que este ciclo se llama dos veces por vuelta del bucle principal,
    cada una con un subconjunto de mercados distinto cuando corresponda."""
    etiqueta = f" ({'/'.join(sorted(mercados))})" if mercados is not None else ""
    log("=" * 60)
    log(f"Iniciando nuevo ciclo de revision{etiqueta}. [{modo_texto}]")
    revisar_ventas(ib, mercados=mercados)
    revisar_compras(ib, mercados=mercados)
    log("Ciclo completado.")


def evitar_suspension_windows():
    """Pide a Windows que no suspenda ni hiberne el SISTEMA mientras el bot
    esta activo (para que el bot siga corriendo sin vigilancia). NO fuerza
    la PANTALLA a quedarse encendida (sin ES_DISPLAY_REQUIRED) -peticion
    del usuario, sept. 2026: el bot no debe apagar la pantalla, esa
    decision es solo de la configuracion de energia de Windows, el bot
    unicamente evita que el equipo entero se suspenda/hiberne (lo que si
    pararia el bot). No evita un cierre de tapa forzado. No tiene efecto en
    otros sistemas operativos."""
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
        pass  # no es Windows, o no se pudo aplicar; no es critico para el funcionamiento


def conexion_esta_viva(ib):
    """Comprueba activamente si la conexion funciona de verdad, en vez de
    fiarse solo de ib.isConnected() (que puede no detectar ciertos cortes
    bruscos, como un WinError 10054, y seguir reportando 'conectado' aunque
    ya no lo este)."""
    if not ib.isConnected():
        return False
    try:
        ib.reqCurrentTime()
        return True
    except Exception:
        return False


def esperar_pumpeando(ib, segundos_totales, intervalo_chequeo=30):
    """Espera el numero de segundos indicado, pero en tramos cortos y usando
    ib.sleep() en lugar de time.sleep(). time.sleep() congela todo el
    programa, incluida la parte de ib_async que escucha la conexion, asi que
    un corte de red (p.ej. el WinError 10054) no se detecta hasta que acaba
    la espera completa; con esperas largas (varias horas fuera de horario de
    mercado) eso deja al bot funcionando "a ciegas" con una conexion ya
    muerta. Usando ib.sleep() en tramos cortos, la conexion se puede
    comprobar periodicamente y la espera se corta antes si se pierde, para
    poder reconectar cuanto antes."""
    restante = segundos_totales
    while restante > 0:
        tramo = min(intervalo_chequeo, restante)
        ib.sleep(tramo)
        actualizar_latido()  # espera larga legitima: no es una congelacion, se avisa al vigilante
        restante -= tramo
        if not ib.isConnected():
            log("Conexion perdida durante la espera, se corta la espera para reconectar antes.")
            return
        if peticion_de_parada_pendiente():
            # Corta la espera cuanto antes: el bucle principal de main()
            # detecta el archivo de parada nada mas volver aqui.
            return


# Codigos informativos de conexion de IBKR que no indican ningun problema
# (se emiten en cada conexion/reconexion, "farm connection is OK", etc.) -
# se ignoran para no ensuciar el log con ruido en cada ciclo.
CODIGOS_ERROR_IB_INFORMATIVOS = {1100, 1101, 1102, 2103, 2104, 2105, 2106, 2107, 2108, 2119, 2137, 2158}


def on_error_ib(reqId, errorCode, errorString, contract=None):
    """Registra en el log cualquier error/aviso que IBKR devuelva por la API
    (errorEvent de ib_async), salvo los puramente informativos de conexion.
    Sin esto, errores reales de IBKR (permisos, suscripcion de datos no
    activa, contrato no encontrado...) eran invisibles para el bot: solo se
    veia un TimeoutError generico tras agotar el timeout, sin saber la causa
    real (bug real de produccion, sept. 2026, diagnostico del timeout de
    BTC)."""
    if errorCode in CODIGOS_ERROR_IB_INFORMATIVOS:
        return
    contrato_str = f" [{contract.symbol}]" if contract is not None and getattr(contract, "symbol", None) else ""
    log(f"IBKR API - error {errorCode}{contrato_str}: {errorString}")


def main():
    evitar_suspension_windows()
    escribir_pid()
    actualizar_latido()
    cargar_estado_venta()
    threading.Thread(target=vigilante_congelacion, daemon=True).start()

    # Si queda un archivo de parada de una sesion anterior (p.ej. el usuario
    # detuvo el bot y luego lo volvio a arrancar sin pasar por /arrancar de
    # telegram_bot_ibkr.py, que ya lo borra), se ignora al arrancar: un
    # arranque nuevo nunca debe autopararse de inmediato.
    if peticion_de_parada_pendiente():
        log(f"Aviso: existia una peticion de parada pendiente ({ARCHIVO_DETENER}) de una sesion "
            f"anterior, se ignora al arrancar de nuevo.")
        try:
            os.remove(ARCHIVO_DETENER)
        except OSError:
            pass

    ib = IB()
    ib.connect('127.0.0.1', 4002, clientId=1)
    ib.RequestTimeout = 30  # segundos: evita que cualquier peticion se quede colgada sin limite
    ib.errorEvent += on_error_ib
    modo_texto = avisar_modo_cuenta(ib)

    resumenes_enviados_hoy = set()  # claves (mercado, fecha) para no repetir el resumen
    # Proximo instante (time.monotonic()) en el que toca revisar cada grupo
    # de mercados; 0.0 fuerza a que ambos corran en la primera vuelta.
    proxima_revision_cripto = 0.0
    proxima_revision_otros = 0.0

    try:
        while True:
            if peticion_de_parada_pendiente():
                log(f"Parada solicitada (archivo {ARCHIVO_DETENER} detectado): cerrando limpiamente. "
                    f"No se borra aqui el archivo -run.bot.bat lo borra al ver que fue una parada "
                    f"limpia, para no reiniciar el bucle-.")
                return

            if not conexion_esta_viva(ib):
                global _aviso_reconexion_fallida_emitido
                log("Conexion con IB Gateway perdida. Intentando reconectar...")
                reconectado = False
                for intento in range(1, REINTENTOS_RECONEXION + 1):
                    try:
                        ib.disconnect()
                    except Exception:
                        pass
                    try:
                        ib.connect('127.0.0.1', 4002, clientId=1)
                        ib.RequestTimeout = 30
                        # No hace falta volver a registrar on_error_ib: es el mismo
                        # objeto `ib`, y el listener de errorEvent sobrevive a
                        # disconnect()/connect() (solo se re-crearia con IB() nuevo).
                        log(f"Reconexion con IB Gateway completada (intento {intento}/{REINTENTOS_RECONEXION}).")
                        modo_texto = avisar_modo_cuenta(ib)
                        reconectado = True
                        if _aviso_reconexion_fallida_emitido:
                            notificar_telegram("✅ Reconexion con IB Gateway recuperada, el bot sigue operando con normalidad.")
                            _aviso_reconexion_fallida_emitido = False
                        break
                    except Exception as e:
                        log(f"Fallo el intento {intento}/{REINTENTOS_RECONEXION} de reconexion: {type(e).__name__}: {e}")
                        if intento < REINTENTOS_RECONEXION:
                            ib.sleep(ESPERA_ENTRE_REINTENTOS_RECONEXION_SEGUNDOS)

                if not reconectado:
                    log(f"No se pudo reconectar con IB Gateway tras {REINTENTOS_RECONEXION} intentos. "
                        f"Se reintentara en la siguiente vuelta.")
                    if not _aviso_reconexion_fallida_emitido:
                        notificar_telegram(f"⚠️ No se pudo reconectar con IB Gateway tras {REINTENTOS_RECONEXION} "
                                           f"intentos. Revisa que TWS/IB Gateway siga abierto y conectado.")
                        _aviso_reconexion_fallida_emitido = True
                    log(f"Esperando {INTERVALO_SEGUNDOS // 60} minutos hasta la siguiente revision...")
                    esperar_pumpeando(ib, INTERVALO_SEGUNDOS)
                    continue

            actualizar_tipo_cambio_eur_usd(ib)  # throttlado internamente, seguro llamar cada vuelta

            hoy = datetime.now().date()
            for mercado in ("US", "HK", "KR"):
                if justo_cerro_mercado(mercado) and (mercado, hoy) not in resumenes_enviados_hoy:
                    try:
                        generar_resumen_cierre_mercado(ib, mercado)
                    except Exception as e:
                        log(f"RESUMEN {mercado}: error al generar el resumen: {type(e).__name__}: {e}")
                    resumenes_enviados_hoy.add((mercado, hoy))

            if justo_hora_resumen_cripto() and ("CRYPTO", hoy) not in resumenes_enviados_hoy:
                try:
                    generar_resumen_cierre_mercado(ib, "CRYPTO")
                except Exception as e:
                    log(f"RESUMEN CRYPTO: error al generar el resumen: {type(e).__name__}: {e}")
                resumenes_enviados_hoy.add(("CRYPTO", hoy))

            cripto_abierta = es_horario_operativo("CRYPTO")
            otros_abiertos = (es_horario_operativo("US") or es_horario_operativo("HK")
                               or es_horario_operativo("KR") or en_alguna_ventana_pre_apertura())
            hay_mercado_abierto = cripto_abierta or otros_abiertos

            if not hay_mercado_abierto:
                segundos_espera = segundos_hasta_pre_apertura()
                segundos_espera = max(segundos_espera, 60)  # suelo minimo: nunca esperar casi 0
                minutos_espera = segundos_espera / 60
                log(f"Fuera de horario operativo en todos los mercados (US, HK, KR, CRYPTO). "
                    f"Esperando {minutos_espera:.0f} minutos hasta {MINUTOS_ANTES_DE_APERTURA_PARA_DESPERTAR} "
                    f"min antes de la proxima apertura...")
                esperar_pumpeando(ib, segundos_espera)
                continue

            # Cripto (24/7, CRYPTO_INTERVALO_SEGUNDOS = 1 min) y US/HK/KR
            # (INTERVALO_SEGUNDOS = 4 min) corren en su propia cadencia,
            # cada una independiente de la otra -cripto es mas rapida y no
            # tiene sentido frenarla al ritmo de acciones, ni tiene sentido
            # acelerar acciones al ritmo de cripto (mas peticiones de datos
            # de las que hacen falta). Cada una se ejecuta solo si ya toca
            # (ahora >= proxima_revision_*) Y su mercado esta abierto ahora
            # mismo.
            ahora_mono = time.monotonic()

            if cripto_abierta and ahora_mono >= proxima_revision_cripto:
                inicio_cripto = time.monotonic()
                try:
                    ciclo_completo(ib, modo_texto, mercados={"CRYPTO"})
                except Exception as e:
                    log(f"ERROR en el ciclo CRYPTO: {type(e).__name__}: {e}")
                duracion_cripto = time.monotonic() - inicio_cripto
                if duracion_cripto > CRYPTO_INTERVALO_SEGUNDOS:
                    log(f"AVISO: el ciclo CRYPTO ha tardado {duracion_cripto:.0f}s, mas que su "
                        f"intervalo configurado ({CRYPTO_INTERVALO_SEGUNDOS}s).")
                proxima_revision_cripto = inicio_cripto + CRYPTO_INTERVALO_SEGUNDOS

            if otros_abiertos and ahora_mono >= proxima_revision_otros:
                inicio_otros = time.monotonic()
                try:
                    ciclo_completo(ib, modo_texto, mercados={"US", "HK", "KR"})
                except Exception as e:
                    log(f"ERROR en el ciclo US/HK/KR: {type(e).__name__}: {e}")
                duracion_otros = time.monotonic() - inicio_otros
                if duracion_otros > INTERVALO_SEGUNDOS:
                    log(f"AVISO: el ciclo US/HK/KR ha tardado {duracion_otros:.0f}s, mas que el "
                        f"intervalo configurado ({INTERVALO_SEGUNDOS}s). Considera subir "
                        f"INTERVALO_SEGUNDOS o reducir el numero de valores/temporalidades.")
                proxima_revision_otros = inicio_otros + INTERVALO_SEGUNDOS

            # Espera solo hasta que toque la PROXIMA revision (la que antes,
            # de las dos), y solo se tiene en cuenta la de un grupo si su
            # mercado esta abierto ahora -si no, ese grupo no cuenta para
            # decidir cuanto esperar (evita esperar activamente a una
            # ventana de cripto cerrada, por ejemplo).
            candidatos_espera = []
            if cripto_abierta:
                candidatos_espera.append(proxima_revision_cripto)
            if otros_abiertos:
                candidatos_espera.append(proxima_revision_otros)
            proxima_revision = min(candidatos_espera) if candidatos_espera else time.monotonic() + INTERVALO_SEGUNDOS
            segundos_espera_siguiente = max(proxima_revision - time.monotonic(), 1)
            log(f"Esperando {segundos_espera_siguiente:.0f}s hasta la siguiente revision "
                f"(cripto cada {CRYPTO_INTERVALO_SEGUNDOS}s, US/HK/KR cada {INTERVALO_SEGUNDOS}s)...")
            esperar_pumpeando(ib, segundos_espera_siguiente)
    except KeyboardInterrupt:
        log("Detenido manualmente por el usuario (Ctrl+C).")
    finally:
        ib.disconnect()
        log("Desconectado de IB Gateway.")


if __name__ == "__main__":
    SEGUNDOS_ESPERA_TRAS_FALLO = 15

    while True:
        try:
            main()
            break  # main() termino de forma normal (Ctrl+C gestionado dentro) -> salir del todo
        except KeyboardInterrupt:
            break  # parada manual justo al arrancar/conectar, antes de entrar en el bucle interno
        except Exception as e:
            log(f"ERROR FATAL fuera del ciclo principal: {type(e).__name__}: {e}. "
                f"Reiniciando el bot en {SEGUNDOS_ESPERA_TRAS_FALLO} segundos...")
            notificar_telegram(f"⚠️ <b>ERROR FATAL</b> en el bot de IBKR: {type(e).__name__}: {e}. "
                               f"Reiniciando en {SEGUNDOS_ESPERA_TRAS_FALLO}s.")
            time.sleep(SEGUNDOS_ESPERA_TRAS_FALLO)
