"""
Tests manuales (sin pytest) para las funciones de logica pura de
bot_completo.py: no requieren conexion a IBKR, solo importan el modulo y
prueban calculos matematicos y de horarios con datos simulados.
"""
import os
import sys
import tempfile
import time
import types
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import bot_completo as bot
import pandas as pd

# Cada llamada a bot.log()/bot.actualizar_latido() escribe en disco (senal de
# vida para el vigilante externo). Se redirige a una carpeta temporal desde
# el principio para que correr los tests no deje "latido_bot.txt" ni
# "bot.pid" tirados en la carpeta del repo.
_DIR_TEMP_ESTADO_RUNTIME = tempfile.mkdtemp()
bot.ARCHIVO_LATIDO = os.path.join(_DIR_TEMP_ESTADO_RUNTIME, "latido_bot.txt")
bot.ARCHIVO_PID = os.path.join(_DIR_TEMP_ESTADO_RUNTIME, "bot.pid")
bot.ARCHIVO_HISTORIAL_OPERACIONES = os.path.join(_DIR_TEMP_ESTADO_RUNTIME, "historial_operaciones_ibkr_test.json")

fallos = []


def check(nombre, condicion, detalle=""):
    estado = "OK  " if condicion else "FAIL"
    print(f"[{estado}] {nombre}" + (f" -- {detalle}" if detalle and not condicion else ""))
    if not condicion:
        fallos.append(nombre)


# ---------------------------------------------------------------------------
# 1. calcular_macd: sobre una serie conocida, comprobar forma y valores
#    finitos, y que una serie claramente alcista de un valor MACD positivo.
# ---------------------------------------------------------------------------
precios_alcistas = pd.Series([100 + i * 0.5 for i in range(60)])  # tendencia subiendo
macd, senal, hist = bot.calcular_macd(precios_alcistas)
check("MACD: longitud de salida coincide con la entrada", len(macd) == len(precios_alcistas))
check("MACD: en tendencia alcista sostenida, MACD final > 0",
      macd.iloc[-1] > 0, f"macd.iloc[-1]={macd.iloc[-1]}")
check("MACD: en tendencia alcista sostenida, MACD > señal (cruce alcista)",
      macd.iloc[-1] > senal.iloc[-1])

precios_bajistas = pd.Series([100 - i * 0.5 for i in range(60)])  # tendencia bajando
macd_b, senal_b, _ = bot.calcular_macd(precios_bajistas)
check("MACD: en tendencia bajista sostenida, MACD < señal",
      macd_b.iloc[-1] < senal_b.iloc[-1])

precios_planos = pd.Series([100.0] * 60)
macd_p, senal_p, hist_p = bot.calcular_macd(precios_planos)
check("MACD: precio plano da histograma ~0", abs(hist_p.iloc[-1]) < 1e-9)


# ---------------------------------------------------------------------------
# 2. tick_size_krx: tabla oficial de KRX
# ---------------------------------------------------------------------------
casos_tick = [
    (500, 1), (1999, 1),
    (2000, 5), (4999, 5),
    (5000, 10), (19999, 10),
    (20000, 50), (49999, 50),
    (50000, 100), (199999, 100),
    (200000, 500), (499999, 500),
    (500000, 1000), (1000000, 1000),
]
for precio, esperado in casos_tick:
    obtenido = bot.tick_size_krx(precio)
    check(f"tick_size_krx({precio}) == {esperado}", obtenido == esperado, f"obtenido={obtenido}")


# ---------------------------------------------------------------------------
# 3. calcular_precio_limite_venta
# ---------------------------------------------------------------------------
pl_usd = bot.calcular_precio_limite_venta(100.0, "USD")
check("precio limite USD: 0.2% por debajo de 100 -> 99.8",
      abs(pl_usd - 99.8) < 1e-9, f"obtenido={pl_usd}")

pl_krw = bot.calcular_precio_limite_venta(10050, "KRW")
# 10050 * 0.998 = 10029.9 -> tick de 10 (rango 5000-20000) -> redondeo hacia abajo a 10020
check("precio limite KRW: redondeado al tick de KRX hacia abajo",
      pl_krw % bot.tick_size_krx(pl_krw) == 0, f"obtenido={pl_krw}")
check("precio limite KRW: valor esperado 10020", pl_krw == 10020, f"obtenido={pl_krw}")


# ---------------------------------------------------------------------------
# 4. valor_en_usd / valor_en_eur / comisiones
# ---------------------------------------------------------------------------
check("valor_en_usd: USD pasa igual", bot.valor_en_usd(100, "USD") == 100)
check("valor_en_usd: EUR->USD multiplica por el tipo de cambio",
      abs(bot.valor_en_usd(100, "EUR") - 100 * bot.TIPO_CAMBIO_EUR_USD) < 1e-9)
check("valor_en_usd: HKD->USD divide por el tipo de cambio",
      abs(bot.valor_en_usd(780, "HKD") - 100) < 1e-6)

# estimar_comision: tarifas reales de IBKR (tiered, Nivel I) por mercado.

# US, fraccionaria (cantidad no entera): 1% del valor, minimo 0.01 USD.
comision_us_frac_pequena = bot.estimar_comision(0.75, "USD", 0.05)  # 1% de 0.75 = 0.0075 < minimo
check("estimar_comision US fraccionaria: aplica el minimo (0.01 USD) cuando el 1% es menor",
      abs(comision_us_frac_pequena - bot.COMISION_US_FRACCION_MINIMA) < 1e-9,
      f"obtenido={comision_us_frac_pequena}")

comision_us_frac_grande = bot.estimar_comision(1000, "USD", 3.544)  # 1% de 1000 = 10 > minimo
check("estimar_comision US fraccionaria: aplica el 1% cuando supera el minimo",
      abs(comision_us_frac_grande - 1000 * bot.COMISION_US_FRACCION_PCT) < 1e-9,
      f"obtenido={comision_us_frac_grande}")

# US, acciones ENTERAS: 0.0035 USD/accion, minimo 0.35 USD/orden.
comision_us_entera_pequena = bot.estimar_comision(300, "USD", 3)  # 3*0.0035=0.0105 < minimo
check("estimar_comision US acciones enteras: aplica el minimo (0.35 USD) en ordenes pequeñas",
      abs(comision_us_entera_pequena - bot.COMISION_US_MINIMA) < 1e-9,
      f"obtenido={comision_us_entera_pequena}")

comision_us_entera_grande = bot.estimar_comision(50_000, "USD", 500)  # 500*0.0035=1.75 > minimo
check("estimar_comision US acciones enteras: aplica 0.0035 USD/accion cuando supera el minimo",
      abs(comision_us_entera_grande - 500 * bot.COMISION_US_POR_ACCION) < 1e-9,
      f"obtenido={comision_us_entera_grande}")

comision_us_tope = bot.estimar_comision(10, "USD", 1000)  # 1000*0.0035=3.5, pero tope 1% de 10 = 0.10
check("estimar_comision US acciones enteras: nunca supera el tope del 1% del valor negociado",
      abs(comision_us_tope - 10 * bot.COMISION_MAX_PCT) < 1e-9,
      f"obtenido={comision_us_tope}")

# HK: 0.05% del valor, minimo ~2.25 USD equivalente en HKD.
comision_hk_minima = bot.estimar_comision(1000, "HKD")  # 0.05% de 1000 HKD = 0.5, por debajo del minimo
minimo_hk_esperado = bot.COMISION_HK_MINIMA_USD * bot.TIPO_CAMBIO_USD_HKD
check("estimar_comision HK: aplica el minimo (~2.25 USD equivalente) en ordenes pequeñas",
      abs(comision_hk_minima - minimo_hk_esperado) < 1e-6,
      f"obtenido={comision_hk_minima}, esperado={minimo_hk_esperado}")

comision_hk_grande = bot.estimar_comision(1_000_000, "HKD")
check("estimar_comision HK: aplica el 0.05% cuando supera el minimo",
      abs(comision_hk_grande - 1_000_000 * bot.COMISION_HK_PCT) < 1e-6,
      f"obtenido={comision_hk_grande}")

# KR: 0.06% del valor, minimo 4000 KRW.
comision_kr_minima = bot.estimar_comision(100_000, "KRW")  # 0.06% de 100000 = 60, por debajo del minimo
check("estimar_comision KR: aplica el minimo (4000 KRW) en ordenes pequeñas",
      abs(comision_kr_minima - bot.COMISION_KR_MINIMA_KRW) < 1e-6,
      f"obtenido={comision_kr_minima}")

comision_kr_grande = bot.estimar_comision(100_000_000, "KRW")
check("estimar_comision KR: aplica el 0.06% cuando supera el minimo",
      abs(comision_kr_grande - 100_000_000 * bot.COMISION_KR_PCT) < 1e-6,
      f"obtenido={comision_kr_grande}")


# ---------------------------------------------------------------------------
# 5. es_horario_operativo / minutos_hasta_cierre / ventanas de cierre
#    Se prueba con datetimes fijos inyectados, sin depender de "ahora".
# ---------------------------------------------------------------------------
class _RelojFijo(datetime):
    _instante_fijo = None

    @classmethod
    def now(cls, tz=None):
        if tz is not None:
            return cls._instante_fijo.astimezone(tz)
        return cls._instante_fijo


def con_reloj_fijo(instante, fn, *args, **kwargs):
    original = bot.datetime
    _RelojFijo._instante_fijo = instante
    bot.datetime = _RelojFijo
    try:
        return fn(*args, **kwargs)
    finally:
        bot.datetime = original


# Miercoles 10:00 ET -> mercado US abierto (regular)
miercoles_us_abierto = datetime(2026, 8, 12, 10, 0, tzinfo=bot.ZONA_NY)
check("es_horario_operativo US: miercoles 10:00 ET -> abierto",
      con_reloj_fijo(miercoles_us_abierto, bot.es_horario_operativo, "US") is True)

# Miercoles 20:00 ET -> mercado US cerrado (justo en el limite exclusivo del
# postmercado extendido: fuera de premercado, regular Y postmercado)
miercoles_us_cerrado = datetime(2026, 8, 12, 20, 0, tzinfo=bot.ZONA_NY)
check("es_horario_operativo US: miercoles 20:00 ET -> cerrado",
      con_reloj_fijo(miercoles_us_cerrado, bot.es_horario_operativo, "US") is False)

# Sabado 10:00 ET -> cerrado por ser fin de semana
sabado_us = datetime(2026, 8, 15, 10, 0, tzinfo=bot.ZONA_NY)
check("es_horario_operativo US: sabado -> cerrado (fin de semana)",
      con_reloj_fijo(sabado_us, bot.es_horario_operativo, "US") is False)

# Justo en el limite del cierre REGULAR (16:00 ET): ahora sigue ABIERTO,
# porque entra el postmercado extendido (16:00-20:00 ET) - decision del
# usuario de poder comprar (no vender) en esa franja.
limite_cierre_regular_us = datetime(2026, 8, 12, 16, 0, tzinfo=bot.ZONA_NY)
check("es_horario_operativo US: exactamente a las 16:00 ET -> ABIERTO (empieza el postmercado)",
      con_reloj_fijo(limite_cierre_regular_us, bot.es_horario_operativo, "US") is True)

# 19:59 ET -> dentro del postmercado extendido (abierto)
diecinueve_59 = datetime(2026, 8, 12, 19, 59, tzinfo=bot.ZONA_NY)
check("es_horario_operativo US: 19:59 ET -> abierto (postmercado)",
      con_reloj_fijo(diecinueve_59, bot.es_horario_operativo, "US") is True)

# fuera_de_sesion_regular_us / en_postmercado_us: premercado, regular y postmercado
premercado_us = datetime(2026, 8, 12, 6, 0, tzinfo=bot.ZONA_NY)
check("fuera_de_sesion_regular_us: 6:00 ET (premercado) -> True",
      con_reloj_fijo(premercado_us, bot.fuera_de_sesion_regular_us) is True)
check("en_postmercado_us: 6:00 ET (premercado) -> False",
      con_reloj_fijo(premercado_us, bot.en_postmercado_us) is False)

check("fuera_de_sesion_regular_us: 10:00 ET (sesion regular) -> False",
      con_reloj_fijo(miercoles_us_abierto, bot.fuera_de_sesion_regular_us) is False)

postmercado_us = datetime(2026, 8, 12, 18, 0, tzinfo=bot.ZONA_NY)
check("fuera_de_sesion_regular_us: 18:00 ET (postmercado) -> True",
      con_reloj_fijo(postmercado_us, bot.fuera_de_sesion_regular_us) is True)
check("en_postmercado_us: 18:00 ET (postmercado) -> True",
      con_reloj_fijo(postmercado_us, bot.en_postmercado_us) is True)

# minutos_hasta_cierre: 15:50 ET -> 10 minutos para el cierre
quince_cincuenta = datetime(2026, 8, 12, 15, 50, tzinfo=bot.ZONA_NY)
minutos = con_reloj_fijo(quince_cincuenta, bot.minutos_hasta_cierre, "US")
check("minutos_hasta_cierre US a las 15:50 ET -> 10.0",
      abs(minutos - 10.0) < 1e-9, f"obtenido={minutos}")

# en_ventana_venta_forzada: dentro de los ultimos 15 min antes del cierre
check("en_ventana_venta_forzada US a las 15:50 ET (10 min para cerrar) -> True",
      con_reloj_fijo(quince_cincuenta, bot.en_ventana_venta_forzada, "US") is True)

# en_ventana_sin_compra: dentro de los ultimos 90 min antes del cierre
catorce_cuarenta = datetime(2026, 8, 12, 14, 40, tzinfo=bot.ZONA_NY)  # 80 min para cerrar
check("en_ventana_sin_compra US a las 14:40 ET (80 min para cerrar) -> True",
      con_reloj_fijo(catorce_cuarenta, bot.en_ventana_sin_compra, "US") is True)

trece_cero = datetime(2026, 8, 12, 13, 0, tzinfo=bot.ZONA_NY)  # 180 min para cerrar
check("en_ventana_sin_compra US a las 13:00 ET (180 min para cerrar) -> False",
      con_reloj_fijo(trece_cero, bot.en_ventana_sin_compra, "US") is False)


# ---------------------------------------------------------------------------
# 6. analizar_activo: decision de COMPRA/BLOQUEADO/SIN_SENAL con IB simulado
# ---------------------------------------------------------------------------
class _Vela:
    def __init__(self, close):
        self.close = close


class _ContratoFalso:
    def __init__(self, symbol):
        self.symbol = symbol
        self.conId = 12345  # simula contrato ya resuelto


class _IBFalso:
    """Doble de prueba minimo: qualifyContracts no hace nada, y
    reqHistoricalData siempre devuelve la misma serie alcista de precios
    (suficiente para que el MACD de cada temporalidad salga alcista)."""

    def __init__(self, precios):
        self.precios = precios

    def qualifyContracts(self, contrato):
        contrato.conId = 12345  # simula que IBKR resolvio el contrato correctamente

    def reqHistoricalData(self, contrato, **kwargs):
        return [_Vela(p) for p in self.precios]

    def sleep(self, segundos):
        pass


activo_prueba = {"ticker": "TEST", "exchange": "SMART", "currency": "USD", "mercado": "US"}

# Serie alcista LINEAL (pendiente constante), 60 puntos: las 5 temporalidades
# cortas (incluida 1h) dan alcista, así que el atajo de las "4 cortas
# alcistas" (1min/5min/15min/30min) ya compra directamente, SIN mirar si las
# largas (dia/semana) confirman con momentum acelerando. Con esta regla, el
# viejo resultado "BLOQUEADO" (cortas ok pero largas no) ya no puede darse:
# en cuanto las cortas estan alineadas, el atajo compra antes de llegar ahi.
precios_lineal = [100 + i * 0.3 for i in range(60)]
ib_falso_lineal = _IBFalso(precios_lineal)
_, decision_lineal = bot.analizar_activo(ib_falso_lineal, activo_prueba)
check("analizar_activo: 4 cortas alcistas (aunque las largas no aceleren) -> COMPRA por atajo",
      decision_lineal == "COMPRA", f"decision={decision_lineal}")

# Serie alcista que ACELERA (curva exponencial): tambien COMPRA (por el
# atajo Y por la regla larga, ambas coinciden aqui).
precios_compra = [100 * (1.02 ** i) for i in range(60)]
ib_falso_compra = _IBFalso(precios_compra)
_, decision_compra = bot.analizar_activo(ib_falso_compra, activo_prueba)
check("analizar_activo: tendencia alcista ACELERANDO -> decision COMPRA",
      decision_compra == "COMPRA", f"decision={decision_compra}")


# ---------------------------------------------------------------------------
# 6b. Atajo de las 4 cortas: control fino por temporalidad, usando un IB
#     simulado que devuelve una serie distinta segun el barSize pedido.
# ---------------------------------------------------------------------------
SERIE_ALCISTA = [100 + i * 0.3 for i in range(60)]
SERIE_BAJISTA = [100 - i * 0.3 for i in range(60)]
# Para las temporalidades largas, "en contra" exige aceleracion BAJISTA real
# (una simple caida lineal no basta: como se vio en el test 6, la formula
# del histograma MACD converge de forma similar en ambas direcciones con
# pendiente constante, asi que una caida lineal puede dar histograma
# "creciente" igual que una subida lineal).
SERIE_ACELERANDO_BAJA = [300 - 100 * (1.02 ** i) for i in range(60)]


class _IBPorTemporalidad:
    def __init__(self, series_por_barsize):
        self.series_por_barsize = series_por_barsize

    def qualifyContracts(self, contrato):
        contrato.conId = 12345

    def reqHistoricalData(self, contrato, **kwargs):
        precios = self.series_por_barsize[kwargs["barSizeSetting"]]
        return [_Vela(p) for p in precios]

    def sleep(self, segundos):
        pass

    def positions(self):
        # Para activos CRYPTO, crear_contrato() llama a
        # descubrir_exchange_cripto(ib), que necesita este metodo (sin
        # posiciones previas -> usa el exchange del propio activo).
        return []


# Las 4 cortas alcistas, pero 1 hora y las largas BAJISTAS: el atajo debe
# ganar y dar COMPRA, aunque con la logica antigua (sin atajo) esto habria
# dado SIN_SENAL (cortas_ok=False porque 1h esta en contra).
series_atajo = {
    "1 min": SERIE_ALCISTA, "5 mins": SERIE_ALCISTA, "15 mins": SERIE_ALCISTA,
    "30 mins": SERIE_ALCISTA,
    "1 hour": SERIE_BAJISTA, "1 day": SERIE_ACELERANDO_BAJA, "1 week": SERIE_ACELERANDO_BAJA,
}
_, decision_atajo = bot.analizar_activo(_IBPorTemporalidad(series_atajo), activo_prueba)
check("analizar_activo: 4 cortas alcistas + 1h/dia/semana bajistas -> COMPRA (el atajo manda)",
      decision_atajo == "COMPRA", f"decision={decision_atajo}")

# Si SOLO una de las 4 cortas requeridas esta bajista (p.ej. 1 minuto), el
# atajo NO debe activarse. Con el resto de temporalidades tambien bajistas,
# la decision cae en SIN_SENAL (no BLOQUEADO, porque cortas_ok tampoco se
# cumple al fallar 1min).
series_sin_atajo = {
    "1 min": SERIE_BAJISTA, "5 mins": SERIE_ALCISTA, "15 mins": SERIE_ALCISTA,
    "30 mins": SERIE_ALCISTA,
    "1 hour": SERIE_ALCISTA, "1 day": SERIE_ACELERANDO_BAJA, "1 week": SERIE_ACELERANDO_BAJA,
}
_, decision_sin_atajo = bot.analizar_activo(_IBPorTemporalidad(series_sin_atajo), activo_prueba)
check("analizar_activo: si UNA de las 4 cortas esta bajista (y 3 de 7 en contra "
      "en total) -> el atajo no se activa y tampoco compra por la regla vieja",
      decision_sin_atajo == "SIN_SENAL", f"decision={decision_sin_atajo}")


# ---------------------------------------------------------------------------
# 6c. Atajo EXCLUSIVO de cripto (1/3/10/20 min, peticion del usuario, sept.
#     2026): independiente del atajo general (1/5/15/30 min) - debe poder
#     dar COMPRA aunque el analisis normal de 7 temporalidades NO lo haria.
# ---------------------------------------------------------------------------
activo_cripto_prueba = {"ticker": "BTC", "exchange": bot.EXCHANGE_CRYPTO, "currency": "USD", "mercado": "CRYPTO"}
bot._exchange_cripto_cache = None

# 5/15/30min, 1h, dia y semana BAJISTAS (el analisis normal daria SIN_SENAL,
# como en el test anterior con activo_prueba), pero 1/3/10/20 min (el atajo
# de cripto) TODAS alcistas -> debe dar COMPRA de todos modos.
series_atajo_cripto = {
    "1 min": SERIE_ALCISTA, "5 mins": SERIE_BAJISTA, "15 mins": SERIE_BAJISTA, "30 mins": SERIE_BAJISTA,
    "1 hour": SERIE_BAJISTA, "1 day": SERIE_ACELERANDO_BAJA, "1 week": SERIE_ACELERANDO_BAJA,
    "3 mins": SERIE_ALCISTA, "10 mins": SERIE_ALCISTA, "20 mins": SERIE_ALCISTA,
}
_, decision_atajo_cripto = bot.analizar_activo(_IBPorTemporalidad(series_atajo_cripto), activo_cripto_prueba)
check("analizar_activo CRYPTO: atajo propio (1/3/10/20 min) todas alcistas -> COMPRA, "
      "aunque el analisis de 7 temporalidades por si solo daria SIN_SENAL",
      decision_atajo_cripto == "COMPRA", f"decision={decision_atajo_cripto}")

# Igual que arriba, pero con 10 min BAJISTA: el atajo de cripto no debe
# activarse (hacen falta las 4), y sin el atajo general tampoco activo, la
# decision cae en la misma SIN_SENAL de siempre.
series_sin_atajo_cripto = dict(series_atajo_cripto, **{"10 mins": SERIE_BAJISTA})
bot._exchange_cripto_cache = None
_, decision_sin_atajo_cripto = bot.analizar_activo(_IBPorTemporalidad(series_sin_atajo_cripto), activo_cripto_prueba)
check("analizar_activo CRYPTO: si UNA de las 4 del atajo propio esta bajista (10 min), "
      "no se activa -> SIN_SENAL",
      decision_sin_atajo_cripto == "SIN_SENAL", f"decision={decision_sin_atajo_cripto}")

# El atajo de cripto NUNCA se comprueba para acciones (activo_prueba, US):
# reutiliza el mismo series_atajo_cripto (que SI tiene 3/10/20 min alcistas)
# pero al ser mercado US no debe importar -> misma SIN_SENAL de antes.
_, decision_no_cripto = bot.analizar_activo(_IBPorTemporalidad(series_atajo_cripto), activo_prueba)
check("analizar_activo: el atajo de 1/3/10/20 min NUNCA se aplica a acciones (mercado US)",
      decision_no_cripto == "SIN_SENAL", f"decision={decision_no_cripto}")


# --- atajo_cripto_alcista() aislado: reutiliza el resultado de 1 minuto en
#     vez de pedirlo otra vez, y no llama a IBKR si ya viene None o False ---
class _IBContadorLlamadas:
    def __init__(self, series_por_barsize):
        self.series_por_barsize = series_por_barsize
        self.llamadas = []

    def reqHistoricalData(self, contrato, **kwargs):
        self.llamadas.append(kwargs["barSizeSetting"])
        return [_Vela(p) for p in self.series_por_barsize[kwargs["barSizeSetting"]]]


ib_contador = _IBContadorLlamadas({})
check("atajo_cripto_alcista: con un_minuto_alcista=None, no llama a IBKR y devuelve None",
      bot.atajo_cripto_alcista(ib_contador, None, None) is None and ib_contador.llamadas == [],
      f"llamadas={ib_contador.llamadas}")

ib_contador_2 = _IBContadorLlamadas({})
check("atajo_cripto_alcista: con un_minuto_alcista=False, no llama a IBKR y devuelve False",
      bot.atajo_cripto_alcista(ib_contador_2, None, False) is False and ib_contador_2.llamadas == [],
      f"llamadas={ib_contador_2.llamadas}")

ib_todas_alcistas = _IBContadorLlamadas({"3 mins": SERIE_ALCISTA, "10 mins": SERIE_ALCISTA, "20 mins": SERIE_ALCISTA})
check("atajo_cripto_alcista: 1min ya alcista + las otras 3 tambien -> True",
      bot.atajo_cripto_alcista(ib_todas_alcistas, None, True) is True)

ib_una_bajista = _IBContadorLlamadas({"3 mins": SERIE_ALCISTA, "10 mins": SERIE_BAJISTA, "20 mins": SERIE_ALCISTA})
check("atajo_cripto_alcista: 1min alcista pero 10min bajista -> False",
      bot.atajo_cripto_alcista(ib_una_bajista, None, True) is False)

ib_pocos_datos_atajo = _IBContadorLlamadas({"3 mins": [100, 101], "10 mins": SERIE_ALCISTA, "20 mins": SERIE_ALCISTA})
check("atajo_cripto_alcista: menos de 35 velas en una temporalidad -> None",
      bot.atajo_cripto_alcista(ib_pocos_datos_atajo, None, True) is None)


# Muy pocas velas (menos de 35) -> SIN_DATOS
ib_falso_pocos_datos = _IBFalso([100, 101, 102])
_, decision_pocos = bot.analizar_activo(ib_falso_pocos_datos, activo_prueba)
check("analizar_activo: menos de 35 velas -> decision SIN_DATOS",
      decision_pocos == "SIN_DATOS", f"decision={decision_pocos}")

# Serie bajista sostenida -> no deberia ser COMPRA
precios_venta = [100 - i * 0.3 for i in range(60)]
ib_falso_venta = _IBFalso(precios_venta)
_, decision_venta = bot.analizar_activo(ib_falso_venta, activo_prueba)
check("analizar_activo: serie bajista sostenida -> decision distinta de COMPRA",
      decision_venta != "COMPRA", f"decision={decision_venta}")

# Simbolo no resuelto (conId vacio) -> SIMBOLO_NO_RESUELTO
class _IBFalsoNoResuelto(_IBFalso):
    def qualifyContracts(self, contrato):
        contrato.conId = None


class _ContratoSinResolver:
    def __init__(self, *a, **k):
        self.symbol = a[0] if a else "TEST"
        self.conId = None


orig_stock = bot.Stock
bot.Stock = lambda *a, **k: _ContratoSinResolver(*a, **k)
try:
    _, decision_no_resuelto = bot.analizar_activo(_IBFalsoNoResuelto([]), activo_prueba)
finally:
    bot.Stock = orig_stock
check("analizar_activo: conId vacio -> SIMBOLO_NO_RESUELTO",
      decision_no_resuelto == "SIMBOLO_NO_RESUELTO", f"decision={decision_no_resuelto}")


# ---------------------------------------------------------------------------
# 7. pedir_velas: reintentos ante fallo, y que no reintente si hay exito
# ---------------------------------------------------------------------------
class _IBReintentos:
    def __init__(self, respuestas):
        self.respuestas = list(respuestas)
        self.llamadas = 0
        self.sleeps = []

    def reqHistoricalData(self, contrato, **kwargs):
        r = self.respuestas[self.llamadas]
        self.llamadas += 1
        if isinstance(r, Exception):
            raise r
        return r

    def sleep(self, segundos):
        self.sleeps.append(segundos)


contrato_falso = _ContratoFalso("TEST")

# Se resetea el cortacircuitos global de "datos de mercado caidos" antes de
# estos tests, para que no dependa de cuantos fallos hayan quedado
# acumulados de secciones anteriores.
bot._fallos_seguidos_datos = 0
bot._aviso_datos_caidos_emitido = False

# Primer intento vacio, segundo intento con datos -> deberia reintentar 1 vez y devolver datos
ib_reintento_ok = _IBReintentos([[], [_Vela(100)]])
resultado = bot.pedir_velas(ib_reintento_ok, contrato_falso, "1 D", "1 min")
check("pedir_velas: reintenta tras respuesta vacia y devuelve datos en el 2o intento",
      len(resultado) == 1 and ib_reintento_ok.llamadas == 2,
      f"llamadas={ib_reintento_ok.llamadas}, resultado={resultado}")

# Todos los intentos fallan -> lista vacia, y se agotan los INTENTOS_MAXIMOS
ib_reintento_fail = _IBReintentos([[], [], []])
resultado_fail = bot.pedir_velas(ib_reintento_fail, contrato_falso, "1 D", "1 min")
check("pedir_velas: agota los reintentos y devuelve lista vacia si nunca hay datos",
      resultado_fail == [] and ib_reintento_fail.llamadas == bot.INTENTOS_MAXIMOS,
      f"llamadas={ib_reintento_fail.llamadas}")

# Primer intento exitoso -> no debe reintentar
ib_reintento_exito_directo = _IBReintentos([[_Vela(100)], [_Vela(200)]])
resultado_directo = bot.pedir_velas(ib_reintento_exito_directo, contrato_falso, "1 D", "1 min")
check("pedir_velas: exito al primer intento no reintenta",
      ib_reintento_exito_directo.llamadas == 1, f"llamadas={ib_reintento_exito_directo.llamadas}")

# Excepcion en la llamada -> se trata igual que respuesta vacia (reintenta)
ib_reintento_excepcion = _IBReintentos([RuntimeError("fallo simulado"), [_Vela(100)]])
resultado_exc = bot.pedir_velas(ib_reintento_excepcion, contrato_falso, "1 D", "1 min")
check("pedir_velas: una excepcion en el primer intento no aborta, reintenta y consigue datos",
      len(resultado_exc) == 1, f"resultado={resultado_exc}")


# ---------------------------------------------------------------------------
# 7a-bis. whatToShow correcto segun el tipo de contrato: 'AGGTRADES' para
#         CRYPTO (bug real de produccion, sept. 2026: con 'TRADES' -tambien
#         usado para acciones- reqHistoricalData se quedaba colgado con
#         TimeoutError para BTC, sin dar un error de permisos claro),
#         'TRADES' para el resto (acciones US/HK/KR, sin cambios).
# ---------------------------------------------------------------------------
class _IBCapturaWhatToShow:
    def __init__(self):
        self.what_to_show_recibido = None

    def reqHistoricalData(self, contrato, **kwargs):
        self.what_to_show_recibido = kwargs.get("whatToShow")
        return [_Vela(100)]

    def sleep(self, segundos):
        pass


class _ContratoCryptoFalso(_ContratoFalso):
    secType = "CRYPTO"


class _ContratoForexFalso(_ContratoFalso):
    secType = "CASH"


ib_captura_crypto = _IBCapturaWhatToShow()
bot.pedir_velas(ib_captura_crypto, _ContratoCryptoFalso("BTC"), "1 D", "5 mins")
check("pedir_velas: contrato CRYPTO pide whatToShow='AGGTRADES'",
      ib_captura_crypto.what_to_show_recibido == "AGGTRADES",
      f"whatToShow={ib_captura_crypto.what_to_show_recibido}")

ib_captura_accion = _IBCapturaWhatToShow()
bot.pedir_velas(ib_captura_accion, _ContratoFalso("AAPL"), "1 D", "5 mins")
check("pedir_velas: contrato de accion (sin secType CRYPTO) sigue pidiendo whatToShow='TRADES'",
      ib_captura_accion.what_to_show_recibido == "TRADES",
      f"whatToShow={ib_captura_accion.what_to_show_recibido}")

ib_captura_forex = _IBCapturaWhatToShow()
bot.pedir_velas(ib_captura_forex, _ContratoForexFalso("EUR.USD"), "1 D", "5 mins")
check("pedir_velas: contrato de forex (secType='CASH') pide whatToShow='MIDPOINT'",
      ib_captura_forex.what_to_show_recibido == "MIDPOINT",
      f"whatToShow={ib_captura_forex.what_to_show_recibido}")


# ---------------------------------------------------------------------------
# 7b. Cortacircuitos: tras muchos valores SEGUIDOS sin ningun dato (senal de
#     que TWS/IB Gateway perdio la conexion con los market data farms), deja
#     de reintentar 3 veces con espera de 15s por cada valor -> solo 1
#     intento rapido. En produccion esto evito que un ciclo se quedara horas
#     reintentando ticker a ticker con la conexion de datos caida.
# ---------------------------------------------------------------------------
bot._fallos_seguidos_datos = 0
bot._aviso_datos_caidos_emitido = False

# Simula UMBRAL_FALLOS_SEGUIDOS_DATOS valores seguidos sin ningun dato
for i in range(bot.UMBRAL_FALLOS_SEGUIDOS_DATOS):
    ib_fallo = _IBReintentos([[], [], []])
    bot.pedir_velas(ib_fallo, _ContratoFalso(f"FALLO{i}"), "1 D", "1 min")

check(f"cortacircuitos: tras {bot.UMBRAL_FALLOS_SEGUIDOS_DATOS} valores seguidos sin datos, se activa",
      bot._fallos_seguidos_datos >= bot.UMBRAL_FALLOS_SEGUIDOS_DATOS,
      f"_fallos_seguidos_datos={bot._fallos_seguidos_datos}")

# Con el cortacircuitos activo, el siguiente valor solo debe intentarlo UNA
# vez (no 3), para no perder 45s mas en un valor que probablemente tambien
# vaya a fallar por el mismo motivo de fondo.
ib_siguiente_fallo = _IBReintentos([[], [], []])
bot.pedir_velas(ib_siguiente_fallo, _ContratoFalso("SIGUIENTE"), "1 D", "1 min")
check("cortacircuitos activo: solo hace 1 intento (no 3) en el siguiente valor",
      ib_siguiente_fallo.llamadas == 1, f"llamadas={ib_siguiente_fallo.llamadas}")

# En cuanto un valor SI trae datos, el cortacircuitos se desactiva y vuelve
# a reintentar normalmente (3 intentos) en el siguiente que falle.
ib_recupera = _IBReintentos([[_Vela(100)]])
bot.pedir_velas(ib_recupera, _ContratoFalso("RECUPERA"), "1 D", "1 min")
check("cortacircuitos: se desactiva en cuanto un valor trae datos",
      bot._fallos_seguidos_datos == 0, f"_fallos_seguidos_datos={bot._fallos_seguidos_datos}")

ib_tras_recuperar = _IBReintentos([[], [], []])
bot.pedir_velas(ib_tras_recuperar, _ContratoFalso("TRAS_RECUPERAR"), "1 D", "1 min")
check("tras recuperarse, vuelve a hacer los 3 intentos normales",
      ib_tras_recuperar.llamadas == bot.INTENTOS_MAXIMOS, f"llamadas={ib_tras_recuperar.llamadas}")

# Se resetea para no afectar a los tests siguientes.
bot._fallos_seguidos_datos = 0
bot._aviso_datos_caidos_emitido = False


# ---------------------------------------------------------------------------
# 8. Robustez de revisar_ventas: una posicion que provoca un error
#    (coste_medio = 0 -> division por cero) NO debe impedir procesar las
#    demas posiciones ni lanzar una excepcion hacia fuera.
# ---------------------------------------------------------------------------
class _Contrato:
    def __init__(self, symbol, currency="USD"):
        self.symbol = symbol
        self.currency = currency


class _Posicion:
    def __init__(self, symbol, position, avgCost, currency="USD"):
        self.contract = _Contrato(symbol, currency)
        self.position = position
        self.avgCost = avgCost


class _IBFalsoVentas:
    """positions() devuelve una posicion con avgCost=0 (provocaria division
    por cero) seguida de una posicion normal en perdidas (no debe venderse,
    pero debe LLEGAR a procesarse: si el bug existiera, esta segunda
    posicion nunca se procesaria)."""

    def __init__(self):
        self.ordenes_colocadas = []
        self._posiciones = [
            _Posicion("BUGGY", 10, 0),          # avgCost=0 -> antes rompia el ciclo entero
            _Posicion("NORMAL", 5, 100),         # posicion normal, sin beneficio suficiente
        ]

    def reqPositions(self):
        pass

    def positions(self):
        return self._posiciones

    def sleep(self, segundos):
        pass

    def reqHistoricalData(self, contrato, **kwargs):
        # Precio actual = igual al coste medio -> sin beneficio, no debe vender
        return [_Vela(100)]

    def placeOrder(self, contrato, orden):
        self.ordenes_colocadas.append((contrato.symbol, orden))
        return types.SimpleNamespace(orderStatus=types.SimpleNamespace(status="Submitted"), isDone=lambda: True)


ib_falso_ventas = _IBFalsoVentas()
excepcion_lanzada = None
es_horario_original_ventas = bot.es_horario_operativo
bot.es_horario_operativo = lambda mercado: True  # forzar "mercado abierto" durante este test
try:
    bot.revisar_ventas(ib_falso_ventas)
except Exception as e:
    excepcion_lanzada = e
finally:
    bot.es_horario_operativo = es_horario_original_ventas

check("revisar_ventas: una posicion con avgCost=0 no lanza excepcion hacia fuera",
      excepcion_lanzada is None, f"excepcion={excepcion_lanzada}")
check("revisar_ventas: no coloca ninguna orden (ninguna posicion cumple venta)",
      ib_falso_ventas.ordenes_colocadas == [], f"ordenes={ib_falso_ventas.ordenes_colocadas}")


# ---------------------------------------------------------------------------
# 8b. revisar_ventas: si el mercado de una posicion esta CERRADO, no debe
#     intentar vender aunque el beneficio y el MACD digan que tocaria
#     vender (esto es exactamente el bug real detectado en produccion con
#     Hong Kong: se intentaba vender fuera de horario y IBKR cancelaba la
#     orden, pero el bot lo registraba como si hubiera ido bien).
# ---------------------------------------------------------------------------
class _IBFalsoVentasMercadoCerrado(_IBFalsoVentas):
    def __init__(self):
        self.ordenes_colocadas = []
        self._posiciones = [
            _Posicion("2259", 1400, 100, currency="HKD"),  # con MACD bajista y beneficio alto, "venderia"
        ]

    def reqHistoricalData(self, contrato, **kwargs):
        return [_Vela(120)]  # +20% de beneficio bruto: cumpliria de sobra el umbral de venta


ib_falso_cerrado = _IBFalsoVentasMercadoCerrado()
bot.es_horario_operativo = lambda mercado: False  # todos los mercados "cerrados"
try:
    bot.revisar_ventas(ib_falso_cerrado)
finally:
    bot.es_horario_operativo = es_horario_original_ventas

check("revisar_ventas: con el mercado CERRADO, no coloca ninguna orden aunque tocaria vender",
      ib_falso_cerrado.ordenes_colocadas == [], f"ordenes={ib_falso_cerrado.ordenes_colocadas}")


# ---------------------------------------------------------------------------
# 9. Robustez de revisar_compras: un valor cuya senal de COMPRA revienta al
#    calcular el precio (ej. fallo de red al pedir la cotizacion) NO debe
#    impedir que se analice y, si toca, se compre el SIGUIENTE valor de la
#    lista. Antes del arreglo, una excepcion aqui abortaba el resto del
#    escaneo completo (US+HK+KR) para ese ciclo.
# ---------------------------------------------------------------------------
bot._fallos_seguidos_datos = 0
bot._aviso_datos_caidos_emitido = False


class _IBFalsoCompras:
    def __init__(self):
        self.ordenes_colocadas = []

    def accountSummary(self):
        return [types.SimpleNamespace(tag='NetLiquidation', currency='USD', value='10000')]

    def reqPositions(self):
        pass

    def positions(self):
        return []

    def sleep(self, segundos):
        pass

    def qualifyContracts(self, contrato):
        contrato.conId = 999

    def reqHistoricalData(self, contrato, **kwargs):
        if contrato.symbol == "BAD":
            raise RuntimeError("fallo de red simulado al pedir precio")
        # BUENO: serie acelerando -> señal de COMPRA en analizar_activo,
        # y tambien sirve como precio actual (~ultimo valor de la serie)
        return [_Vela(100 * (1.02 ** i)) for i in range(60)]

    def reqContractDetails(self, contrato):
        return [types.SimpleNamespace(minSize=1, sizeIncrement=1)]

    def placeOrder(self, contrato, orden):
        if contrato.symbol == "CRASH":
            # Simula un fallo al colocar la orden (p.ej. corte de red justo
            # en ese instante), DESPUES de que ya se decidio comprar.
            raise RuntimeError("fallo de red simulado al colocar la orden")
        self.ordenes_colocadas.append(contrato.symbol)
        return types.SimpleNamespace(orderStatus=types.SimpleNamespace(status="Submitted"), isDone=lambda: True)


activos_prueba_compras = [
    {"ticker": "BAD",    "exchange": "SMART", "currency": "USD", "mercado": "US"},
    {"ticker": "CRASH",  "exchange": "SMART", "currency": "USD", "mercado": "US"},
    {"ticker": "GOOD",   "exchange": "SMART", "currency": "USD", "mercado": "US"},
]

activos_originales = bot.ACTIVOS
es_horario_original = bot.es_horario_operativo
en_ventana_sin_compra_original = bot.en_ventana_sin_compra
bot.ACTIVOS = activos_prueba_compras
bot.es_horario_operativo = lambda mercado: True  # forzar "mercado abierto" durante el test
# En_ventana_sin_compra depende de la hora REAL respecto al cierre real del
# mercado; se fuerza a False para que el test no dependa de a que hora del
# dia se ejecute (evita falsos negativos si por casualidad se corre dentro
# de los ultimos 90 min reales antes del cierre de US).
bot.en_ventana_sin_compra = lambda mercado: False

ib_falso_compras = _IBFalsoCompras()
excepcion_compras = None
try:
    bot.revisar_compras(ib_falso_compras)
except Exception as e:
    excepcion_compras = e
finally:
    bot.ACTIVOS = activos_originales
    bot.es_horario_operativo = es_horario_original
    bot.en_ventana_sin_compra = en_ventana_sin_compra_original

check("revisar_compras: un valor que falla al pedir precio no lanza excepcion hacia fuera",
      excepcion_compras is None, f"excepcion={excepcion_compras}")
check("revisar_compras: un fallo al COLOCAR LA ORDEN de un valor no aborta el resto del escaneo",
      "CRASH" not in ib_falso_compras.ordenes_colocadas,
      f"ordenes_colocadas={ib_falso_compras.ordenes_colocadas}")
check("revisar_compras: el valor que viene DESPUES del que falla si se llega a comprar",
      "GOOD" in ib_falso_compras.ordenes_colocadas,
      f"ordenes_colocadas={ib_falso_compras.ordenes_colocadas}")


# ---------------------------------------------------------------------------
# 10. Ordenes fraccionarias: deben usar cashQty (importe en efectivo) y
#     totalQuantity=0, NUNCA una cantidad de acciones fraccionaria directa.
#     Bug real en produccion: IBKR rechazaba (error 10243) toda orden con
#     totalQuantity fraccionario enviada por API; cashQty es la unica forma
#     valida de comprar/vender una fraccion de accion via API.
# ---------------------------------------------------------------------------
orden_compra_frac = bot.crear_orden_mercado_cash('BUY', 1234.56)
check("crear_orden_mercado_cash: totalQuantity=0 (nunca fraccionario)",
      orden_compra_frac.totalQuantity == 0, f"totalQuantity={orden_compra_frac.totalQuantity}")
check("crear_orden_mercado_cash: cashQty = importe solicitado",
      abs(orden_compra_frac.cashQty - 1234.56) < 1e-9, f"cashQty={orden_compra_frac.cashQty}")

orden_venta_frac = bot.crear_orden_limitada_cash('SELL', 987.65, 100.0)
check("crear_orden_limitada_cash: totalQuantity=0 (nunca fraccionario)",
      orden_venta_frac.totalQuantity == 0, f"totalQuantity={orden_venta_frac.totalQuantity}")
check("crear_orden_limitada_cash: cashQty = importe solicitado",
      abs(orden_venta_frac.cashQty - 987.65) < 1e-9, f"cashQty={orden_venta_frac.cashQty}")

check("es_cantidad_fraccionaria: 3.544 es fraccionaria", bot.es_cantidad_fraccionaria(3.544) is True)
check("es_cantidad_fraccionaria: 5 (entero) no es fraccionaria", bot.es_cantidad_fraccionaria(5) is False)
check("es_cantidad_fraccionaria: 5.0 (float entero) no es fraccionaria", bot.es_cantidad_fraccionaria(5.0) is False)


# ---------------------------------------------------------------------------
# 10b. revisar_compras (mercado fraccionable): la orden colocada debe llevar
#     cashQty, no una cantidad de acciones fraccionaria en totalQuantity.
# ---------------------------------------------------------------------------
class _IBFalsoComprasFraccionarias(_IBFalsoCompras):
    def __init__(self):
        super().__init__()
        self.ordenes_objeto = []

    def placeOrder(self, contrato, orden):
        self.ordenes_objeto.append(orden)
        return super().placeOrder(contrato, orden)


bot.ACTIVOS = [{"ticker": "GOOD", "exchange": "SMART", "currency": "USD", "mercado": "US"}]
bot.es_horario_operativo = lambda mercado: True
bot.en_ventana_sin_compra = lambda mercado: False
ib_falso_frac = _IBFalsoComprasFraccionarias()
try:
    # Anclado a sesion regular (10:00 ET): fuera de sesion regular las
    # fracciones se saltan por completo (fracciones_no_disponibles), lo que
    # rompe este test si se ejecuta con el reloj real en pre/postmercado.
    con_reloj_fijo(miercoles_us_abierto, bot.revisar_compras, ib_falso_frac)
finally:
    bot.ACTIVOS = activos_originales
    bot.es_horario_operativo = es_horario_original
    bot.en_ventana_sin_compra = en_ventana_sin_compra_original

check("revisar_compras: la orden real colocada en US usa cashQty, no totalQuantity fraccionario",
      len(ib_falso_frac.ordenes_objeto) == 1
      and ib_falso_frac.ordenes_objeto[0].totalQuantity == 0
      and ib_falso_frac.ordenes_objeto[0].cashQty > 0,
      f"ordenes={[(o.totalQuantity, o.cashQty) for o in ib_falso_frac.ordenes_objeto]}")


# ---------------------------------------------------------------------------
# 11. Historial persistente de fecha de compra inicial, y verificacion de
#     posicion real tras una orden que no confirma 'Filled'. Se usa un
#     archivo temporal para no tocar (ni depender de) el historial real del
#     usuario en su maquina.
# ---------------------------------------------------------------------------
import tempfile

archivo_historial_original = bot.ARCHIVO_HISTORIAL_COMPRAS
directorio_temp = tempfile.mkdtemp()
bot.ARCHIVO_HISTORIAL_COMPRAS = os.path.join(directorio_temp, "historial_compras_test.json")

try:
    # cargar_historial_compras sobre un archivo que no existe -> diccionario vacio
    check("cargar_historial_compras: archivo inexistente -> {}", bot.cargar_historial_compras() == {})

    # registrar + recuperar
    check("obtener_apertura_registrada: sin registro previo -> None",
          bot.obtener_apertura_registrada("US", "ZZZ") is None)
    bot.registrar_apertura_de_posicion("US", "ZZZ")
    apertura = bot.obtener_apertura_registrada("US", "ZZZ")
    check("registrar_apertura_de_posicion + obtener_apertura_registrada: recupera un datetime valido",
          apertura is not None and (datetime.now() - apertura).total_seconds() < 60,
          f"apertura={apertura}")

    # una compra adicional (promediar a la baja) NO debe machacar la fecha original
    fecha_original = apertura
    import time as _time
    _time.sleep(0.01)
    # Simulamos que revisar_compras solo registra si cantidad_antes_compra <= 1e-6;
    # aqui comprobamos directamente la funcion de bajo nivel: si NO se vuelve a
    # llamar a registrar_apertura_de_posicion (porque ya habia posicion), la
    # fecha debe seguir siendo la misma.
    apertura_tras_no_tocar = bot.obtener_apertura_registrada("US", "ZZZ")
    check("obtener_apertura_registrada: la fecha no cambia si no se vuelve a registrar",
          apertura_tras_no_tocar == fecha_original)

    # obtener_cantidad_posicion_real / verificar_posicion_tras_orden_no_confirmada
    class _IBFalsoPosicionReal:
        def __init__(self, cantidad_tras_consulta):
            self.cantidad_tras_consulta = cantidad_tras_consulta

        def reqPositions(self):
            pass

        def positions(self):
            if self.cantidad_tras_consulta <= 0:
                return []
            return [_Posicion("ZZZ", self.cantidad_tras_consulta, 100, currency="USD")]

        def sleep(self, segundos):
            pass

    contrato_zzz = _ContratoFalso("ZZZ")
    contrato_zzz.currency = "USD"

    # Caso: la posicion SI cambio de verdad (p.ej. compra que en realidad se
    # ejecuto pese al estado no confirmado)
    ib_pos_cambio = _IBFalsoPosicionReal(cantidad_tras_consulta=10)
    cantidad_verificada = bot.verificar_posicion_tras_orden_no_confirmada(
        ib_pos_cambio, contrato_zzz, cantidad_antes=0, prefijo_log="TEST")
    check("verificar_posicion_tras_orden_no_confirmada: detecta que la posicion SI cambio",
          cantidad_verificada == 10, f"cantidad_verificada={cantidad_verificada}")

    # Caso: la posicion NO cambio (la orden de verdad no se ejecuto)
    ib_pos_sin_cambio = _IBFalsoPosicionReal(cantidad_tras_consulta=0)
    cantidad_verificada_2 = bot.verificar_posicion_tras_orden_no_confirmada(
        ib_pos_sin_cambio, contrato_zzz, cantidad_antes=0, prefijo_log="TEST")
    check("verificar_posicion_tras_orden_no_confirmada: detecta que la posicion NO cambio",
          cantidad_verificada_2 == 0, f"cantidad_verificada_2={cantidad_verificada_2}")

    # --- Extremo a extremo: revisar_compras registra la apertura cuando la
    #     compra se confirma Filled y era una posicion nueva desde cero ---
    class _IBFalsoCompraFilled:
        def accountSummary(self):
            return [types.SimpleNamespace(tag='NetLiquidation', currency='USD', value='10000')]

        def reqPositions(self):
            pass

        def positions(self):
            return []

        def sleep(self, segundos):
            pass

        def qualifyContracts(self, contrato):
            contrato.conId = 999

        def reqHistoricalData(self, contrato, **kwargs):
            return [_Vela(100 * (1.02 ** i)) for i in range(60)]

        def reqContractDetails(self, contrato):
            return [types.SimpleNamespace(minSize=1, sizeIncrement=1)]

        def placeOrder(self, contrato, orden):
            return types.SimpleNamespace(orderStatus=types.SimpleNamespace(status="Filled"), isDone=lambda: True)

    bot.ACTIVOS = [{"ticker": "NUEVA", "exchange": "SMART", "currency": "USD", "mercado": "US"}]
    bot.es_horario_operativo = lambda mercado: True
    bot.en_ventana_sin_compra = lambda mercado: False
    check("obtener_apertura_registrada: NUEVA sin registro previo -> None",
          bot.obtener_apertura_registrada("US", "NUEVA") is None)
    try:
        bot.revisar_compras(_IBFalsoCompraFilled())
    finally:
        bot.ACTIVOS = activos_originales
        bot.es_horario_operativo = es_horario_original
        bot.en_ventana_sin_compra = en_ventana_sin_compra_original

    check("revisar_compras: una compra Filled de una posicion nueva SI registra la apertura",
          bot.obtener_apertura_registrada("US", "NUEVA") is not None)
finally:
    bot.ARCHIVO_HISTORIAL_COMPRAS = archivo_historial_original


# ---------------------------------------------------------------------------
# 12. obtener_modo_cuenta: detecta cuenta DEMO (prefijo 'DU', convencion de
#     IBKR) vs REAL vs mixta vs desconocida.
# ---------------------------------------------------------------------------
class _IBFalsoCuentas:
    def __init__(self, cuentas):
        self.cuentas = cuentas

    def managedAccounts(self):
        return self.cuentas


es_demo, texto = bot.obtener_modo_cuenta(_IBFalsoCuentas(["DU1234567"]))
check("obtener_modo_cuenta: cuenta DU... -> demo (True)", es_demo is True, f"texto={texto}")

es_demo, texto = bot.obtener_modo_cuenta(_IBFalsoCuentas(["U1234567"]))
check("obtener_modo_cuenta: cuenta U... (sin DU) -> real (False)", es_demo is False, f"texto={texto}")

es_demo, texto = bot.obtener_modo_cuenta(_IBFalsoCuentas(["DU1111111", "U2222222"]))
check("obtener_modo_cuenta: mezcla de demo y real -> None (ambiguo)", es_demo is None, f"texto={texto}")

es_demo, texto = bot.obtener_modo_cuenta(_IBFalsoCuentas([]))
check("obtener_modo_cuenta: sin cuentas -> None (desconocido)", es_demo is None, f"texto={texto}")


# ---------------------------------------------------------------------------
# 13. orden_rechazada_por_codigo + vigilante de congelacion (solo las
#     piezas que se pueden probar sin arrancar el hilo real, que llamaria a
#     os._exit y mataria el propio proceso de tests).
# ---------------------------------------------------------------------------
class _TradeLogFalso:
    def __init__(self, errorCode):
        self.errorCode = errorCode


class _TradeFalso:
    def __init__(self, codigos_error):
        self.log = [_TradeLogFalso(c) for c in codigos_error]


check("orden_rechazada_por_codigo: detecta el codigo 10244 presente",
      bot.orden_rechazada_por_codigo(_TradeFalso([10349, 10244]), {10244}) is True)
check("orden_rechazada_por_codigo: False si el codigo no esta",
      bot.orden_rechazada_por_codigo(_TradeFalso([10349]), {10244}) is False)
check("orden_rechazada_por_codigo: False con trade.log vacio",
      bot.orden_rechazada_por_codigo(_TradeFalso([]), {10244}) is False)

# actualizar_latido / log() mantienen viva la señal que vigila el hilo de
# congelacion (sin arrancar el hilo en si). ARCHIVO_LATIDO/ARCHIVO_PID ya
# estan redirigidos a una carpeta temporal desde el principio del archivo,
# para no dejar "latido_bot.txt" ni "bot.pid" tirados en el repo.
bot._ultimo_latido = 0.0
bot._ultimo_latido_archivo = 0.0
bot.actualizar_latido()
check("actualizar_latido: refresca _ultimo_latido a un valor reciente",
      bot._ultimo_latido > 0.0)
check("actualizar_latido: escribe el archivo de latido en disco",
      os.path.isfile(bot.ARCHIVO_LATIDO))

bot._ultimo_latido = 0.0
bot.log("mensaje de prueba, no deberia aparecer como fallo")
check("log(): tambien refresca _ultimo_latido (cada log es una señal de vida)",
      bot._ultimo_latido > 0.0)

# escribir_pid: guarda el PID del proceso, para que el vigilante externo
# (vigilante_externo.ps1) sepa que proceso matar si detecta congelacion.
bot.escribir_pid()
check("escribir_pid: escribe el PID del proceso actual en el archivo",
      os.path.isfile(bot.ARCHIVO_PID) and open(bot.ARCHIVO_PID).read().strip() == str(os.getpid()))


# ---------------------------------------------------------------------------
# 14. Plan B ante el error 10244 (valor sin fracciones habilitadas via API):
#     debe reintentar con acciones ENTERAS en vez de rendirse.
# ---------------------------------------------------------------------------
class _IBFalsoRechazoCashQty(_IBFalsoCompras):
    """cashQty (Plan A) se rechaza con 10244, pero la cantidad fraccionaria
    puesta DIRECTAMENTE (Plan B) SI se ejecuta -caso confirmado a mano en
    cuenta real (compra de PFE): la cuenta admite fracciones de verdad, solo
    rechaza el mecanismo cashQty."""

    def __init__(self):
        super().__init__()
        self.ordenes_objeto = []

    def placeOrder(self, contrato, orden):
        self.ordenes_objeto.append(orden)
        if orden.totalQuantity == 0:
            trade = types.SimpleNamespace(
                orderStatus=types.SimpleNamespace(status="Cancelled"),
                isDone=lambda: True,
                log=[_TradeLogFalso(10244)],
            )
            return trade
        return types.SimpleNamespace(orderStatus=types.SimpleNamespace(status="Filled"),
                                      isDone=lambda: True, log=[])


bot.ACTIVOS = [{"ticker": "SINFRACCION", "exchange": "SMART", "currency": "USD", "mercado": "US"}]
bot.es_horario_operativo = lambda mercado: True
bot.en_ventana_sin_compra = lambda mercado: False
ib_falso_cashqty = _IBFalsoRechazoCashQty()
try:
    con_reloj_fijo(miercoles_us_abierto, bot.revisar_compras, ib_falso_cashqty)
finally:
    bot.ACTIVOS = activos_originales
    bot.es_horario_operativo = es_horario_original
    bot.en_ventana_sin_compra = en_ventana_sin_compra_original

check("Plan B error 10244: se intentaron 2 ordenes (cashQty rechazada + cantidad fraccionaria directa)",
      len(ib_falso_cashqty.ordenes_objeto) == 2,
      f"ordenes={[(o.totalQuantity, getattr(o, 'cashQty', None)) for o in ib_falso_cashqty.ordenes_objeto]}")
check("Plan B error 10244: la primera orden fue por cashQty (totalQuantity=0)",
      len(ib_falso_cashqty.ordenes_objeto) == 2 and ib_falso_cashqty.ordenes_objeto[0].totalQuantity == 0)
check("Plan B error 10244: la segunda orden fue la cantidad fraccionaria ORIGINAL puesta directamente "
      "(no redondeada a entero)",
      len(ib_falso_cashqty.ordenes_objeto) == 2
      and bot.es_cantidad_fraccionaria(ib_falso_cashqty.ordenes_objeto[1].totalQuantity),
      f"totalQuantity={ib_falso_cashqty.ordenes_objeto[1].totalQuantity if len(ib_falso_cashqty.ordenes_objeto) == 2 else 'N/A'}")


class _IBFalsoRechazoTotal(_IBFalsoCompras):
    """Ni cashQty (Plan A) ni la cantidad fraccionaria directa (Plan B)
    funcionan (10244 y 10243 respectivamente); solo el fallback final a
    acciones ENTERAS (Plan C) se ejecuta."""

    def __init__(self):
        super().__init__()
        self.ordenes_objeto = []

    def placeOrder(self, contrato, orden):
        self.ordenes_objeto.append(orden)
        if orden.totalQuantity == 0:
            return types.SimpleNamespace(
                orderStatus=types.SimpleNamespace(status="Cancelled"),
                isDone=lambda: True,
                log=[_TradeLogFalso(10244)],
            )
        if bot.es_cantidad_fraccionaria(orden.totalQuantity):
            return types.SimpleNamespace(
                orderStatus=types.SimpleNamespace(status="Cancelled"),
                isDone=lambda: True,
                log=[_TradeLogFalso(10243)],
            )
        # La orden de acciones ENTERAS (Plan C, el ultimo recurso) SI se ejecuta
        return types.SimpleNamespace(orderStatus=types.SimpleNamespace(status="Filled"),
                                      isDone=lambda: True, log=[])


bot.ACTIVOS = [{"ticker": "SINFRACCION", "exchange": "SMART", "currency": "USD", "mercado": "US"}]
bot.es_horario_operativo = lambda mercado: True
bot.en_ventana_sin_compra = lambda mercado: False
ib_falso_total = _IBFalsoRechazoTotal()
try:
    con_reloj_fijo(miercoles_us_abierto, bot.revisar_compras, ib_falso_total)
finally:
    bot.ACTIVOS = activos_originales
    bot.es_horario_operativo = es_horario_original
    bot.en_ventana_sin_compra = en_ventana_sin_compra_original

check("Plan C error 10243+10244: se intentaron las 3 ordenes (cashQty, fraccion directa, acciones enteras)",
      len(ib_falso_total.ordenes_objeto) == 3,
      f"ordenes={[(o.totalQuantity, getattr(o, 'cashQty', None)) for o in ib_falso_total.ordenes_objeto]}")
check("Plan C error 10243+10244: la ultima orden (fallback final) fue por acciones ENTERAS",
      len(ib_falso_total.ordenes_objeto) == 3
      and ib_falso_total.ordenes_objeto[2].totalQuantity >= 1
      and not bot.es_cantidad_fraccionaria(ib_falso_total.ordenes_objeto[2].totalQuantity),
      f"totalQuantity={ib_falso_total.ordenes_objeto[2].totalQuantity if len(ib_falso_total.ordenes_objeto) == 3 else 'N/A'}")


# ---------------------------------------------------------------------------
# 15. generar_resumen_cierre_mercado: caso real de produccion visto con QCOM
#     -> el historial persistente guardaba la apertura de una compra
#     POSTERIOR a la venta que se estaba resumiendo (el valor se volvio a
#     comprar el mismo dia despues de cerrar esta ronda), asi que la tabla
#     mostraba "Abierta desde" con una hora DESPUES de "Cerrada a las". Debe
#     preferir la compra calculada de reqExecutions() (anterior a la venta)
#     en vez de la fecha registrada. Tambien se comprueba que aparecen los
#     totales en USD/EUR pedidos.
# ---------------------------------------------------------------------------
import contextlib
import io


class _EjecucionFalsa:
    def __init__(self, side, time, shares, avgPrice, symbol, currency="USD"):
        self.contract = _Contrato(symbol, currency)
        self.execution = types.SimpleNamespace(side=side, time=time, shares=shares, avgPrice=avgPrice)


class _IBFalsoResumen:
    def __init__(self, ejecuciones):
        self._ejecuciones = ejecuciones

    def positions(self):
        return []

    def reqExecutions(self, filtro):
        return self._ejecuciones


_hoy_resumen = datetime.now()
_compra_1 = _hoy_resumen.replace(hour=13, minute=0, second=0, microsecond=0)
_venta = _hoy_resumen.replace(hour=15, minute=0, second=0, microsecond=0)
_compra_2_posterior = _hoy_resumen.replace(hour=17, minute=0, second=0, microsecond=0)

ejecuciones_qcom = [
    _EjecucionFalsa("BOT", _compra_1, 10, 100.0, "QCOM"),
    _EjecucionFalsa("SLD", _venta, 10, 110.0, "QCOM"),
    _EjecucionFalsa("BOT", _compra_2_posterior, 5, 120.0, "QCOM"),  # ronda NUEVA, tras la venta
]

archivo_historial_original_resumen = bot.ARCHIVO_HISTORIAL_COMPRAS
directorio_temp_resumen = tempfile.mkdtemp()
bot.ARCHIVO_HISTORIAL_COMPRAS = os.path.join(directorio_temp_resumen, "historial_resumen_test.json")
# El historial registra la apertura de la ronda ACTUAL (la compra de las
# 17:00), que es posterior a la venta de las 15:00 que se esta resumiendo.
bot.guardar_historial_compras({bot.clave_historial("US", "QCOM"): _compra_2_posterior.isoformat(timespec="seconds")})

salida_resumen = io.StringIO()
try:
    with contextlib.redirect_stdout(salida_resumen):
        bot.generar_resumen_cierre_mercado(_IBFalsoResumen(ejecuciones_qcom), "US")
finally:
    bot.ARCHIVO_HISTORIAL_COMPRAS = archivo_historial_original_resumen

texto_resumen = salida_resumen.getvalue()
linea_qcom = next((l for l in texto_resumen.splitlines() if l.strip().startswith("QCOM") or "QCOM" in l), "")

check("resumen cierre: la fila de QCOM usa la compra de las 13:00 (previa a la venta), no la de las 17:00",
      "13:00" in linea_qcom and "17:00" not in linea_qcom,
      f"linea={linea_qcom!r}")
check("resumen cierre: la fila de QCOM muestra la venta de las 15:00",
      "15:00" in linea_qcom, f"linea={linea_qcom!r}")
check("resumen cierre: aparece el total de ganancia del dia en USD y EUR",
      "TOTAL ganancia hoy (US)" in texto_resumen and "USD" in texto_resumen and "EUR" in texto_resumen,
      f"salida={texto_resumen!r}")
check("resumen cierre: el total invertido de posiciones abiertas sigue mostrando USD y EUR (sin posiciones abiertas aqui, no debe fallar)",
      "ERROR" not in texto_resumen and "Traceback" not in texto_resumen)


# ---------------------------------------------------------------------------
# 15b. generar_resumen_cierre_mercado: el resumen diario por Telegram SOLO
#      se manda para CRYPTO (US/HK/KR ya se consultan a demanda desde
#      telegram_bot_ibkr.py con /cartera, /hoy, etc.)
# ---------------------------------------------------------------------------
class _ContratoCriptoParaResumen:
    def __init__(self, symbol, currency="USD"):
        self.symbol = symbol
        self.currency = currency
        self.secType = "CRYPTO"


class _EjecucionFalsaCripto:
    def __init__(self, side, time, shares, avgPrice, symbol):
        self.contract = _ContratoCriptoParaResumen(symbol)
        self.execution = types.SimpleNamespace(side=side, time=time, shares=shares, avgPrice=avgPrice)


mensajes_telegram_resumen = []
notificar_telegram_original = bot.notificar_telegram
bot.notificar_telegram = lambda mensaje: mensajes_telegram_resumen.append(mensaje)

try:
    # Mercado US: mismas ejecuciones de QCOM de antes, pero NO debe disparar
    # ningun mensaje de Telegram.
    with contextlib.redirect_stdout(io.StringIO()):
        bot.generar_resumen_cierre_mercado(_IBFalsoResumen(ejecuciones_qcom), "US")
    check("resumen cierre US: NO manda nada a Telegram",
          len(mensajes_telegram_resumen) == 0, f"mensajes={mensajes_telegram_resumen}")

    # Mercado CRYPTO: SI debe disparar un mensaje de Telegram con el resumen.
    _venta_btc = _hoy_resumen.replace(hour=15, minute=0, second=0, microsecond=0)
    _compra_btc = _hoy_resumen.replace(hour=13, minute=0, second=0, microsecond=0)
    ejecuciones_btc = [
        _EjecucionFalsaCripto("BOT", _compra_btc, 0.001, 50000.0, "BTC"),
        _EjecucionFalsaCripto("SLD", _venta_btc, 0.001, 51000.0, "BTC"),
    ]
    with contextlib.redirect_stdout(io.StringIO()):
        bot.generar_resumen_cierre_mercado(_IBFalsoResumen(ejecuciones_btc), "CRYPTO")
    check("resumen cierre CRYPTO: SI manda un mensaje a Telegram",
          len(mensajes_telegram_resumen) == 1, f"mensajes={mensajes_telegram_resumen}")
    if mensajes_telegram_resumen:
        mensaje_cripto = mensajes_telegram_resumen[0]
        check("resumen cripto por Telegram: incluye el titulo del resumen diario",
              "RESUMEN DIARIO CRYPTO" in mensaje_cripto, f"mensaje={mensaje_cripto!r}")
        check("resumen cripto por Telegram: incluye el ticker BTC",
              "BTC" in mensaje_cripto, f"mensaje={mensaje_cripto!r}")
        check("resumen cripto por Telegram: incluye la ganancia total del dia",
              "Ganancia total hoy" in mensaje_cripto, f"mensaje={mensaje_cripto!r}")
finally:
    bot.notificar_telegram = notificar_telegram_original


# ---------------------------------------------------------------------------
# 16. Pre/postmercado de US: en AMBOS tramos las ordenes ejecutadas deben
#     ser LIMITADAS al precio exacto (con outsideRth), no a mercado. En
#     premercado se compra Y se vende con normalidad; en postmercado SOLO
#     se vende (no se compra) -decision explicita del usuario-.
# ---------------------------------------------------------------------------
class _IBFalsoComprasHorario(_IBFalsoCompras):
    def __init__(self):
        super().__init__()
        self.ordenes_objeto = []

    def placeOrder(self, contrato, orden):
        self.ordenes_objeto.append(orden)
        return types.SimpleNamespace(orderStatus=types.SimpleNamespace(status="Filled"),
                                      isDone=lambda: True, log=[])


bot.ACTIVOS = [{"ticker": "PREMKT", "exchange": "SMART", "currency": "USD", "mercado": "US"}]
bot.es_horario_operativo = lambda mercado: True
bot.en_ventana_sin_compra = lambda mercado: False
ib_falso_premercado = _IBFalsoComprasHorario()
premercado_instante = datetime(2026, 8, 12, 6, 0, tzinfo=bot.ZONA_NY)  # miercoles 6:00 ET
try:
    con_reloj_fijo(premercado_instante, bot.revisar_compras, ib_falso_premercado)
finally:
    bot.ACTIVOS = activos_originales
    bot.es_horario_operativo = es_horario_original
    bot.en_ventana_sin_compra = en_ventana_sin_compra_original

check("revisar_compras en premercado US: coloca exactamente UNA orden (Plan A/B saltados, "
      "va directo a acciones enteras: las fracciones no funcionan fuera de sesion regular)",
      len(ib_falso_premercado.ordenes_objeto) == 1,
      f"ordenes={len(ib_falso_premercado.ordenes_objeto)}")
if ib_falso_premercado.ordenes_objeto:
    orden_premercado = ib_falso_premercado.ordenes_objeto[0]
    check("revisar_compras en premercado US: la orden es LIMITADA (no a mercado)",
          orden_premercado.orderType == "LMT", f"orderType={orden_premercado.orderType}")
    check("revisar_compras en premercado US: la orden tiene outsideRth activado",
          orden_premercado.outsideRth is True)
    check("revisar_compras en premercado US: la orden es de acciones ENTERAS, no cashQty "
          "(fracciones_no_disponibles: se salta directo a Plan C)",
          not bot.es_cantidad_fraccionaria(orden_premercado.totalQuantity)
          and orden_premercado.totalQuantity >= 1,
          f"totalQuantity={orden_premercado.totalQuantity}")

# Postmercado: NO debe intentar comprar aunque haya señal de compra.
ib_falso_postmercado_compras = _IBFalsoComprasHorario()
postmercado_instante = datetime(2026, 8, 12, 18, 0, tzinfo=bot.ZONA_NY)  # miercoles 18:00 ET
bot.ACTIVOS = [{"ticker": "POSTMKT_COMPRA", "exchange": "SMART", "currency": "USD", "mercado": "US"}]
bot.es_horario_operativo = lambda mercado: True
bot.en_ventana_sin_compra = lambda mercado: False
try:
    con_reloj_fijo(postmercado_instante, bot.revisar_compras, ib_falso_postmercado_compras)
finally:
    bot.ACTIVOS = activos_originales
    bot.es_horario_operativo = es_horario_original
    bot.en_ventana_sin_compra = en_ventana_sin_compra_original

check("revisar_compras en postmercado US: NO coloca ninguna orden aunque hay señal de compra",
      ib_falso_postmercado_compras.ordenes_objeto == [],
      f"ordenes={ib_falso_postmercado_compras.ordenes_objeto}")

# Ventas: en pre Y postmercado debe vender igual, ambas con orden LIMITADA
# al precio exacto (con outsideRth), no a mercado.
class _Contrato2:
    def __init__(self, symbol, currency="USD"):
        self.symbol = symbol
        self.currency = currency


class _Posicion2:
    def __init__(self, symbol, position, avgCost, currency="USD"):
        self.contract = _Contrato2(symbol, currency)
        self.position = position
        self.avgCost = avgCost


class _IBFalsoVentasHorario:
    def __init__(self):
        self.ordenes_colocadas = []
        self._posiciones = [_Posicion2("POSTMKT", 10, 100)]

    def reqPositions(self):
        pass

    def positions(self):
        return self._posiciones

    def sleep(self, segundos):
        pass

    def reqHistoricalData(self, contrato, **kwargs):
        return [_Vela(120)]  # +20% de beneficio, de sobra para vender

    def placeOrder(self, contrato, orden):
        self.ordenes_colocadas.append(orden)
        return types.SimpleNamespace(orderStatus=types.SimpleNamespace(status="Filled"),
                                      isDone=lambda: True, log=[])


macd_bajista_original = bot.macd_5min_bajista
bot.macd_5min_bajista = lambda ib, contrato: True  # forzar señal de venta

ib_falso_venta_postmercado = _IBFalsoVentasHorario()
try:
    con_reloj_fijo(postmercado_instante, bot.revisar_ventas, ib_falso_venta_postmercado)
finally:
    bot.macd_5min_bajista = macd_bajista_original

check("revisar_ventas en postmercado US: coloca exactamente una orden (SI se puede vender)",
      len(ib_falso_venta_postmercado.ordenes_colocadas) == 1,
      f"ordenes={ib_falso_venta_postmercado.ordenes_colocadas}")
if ib_falso_venta_postmercado.ordenes_colocadas:
    orden_venta_postmercado = ib_falso_venta_postmercado.ordenes_colocadas[0]
    check("revisar_ventas en postmercado US: la orden es LIMITADA (no a mercado)",
          orden_venta_postmercado.orderType == "LMT", f"orderType={orden_venta_postmercado.orderType}")
    check("revisar_ventas en postmercado US: la orden tiene outsideRth activado",
          orden_venta_postmercado.outsideRth is True)

# Premercado: SI debe vender tambien, con orden LIMITADA al precio exacto
# (con outsideRth), no a mercado.
ib_falso_venta_premercado = _IBFalsoVentasHorario()
ib_falso_venta_premercado._posiciones = [_Posicion2("PREVENTA", 10, 100)]
bot.macd_5min_bajista = lambda ib, contrato: True
try:
    con_reloj_fijo(premercado_instante, bot.revisar_ventas, ib_falso_venta_premercado)
finally:
    bot.macd_5min_bajista = macd_bajista_original

check("revisar_ventas en premercado US: coloca exactamente una orden",
      len(ib_falso_venta_premercado.ordenes_colocadas) == 1,
      f"ordenes={ib_falso_venta_premercado.ordenes_colocadas}")
if ib_falso_venta_premercado.ordenes_colocadas:
    orden_venta_premercado = ib_falso_venta_premercado.ordenes_colocadas[0]
    check("revisar_ventas en premercado US: la orden es LIMITADA (no a mercado)",
          orden_venta_premercado.orderType == "LMT", f"orderType={orden_venta_premercado.orderType}")
    check("revisar_ventas en premercado US: la orden tiene outsideRth activado",
          orden_venta_premercado.outsideRth is True)


# ---------------------------------------------------------------------------
# 9. Criptomonedas (PAXOS/ZEROHASH, via IBKR)
# ---------------------------------------------------------------------------

# --- 9a. es_horario_operativo_cripto / es_horario_operativo("CRYPTO") ---
crypto_24_7_original = bot.CRYPTO_24_7

bot.CRYPTO_24_7 = True
check("es_horario_operativo CRYPTO con CRYPTO_24_7=True: sabado -> abierto (24/7)",
      con_reloj_fijo(sabado_us, bot.es_horario_operativo, "CRYPTO") is True)

bot.CRYPTO_24_7 = False
viernes_15_59 = datetime(2026, 8, 14, 15, 59, tzinfo=bot.ZONA_NY)  # viernes, justo antes del cierre
check("es_horario_operativo CRYPTO (Basic): viernes 15:59 ET -> abierto",
      con_reloj_fijo(viernes_15_59, bot.es_horario_operativo, "CRYPTO") is True)

viernes_16_00 = datetime(2026, 8, 14, 16, 0, tzinfo=bot.ZONA_NY)  # viernes, justo el cierre
check("es_horario_operativo CRYPTO (Basic): viernes 16:00 ET -> cerrado",
      con_reloj_fijo(viernes_16_00, bot.es_horario_operativo, "CRYPTO") is False)

sabado_cripto = datetime(2026, 8, 15, 12, 0, tzinfo=bot.ZONA_NY)  # sabado a mediodia
check("es_horario_operativo CRYPTO (Basic): sabado -> cerrado",
      con_reloj_fijo(sabado_cripto, bot.es_horario_operativo, "CRYPTO") is False)

domingo_2_59 = datetime(2026, 8, 16, 2, 59, tzinfo=bot.ZONA_NY)  # domingo, justo antes de abrir
check("es_horario_operativo CRYPTO (Basic): domingo 2:59 ET -> cerrado",
      con_reloj_fijo(domingo_2_59, bot.es_horario_operativo, "CRYPTO") is False)

domingo_3_00 = datetime(2026, 8, 16, 3, 0, tzinfo=bot.ZONA_NY)  # domingo, justo la apertura
check("es_horario_operativo CRYPTO (Basic): domingo 3:00 ET -> abierto",
      con_reloj_fijo(domingo_3_00, bot.es_horario_operativo, "CRYPTO") is True)

lunes_cripto = datetime(2026, 8, 17, 10, 0, tzinfo=bot.ZONA_NY)  # lunes normal
check("es_horario_operativo CRYPTO (Basic): lunes 10:00 ET -> abierto",
      con_reloj_fijo(lunes_cripto, bot.es_horario_operativo, "CRYPTO") is True)

bot.CRYPTO_24_7 = crypto_24_7_original


# --- 9a-bis. justo_hora_resumen_cripto: dispara en una ventana fija diaria
#     (cripto no tiene cierre real, a diferencia de justo_cerro_mercado) ---
justo_las_23_55 = datetime(2026, 8, 12, 23, 55, tzinfo=bot.ZONA_NY)
check("justo_hora_resumen_cripto: justo a la hora fijada -> True",
      con_reloj_fijo(justo_las_23_55, bot.justo_hora_resumen_cripto) is True)

dos_horas_despues = datetime(2026, 8, 13, 1, 55, tzinfo=bot.ZONA_NY)
check("justo_hora_resumen_cripto: 2h despues, dentro del margen de 4h -> True",
      con_reloj_fijo(dos_horas_despues, bot.justo_hora_resumen_cripto) is True)

cinco_horas_despues = datetime(2026, 8, 13, 4, 55, tzinfo=bot.ZONA_NY)
check("justo_hora_resumen_cripto: 5h despues, fuera del margen de 4h -> False",
      con_reloj_fijo(cinco_horas_despues, bot.justo_hora_resumen_cripto) is False)

antes_de_hora = datetime(2026, 8, 12, 23, 0, tzinfo=bot.ZONA_NY)
check("justo_hora_resumen_cripto: antes de la hora fijada -> False",
      con_reloj_fijo(antes_de_hora, bot.justo_hora_resumen_cripto) is False)


# --- 9b. crear_contrato: activo CRYPTO -> objeto Crypto, no Stock ---
activo_btc = {"ticker": "BTC", "exchange": bot.EXCHANGE_CRYPTO, "currency": "USD", "mercado": "CRYPTO"}


class _IBSinPosicionesCripto:
    def positions(self):
        return []


bot._exchange_cripto_cache = None  # sin posiciones previas -> usa activo["exchange"] (fallback)
contrato_btc = bot.crear_contrato(_IBSinPosicionesCripto(), activo_btc)
check("crear_contrato CRYPTO: devuelve un Crypto (no un Stock)",
      isinstance(contrato_btc, bot.Crypto), f"tipo={type(contrato_btc)}")
check("crear_contrato CRYPTO: symbol/exchange/currency correctos",
      contrato_btc.symbol == "BTC" and contrato_btc.exchange == bot.EXCHANGE_CRYPTO and contrato_btc.currency == "USD")

# --- 9b-bis. crear_contrato CRYPTO: si YA hay una posicion de cripto real
#     abierta (con su conId/exchange ya resueltos por IBKR), se descubre y
#     reutiliza ESE exchange en vez de activo["exchange"] -este es el fix
#     real de produccion (sept. 2026): ni "PAXOS" ni "ZEROHASH" adivinados a
#     mano tenian datos en la cuenta real, solo el exchange de la posicion
#     real de la usuaria los tenia. Ver ACTIVOS_CRYPTO para la historia
#     completa.
class _ContratoCriptoResuelto:
    def __init__(self, exchange):
        self.symbol = "BTC"
        self.currency = "USD"
        self.secType = "CRYPTO"
        self.exchange = exchange


class _PosicionCriptoFalsa:
    def __init__(self, exchange):
        self.contract = _ContratoCriptoResuelto(exchange)
        self.position = 0.01


class _IBConPosicionCriptoResuelta:
    def __init__(self, exchange):
        self._exchange = exchange

    def positions(self):
        return [_PosicionCriptoFalsa(self._exchange)]

    def qualifyContracts(self, contrato):
        pass  # no hace falta: la posicion ya viene con exchange relleno


bot._exchange_cripto_cache = None
ib_con_posicion_real = _IBConPosicionCriptoResuelta("UNEXCHANGE_REAL_DESCONOCIDO")
activo_eth = {"ticker": "ETH", "exchange": bot.EXCHANGE_CRYPTO, "currency": "USD", "mercado": "CRYPTO"}
contrato_eth = bot.crear_contrato(ib_con_posicion_real, activo_eth)
check("crear_contrato CRYPTO: descubre el exchange a partir de una posicion real ya abierta",
      contrato_eth.exchange == "UNEXCHANGE_REAL_DESCONOCIDO", f"exchange={contrato_eth.exchange!r}")
check("crear_contrato CRYPTO: reutiliza ese exchange descubierto para OTRA moneda (ETH) sin posicion propia",
      contrato_eth.symbol == "ETH" and contrato_eth.exchange == "UNEXCHANGE_REAL_DESCONOCIDO")

# La cache es de PROCESO: una segunda llamada, incluso con una `ib` distinta
# que ya NO tiene esa posicion, debe seguir devolviendo el exchange
# descubierto la primera vez (no se vuelve a preguntar a IBKR en cada
# ticker/ciclo).
contrato_btc_cacheado = bot.crear_contrato(_IBSinPosicionesCripto(), activo_btc)
check("crear_contrato CRYPTO: el exchange descubierto se cachea entre llamadas",
      contrato_btc_cacheado.exchange == "UNEXCHANGE_REAL_DESCONOCIDO",
      f"exchange={contrato_btc_cacheado.exchange!r}")
bot._exchange_cripto_cache = None


# --- 9c. mercado_de_posicion / contrato_pertenece_a_mercado: cripto y US
#     acciones comparten divisa (USD), hay que distinguirlos por secType ---
class _ContratoConSecType:
    def __init__(self, symbol, currency, secType, exchange=""):
        self.symbol = symbol
        self.currency = currency
        self.secType = secType
        self.exchange = exchange


class _PosicionConSecType:
    def __init__(self, symbol, currency, secType, position=1, avgCost=100):
        self.contract = _ContratoConSecType(symbol, currency, secType)
        self.position = position
        self.avgCost = avgCost


pos_btc = _PosicionConSecType("BTC", "USD", "CRYPTO")
pos_aapl = _PosicionConSecType("AAPL", "USD", "STK")
check("mercado_de_posicion: BTC (secType=CRYPTO, USD) -> 'CRYPTO', no 'US'",
      bot.mercado_de_posicion(pos_btc) == "CRYPTO")
check("mercado_de_posicion: AAPL (secType=STK, USD) -> 'US'",
      bot.mercado_de_posicion(pos_aapl) == "US")

check("contrato_pertenece_a_mercado: BTC pertenece a 'CRYPTO'",
      bot.contrato_pertenece_a_mercado(pos_btc.contract, "CRYPTO") is True)
check("contrato_pertenece_a_mercado: BTC NO pertenece a 'US' (aunque comparta divisa USD)",
      bot.contrato_pertenece_a_mercado(pos_btc.contract, "US") is False)
check("contrato_pertenece_a_mercado: AAPL pertenece a 'US'",
      bot.contrato_pertenece_a_mercado(pos_aapl.contract, "US") is True)
check("contrato_pertenece_a_mercado: AAPL NO pertenece a 'CRYPTO'",
      bot.contrato_pertenece_a_mercado(pos_aapl.contract, "CRYPTO") is False)

# Un contrato sin atributo secType (p.ej. un doble de prueba antiguo) no debe
# romper nada: getattr con default lo trata como "no es cripto".
posicion_sin_sectype = _Posicion("XYZ", 1, 100)
resultado_sin_sectype = bot.mercado_de_posicion(posicion_sin_sectype)
check("mercado_de_posicion: contrato SIN atributo secType no revienta, se trata como no-cripto",
      resultado_sin_sectype == "US", f"resultado={resultado_sin_sectype}")


# --- 9d. estimar_comision_cripto: 0.18%, minimo 1.75 USD, tope 1% ---
# Operacion grande: 0.18% domina sobre el minimo (y no llega al tope del 1%)
check("estimar_comision_cripto: operacion grande, aplica el 0.18%",
      abs(bot.estimar_comision_cripto(10_000) - 18.0) < 1e-9,
      f"obtenido={bot.estimar_comision_cripto(10_000)}")

# Operacion pequeña: 0.18% no llega al minimo (1.75 USD), pero el tope del 1%
# del valor operado es AUN MENOR que el minimo -> se aplica el tope del 1%,
# no el minimo (protege operaciones pequeñas de pagar de mas).
comision_pequena = bot.estimar_comision_cripto(40.0)
check("estimar_comision_cripto: operacion de 40 USD, el tope del 1% (0.40) gana al minimo (1.75)",
      abs(comision_pequena - 0.40) < 1e-9, f"obtenido={comision_pequena}")

# Operacion en el rango donde SI se aplica el minimo (el 0.18% no llega a
# 1.75, pero el 1% del valor si supera 1.75 -> gana el minimo)
comision_media = bot.estimar_comision_cripto(500.0)
check("estimar_comision_cripto: operacion de 500 USD, se aplica el minimo de 1.75 USD",
      abs(comision_media - 1.75) < 1e-9, f"obtenido={comision_media}")

check("estimar_comision_cripto: valor 0 -> comision 0 (no revienta por division por cero)",
      bot.estimar_comision_cripto(0) == 0.0)


# --- crear_orden_limitada_cripto: tif='GTC' explicito (bug real de
#     produccion, sept. 2026: LimitOrder() deja tif='' por defecto, y el
#     exchange real de cripto de la cuenta -ZEROHASHE- lo rechazaba con
#     "Error 10052: Invalid time in force"; ninguna compra ni venta de
#     cripto llegaba a colocarse hasta fijar un tif valido a mano) ---
orden_cripto_tif = bot.crear_orden_limitada_cripto('BUY', 0.001, 50000.0)
check("crear_orden_limitada_cripto: tif='GTC' (no vacio, valido para el exchange de cripto)",
      orden_cripto_tif.tif == "GTC", f"tif={orden_cripto_tif.tif!r}")


# --- 9e. revisar_compras con un activo CRYPTO: usa LimitOrder con
#     totalQuantity fraccionario NATIVO (sin cashQty, sin Plan B/C) ---
class _IBFalsoComprasCripto:
    def __init__(self):
        self.ordenes_colocadas = []

    def reqPositions(self):
        pass

    def positions(self):
        return []

    def sleep(self, segundos):
        pass

    def accountSummary(self):
        return [types.SimpleNamespace(tag='NetLiquidation', currency='USD', value='300.0')]

    def reqHistoricalData(self, contrato, **kwargs):
        return [_Vela(50000.0)]  # precio simulado de BTC

    def reqContractDetails(self, contrato):
        return []

    def placeOrder(self, contrato, orden):
        self.ordenes_colocadas.append(orden)
        return types.SimpleNamespace(orderStatus=types.SimpleNamespace(status="Filled", filled=None, avgFillPrice=None),
                                      isDone=lambda: True, log=[])


activos_originales_compras = bot.ACTIVOS
bot.ACTIVOS = [activo_btc]
analizar_activo_original = bot.analizar_activo
bot.analizar_activo = lambda ib, activo: (bot.crear_contrato(ib, activo), "COMPRA")
bot._exchange_cripto_cache = None  # sin posiciones previas (positions() -> []), usa activo["exchange"]

ib_falso_compras_cripto = _IBFalsoComprasCripto()
try:
    bot.revisar_compras(ib_falso_compras_cripto)
finally:
    bot.ACTIVOS = activos_originales_compras
    bot.analizar_activo = analizar_activo_original

check("revisar_compras CRYPTO: coloca exactamente una orden",
      len(ib_falso_compras_cripto.ordenes_colocadas) == 1,
      f"ordenes={ib_falso_compras_cripto.ordenes_colocadas}")
if ib_falso_compras_cripto.ordenes_colocadas:
    orden_cripto = ib_falso_compras_cripto.ordenes_colocadas[0]
    check("revisar_compras CRYPTO: la orden es LIMITADA (no a mercado)",
          orden_cripto.orderType == "LMT", f"orderType={orden_cripto.orderType}")
    check("revisar_compras CRYPTO: usa totalQuantity fraccionario (no cashQty)",
          orden_cripto.totalQuantity > 0, f"totalQuantity={orden_cripto.totalQuantity}")
    check("revisar_compras CRYPTO: la cantidad es fraccionaria (no redondeada a entero)",
          bot.es_cantidad_fraccionaria(orden_cripto.totalQuantity),
          f"totalQuantity={orden_cripto.totalQuantity}")


# --- 9f. revisar_ventas con una posicion CRYPTO: usa LimitOrder con
#     totalQuantity fraccionario nativo, sin pasar por la logica de venta
#     forzada (que no aplica a cripto, no tiene "cierre diario") ---
class _IBFalsoVentasCripto:
    def __init__(self, posiciones):
        self.ordenes_colocadas = []
        self._posiciones = posiciones

    def reqPositions(self):
        pass

    def positions(self):
        return self._posiciones

    def sleep(self, segundos):
        pass

    def qualifyContracts(self, contrato):
        # Simula que IBKR resuelve el exchange real a partir del conId de la
        # posicion (asi es como revisar_ventas completa un contrato CRYPTO
        # que llega con exchange="" - ver bug real de produccion, error 200,
        # sept. 2026).
        contrato.exchange = bot.EXCHANGE_CRYPTO

    def reqHistoricalData(self, contrato, **kwargs):
        return [_Vela(55000.0)]  # +10% sobre el coste medio de 50000

    def placeOrder(self, contrato, orden):
        self.ordenes_colocadas.append(orden)
        return types.SimpleNamespace(orderStatus=types.SimpleNamespace(status="Filled", filled=None, avgFillPrice=None),
                                      isDone=lambda: True, log=[])


pos_venta_btc = _PosicionConSecType("BTC", "USD", "CRYPTO", position=0.01, avgCost=50000.0)
macd_bajista_original_cripto = bot.macd_5min_bajista
bot.macd_5min_bajista = lambda ib, contrato: True  # forzar señal de venta

ib_falso_ventas_cripto = _IBFalsoVentasCripto([pos_venta_btc])
try:
    bot.revisar_ventas(ib_falso_ventas_cripto)
finally:
    bot.macd_5min_bajista = macd_bajista_original_cripto

check("revisar_ventas CRYPTO: coloca exactamente una orden",
      len(ib_falso_ventas_cripto.ordenes_colocadas) == 1,
      f"ordenes={ib_falso_ventas_cripto.ordenes_colocadas}")
if ib_falso_ventas_cripto.ordenes_colocadas:
    orden_venta_cripto = ib_falso_ventas_cripto.ordenes_colocadas[0]
    check("revisar_ventas CRYPTO: la orden es LIMITADA (no a mercado)",
          orden_venta_cripto.orderType == "LMT", f"orderType={orden_venta_cripto.orderType}")
    check("revisar_ventas CRYPTO: usa totalQuantity fraccionario nativo (no cashQty)",
          abs(orden_venta_cripto.totalQuantity - 0.01) < 1e-9,
          f"totalQuantity={orden_venta_cripto.totalQuantity}")
    check("revisar_ventas CRYPTO: accion de venta (SELL)",
          orden_venta_cripto.action == "SELL", f"action={orden_venta_cripto.action}")

# Bug real de produccion (sept. 2026): el contrato de una posicion CRYPTO
# que llega de ib.positions() viene con exchange="" (a diferencia de las
# acciones), y reqHistoricalData lo rechazaba con el error 321 "Please enter
# exchange". revisar_ventas debe rellenarlo con EXCHANGE_CRYPTO antes de
# pedir datos.
check("revisar_ventas CRYPTO: rellena el exchange vacio del contrato antes de pedir datos",
      pos_venta_btc.contract.exchange == bot.EXCHANGE_CRYPTO,
      f"exchange={pos_venta_btc.contract.exchange!r}")


# ---------------------------------------------------------------------------
# 9d. UMBRAL_BENEFICIO_CRYPTO_PCT: cripto usa un umbral mas bajo (0.3%) que
#     acciones (0.5%, sigue en UMBRAL_BENEFICIO_PCT sin cambios) - peticion
#     del usuario, sept. 2026. El caso interesante es un beneficio NETO
#     entre ambos umbrales (0.3%-0.5%): con el umbral de acciones NO se
#     venderia, con el de cripto SI.
# ---------------------------------------------------------------------------
check("UMBRAL_BENEFICIO_CRYPTO_PCT es 0.3 (mas bajo que el de acciones, 0.5)",
      bot.UMBRAL_BENEFICIO_CRYPTO_PCT == 0.3 and bot.UMBRAL_BENEFICIO_PCT == 0.5,
      f"cripto={bot.UMBRAL_BENEFICIO_CRYPTO_PCT}, acciones={bot.UMBRAL_BENEFICIO_PCT}")


class _IBFalsoVentasCriptoUmbral(_IBFalsoVentasCripto):
    def reqHistoricalData(self, contrato, **kwargs):
        return [_Vela(50550.0)]  # +1.1% bruto sobre 50000 -> ~0.4% neto tras comision (~0.7%)


pos_venta_btc_umbral = _PosicionConSecType("BTC", "USD", "CRYPTO", position=0.01, avgCost=50000.0)
bot.macd_5min_bajista = lambda ib, contrato: True
ib_falso_umbral = _IBFalsoVentasCriptoUmbral([pos_venta_btc_umbral])
try:
    bot.revisar_ventas(ib_falso_umbral)
finally:
    bot.macd_5min_bajista = macd_bajista_original_cripto

check("revisar_ventas CRYPTO: con beneficio neto ~0.4% (entre 0.3% y 0.5%) SI vende, "
      "gracias al umbral mas bajo de cripto",
      len(ib_falso_umbral.ordenes_colocadas) == 1, f"ordenes={ib_falso_umbral.ordenes_colocadas}")


# ---------------------------------------------------------------------------
# 9e. Parametro `mercados` de revisar_compras/revisar_ventas: permite
#     revisar SOLO un subconjunto de mercados (usado por main() para que
#     CRYPTO corra en su propia cadencia de 1 min, independiente de
#     US/HK/KR a 4 min - ver CRYPTO_INTERVALO_SEGUNDOS).
# ---------------------------------------------------------------------------
activos_mixtos_filtro = [
    {"ticker": "AAPL", "exchange": "SMART", "currency": "USD", "mercado": "US"},
    activo_btc,
]
activos_originales_filtro = bot.ACTIVOS
bot.ACTIVOS = activos_mixtos_filtro
analizar_activo_original_filtro = bot.analizar_activo
bot.analizar_activo = lambda ib, activo: (bot.crear_contrato(ib, activo), "COMPRA")
bot._exchange_cripto_cache = None

ib_falso_compras_filtro = _IBFalsoComprasCripto()
try:
    bot.revisar_compras(ib_falso_compras_filtro, mercados={"CRYPTO"})
finally:
    bot.ACTIVOS = activos_originales_filtro
    bot.analizar_activo = analizar_activo_original_filtro

check("revisar_compras con mercados={'CRYPTO'}: solo opera BTC, ignora AAPL (mercado US)",
      len(ib_falso_compras_filtro.ordenes_colocadas) == 1,
      f"ordenes={ib_falso_compras_filtro.ordenes_colocadas}")

pos_venta_aapl_filtro = _Posicion("AAPL", 5, 190.0)
pos_venta_btc_filtro = _PosicionConSecType("BTC", "USD", "CRYPTO", position=0.01, avgCost=50000.0)
bot.macd_5min_bajista = lambda ib, contrato: True
ib_falso_ventas_filtro = _IBFalsoVentasCripto([pos_venta_aapl_filtro, pos_venta_btc_filtro])
try:
    bot.revisar_ventas(ib_falso_ventas_filtro, mercados={"CRYPTO"})
finally:
    bot.macd_5min_bajista = macd_bajista_original_cripto

check("revisar_ventas con mercados={'CRYPTO'}: solo opera BTC, ignora AAPL (mercado US)",
      len(ib_falso_ventas_filtro.ordenes_colocadas) == 1,
      f"ordenes={ib_falso_ventas_filtro.ordenes_colocadas}")


# ---------------------------------------------------------------------------
# 17. actualizar_tipo_cambio_eur_usd: refresca TIPO_CAMBIO_EUR_USD con el
#     precio real de mercado (Forex EUR.USD), en vez de dejarlo fijo a mano
#     - peticion del usuario, sept. 2026. Throttlada: no debe pedir datos
#     en cada llamada, solo cada INTERVALO_ACTUALIZACION_TIPO_CAMBIO_SEGUNDOS.
# ---------------------------------------------------------------------------
class _IBFalsoTipoCambio:
    def __init__(self, precio):
        self.precio = precio
        self.llamadas_reqHistoricalData = 0
        self.contrato_qualificado = None

    def qualifyContracts(self, contrato):
        contrato.conId = 12345
        self.contrato_qualificado = contrato

    def reqHistoricalData(self, contrato, **kwargs):
        self.llamadas_reqHistoricalData += 1
        return [_Vela(self.precio)]

    def sleep(self, segundos):
        pass


tipo_cambio_original = bot.TIPO_CAMBIO_EUR_USD
ultima_actualizacion_original = bot._ultima_actualizacion_tipo_cambio
bot.CONTRATO_EUR_USD.conId = None  # fuerza a que qualifyContracts se llame en esta prueba

# Nota: time.monotonic() NO empieza necesariamente en 0 -en un contenedor
# recien arrancado puede ser un numero pequeño-, asi que para simular "hace
# mas de INTERVALO_ACTUALIZACION_TIPO_CAMBIO_SEGUNDOS" hay que restar desde
# el "ahora" real, no asumir que 0.0 ya esta suficientemente en el pasado.
_hace_rato = time.monotonic() - bot.INTERVALO_ACTUALIZACION_TIPO_CAMBIO_SEGUNDOS - 1
try:
    bot._ultima_actualizacion_tipo_cambio = _hace_rato  # fuerza que la primera llamada SI actualice
    ib_falso_cambio = _IBFalsoTipoCambio(1.2345)
    bot.actualizar_tipo_cambio_eur_usd(ib_falso_cambio)
    check("actualizar_tipo_cambio_eur_usd: actualiza TIPO_CAMBIO_EUR_USD con el precio real",
          bot.TIPO_CAMBIO_EUR_USD == 1.2345, f"TIPO_CAMBIO_EUR_USD={bot.TIPO_CAMBIO_EUR_USD}")
    check("actualizar_tipo_cambio_eur_usd: pide velas de un contrato de forex (EUR.USD)",
          ib_falso_cambio.llamadas_reqHistoricalData == 1)

    # Llamada inmediata siguiente: throttlada, NO debe volver a pedir datos
    # ni cambiar el valor, aunque el precio simulado sea distinto.
    ib_falso_cambio_2 = _IBFalsoTipoCambio(9.9999)
    bot.actualizar_tipo_cambio_eur_usd(ib_falso_cambio_2)
    check("actualizar_tipo_cambio_eur_usd: una segunda llamada inmediata esta throttlada (no pide datos)",
          ib_falso_cambio_2.llamadas_reqHistoricalData == 0)
    check("actualizar_tipo_cambio_eur_usd: el valor no cambia mientras este throttlado",
          bot.TIPO_CAMBIO_EUR_USD == 1.2345, f"TIPO_CAMBIO_EUR_USD={bot.TIPO_CAMBIO_EUR_USD}")

    # Si falla (sin velas), se mantiene el valor anterior sin excepcion.
    bot._ultima_actualizacion_tipo_cambio = time.monotonic() - bot.INTERVALO_ACTUALIZACION_TIPO_CAMBIO_SEGUNDOS - 1

    class _IBFalsoTipoCambioSinDatos(_IBFalsoTipoCambio):
        def reqHistoricalData(self, contrato, **kwargs):
            self.llamadas_reqHistoricalData += 1
            return []

    bot.actualizar_tipo_cambio_eur_usd(_IBFalsoTipoCambioSinDatos(1.5))
    check("actualizar_tipo_cambio_eur_usd: si no hay velas, mantiene el valor anterior sin excepcion",
          bot.TIPO_CAMBIO_EUR_USD == 1.2345, f"TIPO_CAMBIO_EUR_USD={bot.TIPO_CAMBIO_EUR_USD}")
finally:
    bot.TIPO_CAMBIO_EUR_USD = tipo_cambio_original
    bot._ultima_actualizacion_tipo_cambio = ultima_actualizacion_original


# ---------------------------------------------------------------------------
# Resumen final
# ---------------------------------------------------------------------------
print()
if fallos:
    print(f"{len(fallos)} test(s) FALLARON: {fallos}")
    sys.exit(1)
else:
    print("Todos los tests pasaron correctamente.")
