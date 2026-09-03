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
import os
import threading
import time
from datetime import datetime, time as dt_time, timedelta
from collections import defaultdict
from zoneinfo import ZoneInfo

import pandas as pd
import requests
from ib_async import IB, Stock, Crypto, MarketOrder, LimitOrder, ExecutionFilter

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
    operativo ahora mismo, de lunes a viernes."""
    if mercado == "US":
        ahora = datetime.now(ZONA_NY)
        if ahora.weekday() >= 5:
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
    if ahora.weekday() >= 5:
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
    if ahora.weekday() >= 5:
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
        _aviso_datos_caidos_emitido = True

    intentos = 1 if disyuntor_activo else INTENTOS_MAXIMOS

    # Para contratos CRYPTO, IBKR exige whatToShow='AGGTRADES' en
    # reqHistoricalData; 'TRADES' (el que usan acciones/US/HK/KR) no esta
    # soportado para CRYPTO y no da un error claro, sino que se queda
    # colgado hasta agotar el timeout en todos los intentos (bug real visto
    # en produccion, sept. 2026 - el mismo sintoma que el problema de
    # exchange PAXOS/ZEROHASH, pero con causa distinta).
    what_to_show = 'AGGTRADES' if getattr(contrato, 'secType', None) == 'CRYPTO' else 'TRADES'

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
    Plan B/C como con las acciones."""
    return _sin_flags_legacy(LimitOrder(accion, cantidad, precio_limite))


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


def analizar_activo(ib, activo):
    contrato = crear_contrato(ib, activo)
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


def revisar_ventas(ib):
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
                # CIERRE_POR_MERCADO), asi que va directa a la logica normal
                # de MACD, con su propia construccion de orden (LMT +
                # totalQuantity fraccionario nativo, sin cashQty ni Plan B/C
                # -ver crear_orden_limitada_cripto()-).
                if beneficio_pct < UMBRAL_BENEFICIO_PCT:
                    log(f"VENTAS: {contrato.symbol} - {info_posicion} - beneficio neto {beneficio_pct:.2f}% "
                        f"(bruto {beneficio_pct_bruto:.2f}%), por debajo del umbral -> se mantiene.")
                    continue

                bajista_cripto = macd_5min_bajista(ib, contrato)
                if bajista_cripto is None:
                    log(f"VENTAS: {contrato.symbol} - {info_posicion} - datos insuficientes para MACD 5min, se mantiene por precaucion.")
                    continue

                if not bajista_cripto:
                    log(f"VENTAS: {contrato.symbol} - {info_posicion} - beneficio neto {beneficio_pct:.2f}% "
                        f"(bruto {beneficio_pct_bruto:.2f}%), MACD 5min ALCISTA -> se deja correr.")
                    continue

                log(f"VENTAS: {contrato.symbol} - {info_posicion} - beneficio neto {beneficio_pct:.2f}% "
                    f"(bruto {beneficio_pct_bruto:.2f}%), MACD 5min BAJISTA -> VENDIENDO (orden limitada).")
                precio_limite_cripto = calcular_precio_limite_venta(precio_actual, contrato.currency)
                orden = crear_orden_limitada_cripto('SELL', cantidad, precio_limite_cripto)
                trade = ib.placeOrder(contrato, orden)
                estado = esperar_estado_final_orden(ib, trade)
                log(f"VENTAS: {contrato.symbol} - orden limitada a {precio_limite_cripto} USD, estado: {estado}")
                if estado == 'Filled':
                    precio_ejecucion = getattr(trade.orderStatus, "avgFillPrice", None) or precio_limite_cripto
                    cantidad_ejecutada = getattr(trade.orderStatus, "filled", None) or cantidad
                    registrar_operacion_historial(mercado, contrato.symbol, "VENTA", cantidad_ejecutada,
                                                   precio_ejecucion, comision_total, contrato.currency,
                                                   coste_medio=coste_medio, beneficio_pct=beneficio_pct)
                    notificar_telegram(f"🔴 VENTA <b>{contrato.symbol}</b> ({mercado}): "
                                       f"{formato_es(cantidad_ejecutada, 6)} a {formato_es(precio_ejecucion, 4)} "
                                       f"{contrato.currency} ({formato_es(beneficio_pct, signo=True)}%)")
                else:
                    verificar_posicion_tras_orden_no_confirmada(ib, contrato, cantidad, f"VENTAS: {contrato.symbol}")
                continue

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
                if estado == 'Filled':
                    precio_ejecucion = getattr(trade.orderStatus, "avgFillPrice", None) or precio_limite
                    cantidad_ejecutada = getattr(trade.orderStatus, "filled", None) or cantidad
                    registrar_operacion_historial(mercado, contrato.symbol, "VENTA", cantidad_ejecutada,
                                                   precio_ejecucion, comision_total, contrato.currency,
                                                   coste_medio=coste_medio, beneficio_pct=beneficio_pct)
                    notificar_telegram(f"🔴 VENTA FORZADA <b>{contrato.symbol}</b> ({mercado}): "
                                       f"{formato_es(cantidad_ejecutada, 4)} a {formato_es(precio_ejecucion, 4)} "
                                       f"{contrato.currency} ({formato_es(beneficio_pct, signo=True)}%)")
                else:
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
                # Pre o postmercado de US: liquidez mucho menor que en sesion
                # regular, se usa orden LIMITADA al precio exacto (con
                # outsideRth) en vez de orden a mercado.
                usar_limite_fuera_horario = mercado == "US" and fuera_de_sesion_regular_us()
                tipo_orden_texto = "limitada al precio exacto (fuera de sesion regular)" if usar_limite_fuera_horario else "a mercado"
                log(f"VENTAS: {contrato.symbol} - {info_posicion} - beneficio neto {beneficio_pct:.2f}% "
                    f"(bruto {beneficio_pct_bruto:.2f}%), MACD 5min BAJISTA -> VENDIENDO (orden {tipo_orden_texto}).")

                def _orden_venta_cash(importe):
                    if usar_limite_fuera_horario:
                        return crear_orden_limitada_cash('SELL', importe, precio_actual, fuera_horario_regular=True)
                    return crear_orden_mercado_cash('SELL', importe)

                def _orden_venta(cant):
                    if usar_limite_fuera_horario:
                        return crear_orden_limitada('SELL', cant, precio_actual, fuera_horario_regular=True)
                    return crear_orden_mercado('SELL', cant)

                if es_cantidad_fraccionaria(cantidad):
                    orden = _orden_venta_cash(cantidad * precio_actual)
                else:
                    orden = _orden_venta(cantidad)
                trade = ib.placeOrder(contrato, orden)
                estado = esperar_estado_final_orden(ib, trade)
                log(f"VENTAS: {contrato.symbol} - orden {tipo_orden_texto}, "
                    f"estado: {estado}")
                if (es_cantidad_fraccionaria(cantidad) and estado != 'Filled'
                        and orden_rechazada_por_codigo(trade, {10244})):
                    # Ver Plan B en revisar_compras: la cuenta puede rechazar
                    # cashQty (10244) pero SI admitir la cantidad fraccionaria
                    # puesta directamente (confirmado a mano en cuenta real).
                    log(f"VENTAS: {contrato.symbol} - no admite el importe en efectivo (cashQty) via API "
                        f"(error 10244); reintentando con la cantidad fraccionaria puesta directamente.")
                    orden = _orden_venta(cantidad)
                    trade = ib.placeOrder(contrato, orden)
                    estado = esperar_estado_final_orden(ib, trade)
                    log(f"VENTAS: {contrato.symbol} - orden {tipo_orden_texto} (cantidad fraccionaria directa), "
                        f"estado: {estado}")
                if estado == 'Filled':
                    precio_ejecucion = getattr(trade.orderStatus, "avgFillPrice", None) or precio_actual
                    cantidad_ejecutada = getattr(trade.orderStatus, "filled", None) or cantidad
                    registrar_operacion_historial(mercado, contrato.symbol, "VENTA", cantidad_ejecutada,
                                                   precio_ejecucion, comision_total, contrato.currency,
                                                   coste_medio=coste_medio, beneficio_pct=beneficio_pct)
                    notificar_telegram(f"🔴 VENTA <b>{contrato.symbol}</b> ({mercado}): "
                                       f"{formato_es(cantidad_ejecutada, 4)} a {formato_es(precio_ejecucion, 4)} "
                                       f"{contrato.currency} ({formato_es(beneficio_pct, signo=True)}%)")
                else:
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

            if activo["mercado"] == "CRYPTO":
                # Cripto siempre admite fracciones nativas via LMT (ver
                # crear_orden_limitada_cripto()): sin cashQty, sin Plan B/C,
                # sin distincion pre/postmercado (opera de forma continua).
                if importe_a_usar < VALOR_MINIMO_OPERACION_CRIPTO_USD:
                    log(f"COMPRAS: {ticker} - senal de COMPRA pero el margen disponible "
                        f"({importe_a_usar:.2f} USD) no llega al minimo de "
                        f"{VALOR_MINIMO_OPERACION_CRIPTO_USD:.2f} USD por operacion, se omite.")
                    continue

                cantidad_cripto = round(importe_a_usar / precio_actual, DECIMALES_FRACCION_CRIPTO)
                if cantidad_cripto <= 0:
                    log(f"COMPRAS: {ticker} - senal de COMPRA pero el importe calculado ({importe_a_usar:.2f} "
                        f"USD) no llega a una cantidad valida al precio actual ({precio_actual} USD), se omite.")
                    continue

                log(f"COMPRAS: {ticker} (CRYPTO) - senal de COMPRA, comprando ~{cantidad_cripto:g} unidades "
                    f"a ~{precio_actual} USD (posicion actual: {valor_posicion_actual_usd:.2f} USD, "
                    f"limite: {limite_por_valor_usd:.2f} USD).")
                cantidad_antes_compra_cripto = next(
                    (p.position for p in posiciones_actuales if p.contract.symbol == ticker and p.position > 0), 0.0)

                orden = crear_orden_limitada_cripto('BUY', cantidad_cripto, precio_actual)
                trade = ib.placeOrder(contrato, orden)
                estado = esperar_estado_final_orden(ib, trade)
                log(f"COMPRAS: {ticker} - estado de la orden: {estado}")

                compra_confirmada_cripto = estado == 'Filled'
                if not compra_confirmada_cripto:
                    cantidad_tras_compra_cripto = verificar_posicion_tras_orden_no_confirmada(
                        ib, contrato, cantidad_antes_compra_cripto, f"COMPRAS: {ticker}")
                    compra_confirmada_cripto = cantidad_tras_compra_cripto > cantidad_antes_compra_cripto + 1e-6

                if compra_confirmada_cripto:
                    precio_ejecucion = getattr(trade.orderStatus, "avgFillPrice", None) or precio_actual
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

    try:
        while True:
            if peticion_de_parada_pendiente():
                log(f"Parada solicitada (archivo {ARCHIVO_DETENER} detectado): cerrando limpiamente. "
                    f"No se borra aqui el archivo -run.bot.bat lo borra al ver que fue una parada "
                    f"limpia, para no reiniciar el bucle-.")
                return

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
                        # No hace falta volver a registrar on_error_ib: es el mismo
                        # objeto `ib`, y el listener de errorEvent sobrevive a
                        # disconnect()/connect() (solo se re-crearia con IB() nuevo).
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
                                    or es_horario_operativo("CRYPTO")
                                    or en_alguna_ventana_pre_apertura())

            if not hay_mercado_abierto:
                segundos_espera = segundos_hasta_pre_apertura()
                segundos_espera = max(segundos_espera, 60)  # suelo minimo: nunca esperar casi 0
                minutos_espera = segundos_espera / 60
                log(f"Fuera de horario operativo en todos los mercados (US, HK, KR, CRYPTO). "
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
