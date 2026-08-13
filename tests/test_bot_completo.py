"""
Tests manuales (sin pytest) para las funciones de logica pura de
bot_completo.py: no requieren conexion a IBKR, solo importan el modulo y
prueban calculos matematicos y de horarios con datos simulados.
"""
import os
import sys
import tempfile
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

comision_pequena = bot.estimar_comision(100, "USD")  # 0.07% de 100 = 0.07, por debajo del minimo
check("estimar_comision: aplica el minimo cuando el 0.07% es menor",
      abs(comision_pequena - bot.minimo_comision_en_moneda("USD")) < 1e-9,
      f"obtenido={comision_pequena}")

comision_grande = bot.estimar_comision(1_000_000, "USD")  # 0.07% de 1M = 700, por encima del minimo
check("estimar_comision: aplica el 0.07% cuando supera el minimo",
      abs(comision_grande - 1_000_000 * bot.COMISION_PCT) < 1e-9,
      f"obtenido={comision_grande}")


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

# Miercoles 20:00 ET -> mercado US cerrado (fuera de premercado y regular)
miercoles_us_cerrado = datetime(2026, 8, 12, 20, 0, tzinfo=bot.ZONA_NY)
check("es_horario_operativo US: miercoles 20:00 ET -> cerrado",
      con_reloj_fijo(miercoles_us_cerrado, bot.es_horario_operativo, "US") is False)

# Sabado 10:00 ET -> cerrado por ser fin de semana
sabado_us = datetime(2026, 8, 15, 10, 0, tzinfo=bot.ZONA_NY)
check("es_horario_operativo US: sabado -> cerrado (fin de semana)",
      con_reloj_fijo(sabado_us, bot.es_horario_operativo, "US") is False)

# Justo en el limite de cierre (16:00 ET, exclusivo) -> cerrado
limite_cierre_us = datetime(2026, 8, 12, 16, 0, tzinfo=bot.ZONA_NY)
check("es_horario_operativo US: exactamente a las 16:00 ET -> cerrado (limite exclusivo)",
      con_reloj_fijo(limite_cierre_us, bot.es_horario_operativo, "US") is False)

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
    bot.revisar_compras(ib_falso_frac)
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
class _IBFalsoRechazoFraccion(_IBFalsoCompras):
    def __init__(self):
        super().__init__()
        self.ordenes_objeto = []

    def placeOrder(self, contrato, orden):
        self.ordenes_objeto.append(orden)
        if orden.totalQuantity == 0:
            # La orden por cashQty siempre es rechazada con el error 10244
            trade = types.SimpleNamespace(
                orderStatus=types.SimpleNamespace(status="Cancelled"),
                isDone=lambda: True,
                log=[_TradeLogFalso(10244)],
            )
            return trade
        # La orden de acciones enteras (el fallback) SI se ejecuta
        return types.SimpleNamespace(orderStatus=types.SimpleNamespace(status="Filled"),
                                      isDone=lambda: True, log=[])


bot.ACTIVOS = [{"ticker": "SINFRACCION", "exchange": "SMART", "currency": "USD", "mercado": "US"}]
bot.es_horario_operativo = lambda mercado: True
bot.en_ventana_sin_compra = lambda mercado: False
ib_falso_rechazo = _IBFalsoRechazoFraccion()
try:
    bot.revisar_compras(ib_falso_rechazo)
finally:
    bot.ACTIVOS = activos_originales
    bot.es_horario_operativo = es_horario_original
    bot.en_ventana_sin_compra = en_ventana_sin_compra_original

check("Plan B error 10244: se intentaron 2 ordenes (cashQty rechazada + fallback en acciones enteras)",
      len(ib_falso_rechazo.ordenes_objeto) == 2,
      f"ordenes={[(o.totalQuantity, getattr(o, 'cashQty', None)) for o in ib_falso_rechazo.ordenes_objeto]}")
check("Plan B error 10244: la primera orden fue por cashQty (totalQuantity=0)",
      len(ib_falso_rechazo.ordenes_objeto) == 2 and ib_falso_rechazo.ordenes_objeto[0].totalQuantity == 0)
check("Plan B error 10244: la segunda orden (fallback) fue por acciones ENTERAS (totalQuantity>0)",
      len(ib_falso_rechazo.ordenes_objeto) == 2 and ib_falso_rechazo.ordenes_objeto[1].totalQuantity >= 1,
      f"totalQuantity={ib_falso_rechazo.ordenes_objeto[1].totalQuantity if len(ib_falso_rechazo.ordenes_objeto) == 2 else 'N/A'}")


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
# Resumen final
# ---------------------------------------------------------------------------
print()
if fallos:
    print(f"{len(fallos)} test(s) FALLARON: {fallos}")
    sys.exit(1)
else:
    print("Todos los tests pasaron correctamente.")
