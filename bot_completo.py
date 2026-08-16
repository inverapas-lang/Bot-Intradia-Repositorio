"""
BOT COMPLETO - bucle automatico cada 4 minutos. Soporta estos mercados:
  - US (NYSE/Nasdaq, en USD): premercado 4:00-9:30 ET + mercado regular 9:30-16:00 ET.
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
import os
import threading
import time
from datetime import datetime, time as dt_time, timedelta
from collections import defaultdict
from zoneinfo import ZoneInfo

import pandas as pd
from ib_async import IB, Stock, MarketOrder, LimitOrder, ExecutionFilter

# --- Horarios por mercado ---
ZONA_NY = ZoneInfo("America/New_York")
HORA_INICIO_US = dt_time(4, 0)     # 4:00 ET (premercado)
HORA_CIERRE_US = dt_time(16, 0)    # 16:00 ET (cierre regular)

ZONA_EU = ZoneInfo("Europe/Paris")
HORA_INICIO_EU = dt_time(9, 0)     # 9:00 hora de Paris/Amsterdam/Bruselas/Milan
HORA_CIERRE_EU = dt_time(17, 30)   # 17:30 hora de Paris/Amsterdam/Bruselas/Milan

ZONA_HK = ZoneInfo("Asia/Hong_Kong")
HORA_INICIO_HK = dt_time(9, 30)    # 9:30 hora de Hong Kong (simplificado, ignora pausa de mediodia)
HORA_CIERRE_HK = dt_time(16, 0)    # 16:00 hora de Hong Kong

ZONA_KR = ZoneInfo("Asia/Seoul")
HORA_INICIO_KR = dt_time(9, 0)     # 9:00 hora de Corea
HORA_CIERRE_KR = dt_time(15, 30)   # 15:30 hora de Corea

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

# --- Configuracion general ---
IMPORTE_EUROS = 1000
IMPORTE_EUROS_HK = 3500  # HK opera en lotes fijos (a veces 500+ acciones): presupuesto mayor para poder cubrirlos
TIPO_CAMBIO_EUR_USD = 1.14   # Actualiza estos valores cuando quieras, a mano
TIPO_CAMBIO_USD_HKD = 7.80   # 1 USD = 7.80 HKD (aprox, el HKD esta fijado al USD)
TIPO_CAMBIO_USD_KRW = 1480   # 1 USD = 1480 KRW (aprox)
UMBRAL_BENEFICIO_PCT = 0.5  # % minimo de beneficio para activar la vigilancia de venta
MARGEN_ORDEN_LIMITADA_VENTA_PCT = 0.2  # % por debajo del precio actual al vender con orden limitada
INTERVALO_SEGUNDOS = 4 * 60  # 4 minutos
LIMITE_EXPOSICION_PCT = 15   # % maximo del total de cartera (en USD equivalente) por valor

# --- Fracciones de accion ---
# IBKR solo admite comprar fracciones de accion en el mercado US (y solo para
# una parte de los valores, los que tengan ese permiso habilitado). En HK y
# KR las acciones se compran siempre en unidades/lotes enteros.
FRACCIONABLE_POR_MERCADO = {"US": True, "EU": False, "HK": False, "KR": False}
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

ACTIVOS = ACTIVOS_US + ACTIVOS_HK + ACTIVOS_KR  # EU excluido: pendiente de suscripcion de datos de mercado

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
            os._exit(1)


def log(mensaje):
    actualizar_latido()
    ahora = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{ahora}] {mensaje}")


def es_horario_operativo(mercado):
    """True si el mercado indicado ('US', 'EU', 'HK' o 'KR') esta en horario
    operativo ahora mismo, de lunes a viernes."""
    if mercado == "US":
        ahora = datetime.now(ZONA_NY)
        if ahora.weekday() >= 5:
            return False
        return HORA_INICIO_US <= ahora.time() < HORA_CIERRE_US
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
    return False


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
    mercado, saltando fines de semana."""
    zona, hora_apertura = APERTURA_POR_MERCADO[mercado]
    ahora = datetime.now(zona)
    candidato = ahora.replace(hour=hora_apertura.hour, minute=hora_apertura.minute,
                              second=0, microsecond=0)
    if candidato <= ahora:
        candidato += timedelta(days=1)
    while candidato.weekday() >= 5:  # 5=sabado, 6=domingo
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
        _aviso_datos_caidos_emitido = True

    intentos = 1 if disyuntor_activo else INTENTOS_MAXIMOS

    for intento in range(1, intentos + 1):
        try:
            velas = ib.reqHistoricalData(
                contrato, endDateTime='', durationStr=duration,
                barSizeSetting=barSize, whatToShow='TRADES',
                useRTH=False, formatDate=1,
            )
        except Exception as e:
            log(f"{contrato.symbol} - error al pedir datos en el intento {intento}/{intentos}: "
                f"{type(e).__name__}: {e}")
            velas = []

        if velas:
            _fallos_seguidos_datos = 0
            _aviso_datos_caidos_emitido = False
            return velas

        if intento < intentos:
            log(f"{contrato.symbol} - sin datos en el intento {intento}/{intentos}, "
                f"reintentando en {ESPERA_ENTRE_INTENTOS_SEGUNDOS}s...")
            ib.sleep(ESPERA_ENTRE_INTENTOS_SEGUNDOS)

    _fallos_seguidos_datos += 1
    return []


def crear_contrato(activo):
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


def crear_orden_limitada(accion, cantidad, precio_limite):
    return _sin_flags_legacy(LimitOrder(accion, cantidad, precio_limite))


def crear_orden_limitada_cash(accion, importe_efectivo, precio_limite):
    """Version 'cash quantity' de crear_orden_limitada, para cantidades
    fraccionarias (vease crear_orden_mercado_cash)."""
    orden = LimitOrder(accion, 0, precio_limite)
    orden.cashQty = round(importe_efectivo, 2)
    return _sin_flags_legacy(orden)


def orden_rechazada_por_codigo(trade, codigos_error):
    """True si el registro de la orden (trade.log) contiene alguno de los
    codigos de error de IBKR indicados. Se usa para detectar rechazos
    concretos (p.ej. 10244: "cash quantity no admitida en esta orden" -no
    todos los valores tienen habilitadas las fracciones via API en IBKR,
    aunque el mercado en general si las soporte-) y reaccionar de forma
    distinta a un fallo generico."""
    return any(getattr(entry, 'errorCode', None) in codigos_error for entry in getattr(trade, 'log', []))


def analizar_activo(ib, activo):
    contrato = crear_contrato(activo)
    ib.qualifyContracts(contrato)

    if not contrato.conId:
        # El contrato ni siquiera se ha podido resolver (simbolo/exchange
        # incorrecto, o falta de permisos en ese mercado). No tiene sentido
        # gastar reintentos en pedir datos historicos de un contrato invalido.
        return contrato, "SIMBOLO_NO_RESUELTO"

    detalle = {}
    for tf in TEMPORALIDADES:
        velas = pedir_velas(ib, contrato, tf['duration'], tf['barSize'])
        if len(velas) < 35:
            detalle[tf['nombre']] = None
            continue

        cierres = pd.Series([v.close for v in velas])
        macd, linea_senal, histograma = calcular_macd(cierres)

        if tf['tipo'] == 'corta':
            detalle[tf['nombre']] = bool(macd.iloc[-1] > linea_senal.iloc[-1])
        else:
            ultimas_3 = histograma.iloc[-3:]
            detalle[tf['nombre']] = bool(ultimas_3.iloc[2] > ultimas_3.iloc[0])

    # Atajo: si las 4 temporalidades mas cortas (1min, 5min, 15min, 30min)
    # estan todas alcistas, se compra directamente, sin mirar 1h/dia/semana
    # ni la regla de "maximo 1 de 7 en contra" de mas abajo.
    cuatro_cortas_alcistas = (
        all(detalle[n] is not None for n in NOMBRES_4_CORTAS)
        and all(detalle[n] for n in NOMBRES_4_CORTAS)
    )

    faltan_datos = any(detalle[tf['nombre']] is None for tf in TEMPORALIDADES)
    total_false = sum(1 for tf in TEMPORALIDADES if detalle[tf['nombre']] is False)
    cortas_ok = all(detalle[tf['nombre']] for tf in TEMPORALIDADES
                     if tf['tipo'] == 'corta' and detalle[tf['nombre']] is not None)
    largas_ok = all(detalle[tf['nombre']] for tf in TEMPORALIDADES
                     if tf['tipo'] == 'larga' and detalle[tf['nombre']] is not None)

    if cuatro_cortas_alcistas:
        decision = "COMPRA"
    elif faltan_datos:
        decision = "SIN_DATOS"
    elif total_false == 0:
        decision = "COMPRA"
    elif total_false == 1:
        decision = "COMPRA"
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


def revisar_ventas(ib):
    ib.reqPositions()
    ib.sleep(1)  # da tiempo a que la respuesta llegue antes de leer ib.positions()
    posiciones = ib.positions()

    if not posiciones:
        log("VENTAS: no hay posiciones abiertas.")
        return

    posiciones_con_mercado = [
        (CURRENCY_A_MERCADO.get(pos.contract.currency, "?"), pos)
        for pos in posiciones if pos.position > 0
    ]
    posiciones_con_mercado.sort(key=lambda x: x[0])

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
        # pero sin intentar operar.
        if mercado in CIERRE_POR_MERCADO and not es_horario_operativo(mercado):
            if mercado not in mercados_cerrados_avisados:
                log(f"VENTAS: mercado {mercado} fuera de horario operativo, no se intenta vender "
                    f"ninguna posicion de este mercado en este ciclo.")
                mercados_cerrados_avisados.add(mercado)
            continue

        contrato = pos.contract
        cantidad = pos.position
        coste_medio = pos.avgCost

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
            comision_compra = estimar_comision(valor_compra, contrato.currency, cantidad)
            comision_venta = estimar_comision(valor_venta, contrato.currency, cantidad)
            comision_total = comision_compra + comision_venta
            comision_total_pct = comision_total / valor_compra * 100
            beneficio_pct = beneficio_pct_bruto - comision_total_pct

            # Prefijo comun con los datos base de la posicion, reutilizado en
            # todas las lineas de log de esta operacion.
            info_posicion = (f"{cantidad:g} acciones, precio medio {coste_medio:.4f} {contrato.currency}, "
                              f"comision estimada {comision_total:.2f} {contrato.currency}")

            if (mercado != "?" and en_ventana_venta_forzada(mercado)
                    and UMBRAL_BENEFICIO_PCT <= beneficio_pct <= BENEFICIO_MAX_VENTA_FORZADA_PCT):
                log(f"VENTAS: {contrato.symbol} - {info_posicion} - beneficio neto {beneficio_pct:.2f}% "
                    f"(bruto {beneficio_pct_bruto:.2f}%), dentro de los ultimos "
                    f"{MINUTOS_VENTA_FORZADA_ANTES_CIERRE} min antes del cierre de {mercado} -> VENTA FORZADA (orden limitada).")
                precio_limite = calcular_precio_limite_venta(precio_actual, contrato.currency)
                if es_cantidad_fraccionaria(cantidad):
                    # Igual que en las compras: algunas cuentas/valores
                    # rechazan el importe en efectivo (cashQty) con el error
                    # 10244, asi que se manda por importe en efectivo primero
                    # y, si lo rechaza, se reintenta con la cantidad
                    # fraccionaria puesta DIRECTAMENTE (confirmado a mano en
                    # cuenta real que esto SI funciona, vease Plan B de
                    # revisar_compras para el detalle completo).
                    orden = crear_orden_limitada_cash('SELL', cantidad * precio_actual, precio_limite)
                else:
                    orden = crear_orden_limitada('SELL', cantidad, precio_limite)
                trade = ib.placeOrder(contrato, orden)
                estado = esperar_estado_final_orden(ib, trade)
                log(f"VENTAS: {contrato.symbol} - orden limitada a {precio_limite} {contrato.currency}, "
                    f"estado: {estado}")
                if (es_cantidad_fraccionaria(cantidad) and estado != 'Filled'
                        and orden_rechazada_por_codigo(trade, {10244})):
                    log(f"VENTAS: {contrato.symbol} - no admite el importe en efectivo (cashQty) via API "
                        f"(error 10244); reintentando con la cantidad fraccionaria puesta directamente.")
                    orden = crear_orden_limitada('SELL', cantidad, precio_limite)
                    trade = ib.placeOrder(contrato, orden)
                    estado = esperar_estado_final_orden(ib, trade)
                    log(f"VENTAS: {contrato.symbol} - orden limitada (cantidad fraccionaria directa), "
                        f"estado: {estado}")
                if estado != 'Filled':
                    verificar_posicion_tras_orden_no_confirmada(ib, contrato, cantidad, f"VENTAS: {contrato.symbol}")
                continue

            if beneficio_pct < UMBRAL_BENEFICIO_PCT:
                log(f"VENTAS: {contrato.symbol} - {info_posicion} - beneficio neto {beneficio_pct:.2f}% "
                    f"(bruto {beneficio_pct_bruto:.2f}%), por debajo del umbral -> se mantiene.")
                continue

            bajista = macd_5min_bajista(ib, contrato)
            if bajista is None:
                log(f"VENTAS: {contrato.symbol} - {info_posicion} - datos insuficientes para MACD 5min, se mantiene por precaucion.")
                continue

            if bajista:
                log(f"VENTAS: {contrato.symbol} - {info_posicion} - beneficio neto {beneficio_pct:.2f}% "
                    f"(bruto {beneficio_pct_bruto:.2f}%), MACD 5min BAJISTA -> VENDIENDO (orden a mercado).")
                if es_cantidad_fraccionaria(cantidad):
                    orden = crear_orden_mercado_cash('SELL', cantidad * precio_actual)
                else:
                    orden = crear_orden_mercado('SELL', cantidad)
                trade = ib.placeOrder(contrato, orden)
                estado = esperar_estado_final_orden(ib, trade)
                log(f"VENTAS: {contrato.symbol} - orden a mercado, "
                    f"estado: {estado}")
                if (es_cantidad_fraccionaria(cantidad) and estado != 'Filled'
                        and orden_rechazada_por_codigo(trade, {10244})):
                    # Ver Plan B en revisar_compras: la cuenta puede rechazar
                    # cashQty (10244) pero SI admitir la cantidad fraccionaria
                    # puesta directamente (confirmado a mano en cuenta real).
                    log(f"VENTAS: {contrato.symbol} - no admite el importe en efectivo (cashQty) via API "
                        f"(error 10244); reintentando con la cantidad fraccionaria puesta directamente.")
                    orden = crear_orden_mercado('SELL', cantidad)
                    trade = ib.placeOrder(contrato, orden)
                    estado = esperar_estado_final_orden(ib, trade)
                    log(f"VENTAS: {contrato.symbol} - orden a mercado (cantidad fraccionaria directa), "
                        f"estado: {estado}")
                if estado != 'Filled':
                    verificar_posicion_tras_orden_no_confirmada(ib, contrato, cantidad, f"VENTAS: {contrato.symbol}")
            else:
                log(f"VENTAS: {contrato.symbol} - {info_posicion} - beneficio neto {beneficio_pct:.2f}% "
                    f"(bruto {beneficio_pct_bruto:.2f}%), MACD 5min ALCISTA -> se deja correr.")
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


def obtener_valor_posicion_actual_usd(posiciones, ticker, precio_actual, currency):
    """Devuelve el valor en USD de la posicion actual de un ticker (0 si no hay)."""
    for pos in posiciones:
        if pos.contract.symbol == ticker and pos.position > 0:
            return valor_en_usd(pos.position * precio_actual, currency)
    return 0.0


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


def revisar_compras(ib):
    valor_total_cartera_usd = obtener_valor_total_cartera_usd(ib)
    if valor_total_cartera_usd is None:
        log("COMPRAS: no se pudo obtener el valor total de la cartera, se omite este ciclo de compras.")
        return

    limite_por_valor_usd = valor_total_cartera_usd * (LIMITE_EXPOSICION_PCT / 100)

    ib.reqPositions()
    ib.sleep(1)  # da tiempo a que la respuesta llegue antes de leer ib.positions()
    posiciones_actuales = ib.positions()

    mercados_ya_avisados = set()
    mercado_actual = None
    contadores = {"analizados": 0, "senales": 0, "errores": 0}

    def imprimir_resumen_mercado():
        if mercado_actual is not None:
            log(f"{mercado_actual}: {contadores['analizados']} analizados, "
                f"{contadores['senales']} señales de compra, {contadores['errores']} errores.")

    for activo in ACTIVOS:
        if activo["mercado"] != mercado_actual:
            imprimir_resumen_mercado()
            log(f"\n########## COMPRAS - MERCADO {activo['mercado']} ##########")
            mercado_actual = activo["mercado"]
            contadores = {"analizados": 0, "senales": 0, "errores": 0}

        if not es_horario_operativo(activo["mercado"]):
            continue  # este valor esta fuera de horario en su mercado, se omite en este ciclo

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
            fraccionable = FRACCIONABLE_POR_MERCADO.get(activo["mercado"], False)

            if fraccionable:
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
                f"limite: {limite_por_valor_usd:.2f} USD).")
            cantidad_antes_compra = next(
                (p.position for p in posiciones_actuales if p.contract.symbol == ticker and p.position > 0), 0.0)

            if fraccionable:
                orden = crear_orden_mercado_cash('BUY', importe_a_usar)
            else:
                orden = crear_orden_mercado('BUY', cantidad)
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
            # la cantidad fraccionaria original puesta directamente.
            if fraccionable and estado != 'Filled' and orden_rechazada_por_codigo(trade, {10244}):
                log(f"COMPRAS: {ticker} - este valor no admite el importe en efectivo (cashQty) via API "
                    f"(error 10244); reintentando con la cantidad fraccionaria puesta directamente "
                    f"({cantidad:g} acciones).")
                orden = crear_orden_mercado('BUY', cantidad)
                trade = ib.placeOrder(contrato, orden)
                estado = esperar_estado_final_orden(ib, trade)
                log(f"COMPRAS: {ticker} - estado de la orden (cantidad fraccionaria directa): {estado}")

            # Plan C: si tampoco admite la cantidad fraccionaria directa
            # (error 10243, "no se puede introducir la orden de tamano
            # fraccionario a traves de la API"), se reintenta con acciones
            # ENTERAS en vez de rendirse del todo, siempre que el presupuesto
            # llegue para al menos 1.
            if fraccionable and estado != 'Filled' and orden_rechazada_por_codigo(trade, {10243, 10244}):
                cantidad_entera_fallback = int(importe_a_usar // precio_actual)
                if cantidad_entera_fallback >= 1:
                    log(f"COMPRAS: {ticker} - este valor no admite fracciones via API (ni cashQty ni "
                        f"cantidad directa); reintentando con {cantidad_entera_fallback} acciones enteras.")
                    orden = crear_orden_mercado('BUY', cantidad_entera_fallback)
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

            if compra_confirmada and cantidad_antes_compra <= 1e-6:
                # Posicion nueva desde cero (no una ampliacion para promediar
                # a la baja): se registra AHORA como fecha de apertura, para
                # que el resumen de cierre de mercado la muestre aunque
                # reqExecutions() ya no la tenga en dias posteriores.
                registrar_apertura_de_posicion(activo["mercado"], ticker)
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
    if ahora.weekday() >= 5:
        return False
    cierre_hoy = ahora.replace(hour=hora_cierre.hour, minute=hora_cierre.minute,
                                second=0, microsecond=0)
    minutos_desde_cierre = (ahora - cierre_hoy).total_seconds() / 60
    margen = 240  # 4 horas: amplio margen de seguridad frente a caidas/congelaciones
    return 0 <= minutos_desde_cierre <= margen


def valor_en_eur(valor, currency):
    """Convierte un valor a EUR, pasando por USD como paso intermedio."""
    return valor_en_usd(valor, currency) / TIPO_CAMBIO_EUR_USD


def generar_resumen_cierre_mercado(ib, mercado):
    """Genera y muestra una tabla con las posiciones abiertas (desde cuando,
    cantidad, coste medio con comision estimada) y las operaciones cerradas
    hoy (abierta desde, cerrada a las, beneficio % aprox., ganancia), para
    los valores del mercado indicado. Los importes se muestran en moneda
    local del mercado y tambien su equivalente en EUR."""
    monedas_del_mercado = [c for c, m in CURRENCY_A_MERCADO.items() if m == mercado]

    def clave_orden(symbol):
        """Orden numerico si el simbolo son solo digitos (HK/Corea), alfabetico si no (US)."""
        return (0, int(symbol)) if symbol.isdigit() else (1, symbol)

    posiciones = sorted(
        [p for p in ib.positions()
         if p.position > 0 and p.contract.currency in monedas_del_mercado],
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
        if e.contract.currency not in monedas_del_mercado:
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
        comision_compra = estimar_comision(total_invertido, currency_op, cantidad_vendida)
        comision_venta = estimar_comision(valor_base, currency_op, cantidad_vendida)
        comision_estimada = comision_compra + comision_venta
        ganancia = ganancia_bruta - comision_estimada
        beneficio_pct = (ganancia / total_invertido * 100) if total_invertido else 0.0
        total_ganancia_usd_acumulada += valor_en_usd(ganancia, ventas_hoy[0].contract.currency)
        total_ganancia_eur_acumulada += valor_en_eur(ganancia, ventas_hoy[0].contract.currency)

        log(f"{symbol:<10}{abierta_desde_str:<18}{cerrada_a_las_str:<18}{cantidad_vendida:>10.4f}"
            f"{coste_medio_compra:>14.4f}{total_invertido:>14.2f}{beneficio_pct:>12.2f}%{ganancia:>12.2f}")

    if simbolos_con_venta_hoy:
        log("-" * 91)
        log(f"TOTAL ganancia hoy ({mercado}): {total_ganancia_usd_acumulada:.2f} USD / "
            f"{total_ganancia_eur_acumulada:.2f} EUR")

    log("=" * 60)
    log("(Beneficio % es aproximado: se calcula sobre el valor de la venta, "
        "no lote a lote, cuando ha habido varias compras/ventas parciales del mismo valor.)")


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


def ciclo_completo(ib, modo_texto="?"):
    log("=" * 60)
    log(f"Iniciando nuevo ciclo de revision. [{modo_texto}]")
    revisar_ventas(ib)
    revisar_compras(ib)
    log("Ciclo completado.")


def evitar_suspension_windows():
    """Pide a Windows que no suspenda el sistema ni el display mientras el
    bot esta activo. No evita un cierre de tapa forzado, pero si la mayoria
    de mecanismos de ahorro de energia por inactividad, incluyendo el modo
    de suspension moderna en portatiles. No tiene efecto en otros sistemas
    operativos."""
    try:
        import ctypes
        ES_CONTINUOUS = 0x80000000
        ES_SYSTEM_REQUIRED = 0x00000001
        ES_DISPLAY_REQUIRED = 0x00000002
        ES_AWAYMODE_REQUIRED = 0x00000040
        ctypes.windll.kernel32.SetThreadExecutionState(
            ES_CONTINUOUS | ES_SYSTEM_REQUIRED | ES_DISPLAY_REQUIRED | ES_AWAYMODE_REQUIRED
        )
        log("Suspension automatica de Windows desactivada (sistema, pantalla y modo ausente) "
            "mientras el bot este en marcha.")
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


def main():
    evitar_suspension_windows()
    escribir_pid()
    actualizar_latido()
    threading.Thread(target=vigilante_congelacion, daemon=True).start()

    ib = IB()
    ib.connect('127.0.0.1', 4002, clientId=1)
    ib.RequestTimeout = 30  # segundos: evita que cualquier peticion se quede colgada sin limite
    modo_texto = avisar_modo_cuenta(ib)

    resumenes_enviados_hoy = set()  # claves (mercado, fecha) para no repetir el resumen

    try:
        while True:
            if not conexion_esta_viva(ib):
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
                        log(f"Reconexion con IB Gateway completada (intento {intento}/{REINTENTOS_RECONEXION}).")
                        modo_texto = avisar_modo_cuenta(ib)
                        reconectado = True
                        break
                    except Exception as e:
                        log(f"Fallo el intento {intento}/{REINTENTOS_RECONEXION} de reconexion: {type(e).__name__}: {e}")
                        if intento < REINTENTOS_RECONEXION:
                            ib.sleep(ESPERA_ENTRE_REINTENTOS_RECONEXION_SEGUNDOS)

                if not reconectado:
                    log(f"No se pudo reconectar con IB Gateway tras {REINTENTOS_RECONEXION} intentos. "
                        f"Se reintentara en la siguiente vuelta.")
                    log(f"Esperando {INTERVALO_SEGUNDOS // 60} minutos hasta la siguiente revision...")
                    esperar_pumpeando(ib, INTERVALO_SEGUNDOS)
                    continue

            hoy = datetime.now().date()
            for mercado in ("US", "HK", "KR"):
                if justo_cerro_mercado(mercado) and (mercado, hoy) not in resumenes_enviados_hoy:
                    try:
                        generar_resumen_cierre_mercado(ib, mercado)
                    except Exception as e:
                        log(f"RESUMEN {mercado}: error al generar el resumen: {type(e).__name__}: {e}")
                    resumenes_enviados_hoy.add((mercado, hoy))

            hay_mercado_abierto = (es_horario_operativo("US")
                                    or es_horario_operativo("HK") or es_horario_operativo("KR")
                                    or en_alguna_ventana_pre_apertura())

            if not hay_mercado_abierto:
                segundos_espera = segundos_hasta_pre_apertura()
                segundos_espera = max(segundos_espera, 60)  # suelo minimo: nunca esperar casi 0
                minutos_espera = segundos_espera / 60
                log(f"Fuera de horario operativo en todos los mercados (US, HK, KR). "
                    f"Esperando {minutos_espera:.0f} minutos hasta {MINUTOS_ANTES_DE_APERTURA_PARA_DESPERTAR} "
                    f"min antes de la proxima apertura...")
                esperar_pumpeando(ib, segundos_espera)
                continue

            inicio_ciclo = time.monotonic()
            try:
                ciclo_completo(ib, modo_texto)
            except Exception as e:
                log(f"ERROR en el ciclo: {type(e).__name__}: {e}")
            duracion_ciclo = time.monotonic() - inicio_ciclo

            if duracion_ciclo > INTERVALO_SEGUNDOS:
                log(f"AVISO: el ciclo ha tardado {duracion_ciclo:.0f}s, mas que el intervalo "
                    f"configurado ({INTERVALO_SEGUNDOS}s). Se pasa a la siguiente revision sin esperar; "
                    f"considera subir INTERVALO_SEGUNDOS o reducir el numero de valores/temporalidades.")
                segundos_espera_siguiente = 0
            else:
                segundos_espera_siguiente = INTERVALO_SEGUNDOS - duracion_ciclo
                log(f"Ciclo completado en {duracion_ciclo:.0f}s. Esperando "
                    f"{segundos_espera_siguiente:.0f}s hasta la siguiente revision...")
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
            time.sleep(SEGUNDOS_ESPERA_TRAS_FALLO)
