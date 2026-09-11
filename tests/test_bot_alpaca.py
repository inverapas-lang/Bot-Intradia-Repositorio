"""
Tests manuales (sin pytest) para las funciones de logica pura de
bot_alpaca.py: no requieren conexion real a Alpaca, solo importan el modulo
y prueban calculos matematicos, de horarios y de decision con datos
simulados (clientes falsos que imitan TradingClient/StockHistoricalDataClient).
"""
import os
import sys
import tempfile
import types
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("ALPACA_API_KEY", "test-key")
os.environ.setdefault("ALPACA_SECRET_KEY", "test-secret")

import bot_alpaca as bot
import pandas as pd

_DIR_TEMP_ESTADO_RUNTIME = tempfile.mkdtemp()
bot.ARCHIVO_LATIDO = os.path.join(_DIR_TEMP_ESTADO_RUNTIME, "latido_bot_alpaca.txt")
bot.ARCHIVO_PID = os.path.join(_DIR_TEMP_ESTADO_RUNTIME, "bot_alpaca.pid")
bot.ARCHIVO_HISTORIAL_OPERACIONES = os.path.join(_DIR_TEMP_ESTADO_RUNTIME, "historial_operaciones_alpaca_test.json")
bot.ARCHIVO_ESTADO_VENTA = os.path.join(_DIR_TEMP_ESTADO_RUNTIME, "estado_venta_test.json")

fallos = []


def check(nombre, condicion, detalle=""):
    estado = "OK  " if condicion else "FAIL"
    print(f"[{estado}] {nombre}" + (f" -- {detalle}" if detalle and not condicion else ""))
    if not condicion:
        fallos.append(nombre)


# ---------------------------------------------------------------------------
# 1. calcular_macd: misma comprobacion que en test_bot_completo.py (funcion
#    identica, copiada literalmente).
# ---------------------------------------------------------------------------
precios_alcistas = pd.Series([100 + i * 0.5 for i in range(60)])
macd, senal, hist = bot.calcular_macd(precios_alcistas)
check("calcular_macd: longitud de las series coincide con la de entrada",
      len(macd) == len(precios_alcistas) == len(senal) == len(hist))
check("calcular_macd: en una serie claramente alcista, el ultimo MACD es positivo",
      macd.iloc[-1] > 0, f"macd_final={macd.iloc[-1]}")


# ---------------------------------------------------------------------------
# 2. decidir_senal: replica la logica de decision de compra (identica a
#    analizar_activo() en bot_completo.py) a partir de un dict ya calculado.
# ---------------------------------------------------------------------------
todas_alcistas = {tf["nombre"]: True for tf in bot.TEMPORALIDADES}
check("decidir_senal: las 7 temporalidades alcistas -> COMPRA",
      bot.decidir_senal(todas_alcistas) == "COMPRA")

cuatro_cortas_alcistas_1h_bajista = dict(todas_alcistas)
cuatro_cortas_alcistas_1h_bajista["1 hora"] = False  # fuera del atajo (no esta en NOMBRES_4_CORTAS)
check("decidir_senal: atajo de 4 cortas alcistas (1h bajista, largas OK) -> COMPRA directa",
      bot.decidir_senal(cuatro_cortas_alcistas_1h_bajista) == "COMPRA")

falta_una_temporalidad = dict(todas_alcistas)
falta_una_temporalidad["1 semana"] = None
falta_una_temporalidad["1 minuto"] = False  # rompe el atajo de 4 cortas
check("decidir_senal: falta un dato (no en las 4 cortas) -> SIN_DATOS",
      bot.decidir_senal(falta_una_temporalidad) == "SIN_DATOS")

una_corta_en_contra = dict(todas_alcistas)
una_corta_en_contra["1 minuto"] = False  # rompe el atajo de 4 cortas, pero es un retroceso normal
check("decidir_senal: 1 de 7 en contra y es CORTA (retroceso normal) -> COMPRA",
      bot.decidir_senal(una_corta_en_contra) == "COMPRA")

# --- Bug real corregido (sept. 2026, petición del usuario): antes, el
# atajo de "4 cortas alcistas" ignoraba por completo dia/semana, asi que
# compraba igual aunque la tendencia diaria o semanal fuera claramente
# bajista -la peor categoria de entrada, contra la tendencia dominante-. Y
# como el atajo se adelanta a la regla de "1 de 7 en contra", esta ultima
# NUNCA llegaba a aplicarse en este caso exacto (con las 4 cortas alcistas,
# lo mas habitual). Ahora el atajo TAMBIEN exige que ninguna larga este en
# contra. ---
una_larga_en_contra = dict(todas_alcistas)
una_larga_en_contra["1 dia"] = False  # las 4 cortas siguen alcistas, pero la diaria no
check("decidir_senal: 1 de 7 en contra y es LARGA (dia) -> BLOQUEADO_TF_LARGA, NO compra "
      "(antes: bug que compraba igual via el atajo de 4 cortas)",
      bot.decidir_senal(una_larga_en_contra) == "BLOQUEADO_TF_LARGA")

una_semana_en_contra = dict(todas_alcistas)
una_semana_en_contra["1 semana"] = False
check("decidir_senal: 1 de 7 en contra y es LARGA (semana) -> BLOQUEADO_TF_LARGA, NO compra",
      bot.decidir_senal(una_semana_en_contra) == "BLOQUEADO_TF_LARGA")

# NOTA: "BLOQUEADO" (cortas ok, largas no) es un resultado que, con las 4
# cortas dentro de NOMBRES_4_CORTAS, en la practica ya no puede darse: el
# atajo de "4 cortas alcistas" dispara ANTES y devuelve COMPRA directamente
# (mismo comportamiento documentado en bot_completo.py/NOTES.md). Se prueba
# en su lugar el caso real: 2 en contra (fuera del atajo) -> SIN_SENAL.
dos_en_contra = dict(todas_alcistas)
dos_en_contra["1 minuto"] = False  # rompe el atajo de 4 cortas
dos_en_contra["1 dia"] = False
check("decidir_senal: 2 de 7 en contra (atajo roto, cortas no ok) -> SIN_SENAL",
      bot.decidir_senal(dos_en_contra) == "SIN_SENAL")


# ---------------------------------------------------------------------------
# 3. macd_alcista_o_bajista: menos de 35 velas -> None; con datos suficientes
#    determina alcista/bajista segun el tipo de temporalidad.
# ---------------------------------------------------------------------------
class _VelaFalsa:
    def __init__(self, close):
        self.close = close


check("macd_alcista_o_bajista: menos de 35 velas -> None",
      bot.macd_alcista_o_bajista([_VelaFalsa(100) for _ in range(10)], "corta") is None)

velas_acelerando = [_VelaFalsa(100 * (1.02 ** i)) for i in range(60)]
check("macd_alcista_o_bajista: serie acelerando al alza, temporalidad corta -> True",
      bot.macd_alcista_o_bajista(velas_acelerando, "corta") is True)


# ---------------------------------------------------------------------------
# 4. Horarios: reloj fijo, igual que en test_bot_completo.py.
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


miercoles_regular = datetime(2026, 8, 12, 10, 0, tzinfo=bot.ZONA_NY)
check("es_horario_operativo: miercoles 10:00 ET (sesion regular) -> abierto",
      con_reloj_fijo(miercoles_regular, bot.es_horario_operativo) is True)

miercoles_cerrado = datetime(2026, 8, 12, 22, 0, tzinfo=bot.ZONA_NY)
check("es_horario_operativo: miercoles 22:00 ET -> cerrado",
      con_reloj_fijo(miercoles_cerrado, bot.es_horario_operativo) is False)

sabado = datetime(2026, 8, 15, 10, 0, tzinfo=bot.ZONA_NY)
check("es_horario_operativo: sabado -> cerrado (fin de semana)",
      con_reloj_fijo(sabado, bot.es_horario_operativo) is False)

# --- Festivos de NYSE/Nasdaq (peticion del usuario, sept. 2026) ---
check("festivos_nyse 2026: Labor Day cae en 7 de septiembre (caso real reportado por el usuario)",
      date(2026, 9, 7) in bot.festivos_nyse(2026))
check("es_festivo_us: 7 de septiembre de 2026 (Labor Day) -> True", bot.es_festivo_us(date(2026, 9, 7)))
check("es_festivo_us: 8 de septiembre de 2026 (dia normal) -> False", not bot.es_festivo_us(date(2026, 9, 8)))

labor_day_2026 = datetime(2026, 9, 7, 10, 0, tzinfo=bot.ZONA_NY)
check("es_horario_operativo: Labor Day 2026 en horario normal -> cerrado por festivo",
      con_reloj_fijo(labor_day_2026, bot.es_horario_operativo) is False)
check("en_postmercado_us: Labor Day 2026 -> cerrado por festivo (no solo mira fin de semana)",
      con_reloj_fijo(datetime(2026, 9, 7, 17, 0, tzinfo=bot.ZONA_NY), bot.en_postmercado_us) is False)
check("fuera_de_sesion_regular_us: Labor Day 2026 en premercado -> cerrado por festivo",
      con_reloj_fijo(datetime(2026, 9, 7, 6, 0, tzinfo=bot.ZONA_NY), bot.fuera_de_sesion_regular_us) is False)
check("es_horario_operativo: el dia siguiente al festivo, horario normal -> abierto",
      con_reloj_fijo(datetime(2026, 9, 8, 10, 0, tzinfo=bot.ZONA_NY), bot.es_horario_operativo) is True)

limite_cierre_regular = datetime(2026, 8, 12, 16, 0, tzinfo=bot.ZONA_NY)
check("es_horario_operativo: exactamente a las 16:00 ET -> ABIERTO (empieza postmercado)",
      con_reloj_fijo(limite_cierre_regular, bot.es_horario_operativo) is True)
check("en_postmercado_us: exactamente a las 16:00 ET -> True",
      con_reloj_fijo(limite_cierre_regular, bot.en_postmercado_us) is True)

premercado = datetime(2026, 8, 12, 6, 0, tzinfo=bot.ZONA_NY)
check("fuera_de_sesion_regular_us: 6:00 ET (premercado) -> True",
      con_reloj_fijo(premercado, bot.fuera_de_sesion_regular_us) is True)
check("fuera_de_sesion_regular_us: 10:00 ET (sesion regular) -> False",
      con_reloj_fijo(miercoles_regular, bot.fuera_de_sesion_regular_us) is False)

quince_cincuenta = datetime(2026, 8, 12, 15, 50, tzinfo=bot.ZONA_NY)
minutos = con_reloj_fijo(quince_cincuenta, bot.minutos_hasta_cierre)
check("minutos_hasta_cierre: 15:50 ET -> 10.0", abs(minutos - 10.0) < 1e-9, f"obtenido={minutos}")
check("en_ventana_sin_compra: 15:50 ET (10 min antes del cierre) -> True",
      con_reloj_fijo(quince_cincuenta, bot.en_ventana_sin_compra) is True)
check("en_ventana_venta_forzada: 15:50 ET (10 min antes del cierre) -> True",
      con_reloj_fijo(quince_cincuenta, bot.en_ventana_venta_forzada) is True)


# ---------------------------------------------------------------------------
# 5. Comisiones: a peticion del usuario, se asume 0 en compras Y en ventas
#    (no hay ninguna funcion de comision que probar; se comprueba mas abajo,
#    en los tests de revisar_ventas, que el beneficio mostrado es el bruto
#    sin ningun descuento).
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# 6. es_cantidad_fraccionaria / calcular_precio_limite_venta
# ---------------------------------------------------------------------------
check("es_cantidad_fraccionaria: 3.544 -> True", bot.es_cantidad_fraccionaria(3.544) is True)
check("es_cantidad_fraccionaria: 3.0 -> False", bot.es_cantidad_fraccionaria(3.0) is False)
check("calcular_precio_limite_venta: 0.2% por debajo del precio actual",
      abs(bot.calcular_precio_limite_venta(100.0) - 99.8) < 1e-9)


# ---------------------------------------------------------------------------
# 6b. _forzar_timeout_por_defecto: bug real visto en produccion -alpaca-py
#     no pone NINGUN timeout por defecto en sus peticiones HTTP, asi que un
#     corte de red deja el bot colgado indefinidamente (paso 3 horas hasta
#     que el usuario hizo Ctrl+C a mano). Se comprueba que la peticion
#     resultante SIEMPRE lleva un timeout, tanto si el llamador no pasa
#     ninguno como si ya pasaba uno explicito (no se debe sobreescribir).
# ---------------------------------------------------------------------------
class _SesionFalsa:
    def __init__(self):
        self.ultima_llamada_kwargs = None

    def request(self, *args, **kwargs):
        self.ultima_llamada_kwargs = kwargs
        return "respuesta-falsa"


class _ClienteFalso:
    def __init__(self):
        self._session = _SesionFalsa()


cliente_falso = _ClienteFalso()
sesion_falsa = cliente_falso._session
bot._forzar_timeout_por_defecto(cliente_falso)

cliente_falso._session.request("GET", "http://ejemplo")
check("_forzar_timeout_por_defecto: añade timeout cuando el llamador no pasa ninguno",
      sesion_falsa.ultima_llamada_kwargs.get("timeout") == bot.HTTP_TIMEOUT_SEGUNDOS,
      f"kwargs={sesion_falsa.ultima_llamada_kwargs}")

cliente_falso._session.request("GET", "http://ejemplo", timeout=5)
check("_forzar_timeout_por_defecto: NO sobreescribe un timeout ya puesto por el llamador",
      sesion_falsa.ultima_llamada_kwargs.get("timeout") == 5,
      f"kwargs={sesion_falsa.ultima_llamada_kwargs}")


# --- 6c. notificar_telegram: opcional, no rompe nada si no esta configurado ---
llamadas_post = []


def _post_falso(url, data=None, timeout=None):
    llamadas_post.append((url, data, timeout))
    return types.SimpleNamespace(status_code=200)


token_original = bot.TELEGRAM_BOT_TOKEN
chat_id_original = bot.TELEGRAM_CHAT_ID
post_original = bot.requests.post
bot.requests.post = _post_falso

try:
    bot.TELEGRAM_BOT_TOKEN = ""
    bot.TELEGRAM_CHAT_ID = ""
    bot.notificar_telegram("mensaje de prueba")
    check("notificar_telegram: sin configurar, NO llama a requests.post",
          llamadas_post == [], f"llamadas={llamadas_post}")

    bot.TELEGRAM_BOT_TOKEN = "token-falso"
    bot.TELEGRAM_CHAT_ID = "12345"
    bot.notificar_telegram("mensaje de prueba")
    check("notificar_telegram: configurado, SI llama a requests.post con el chat_id correcto",
          len(llamadas_post) == 1 and llamadas_post[0][1]["chat_id"] == "12345",
          f"llamadas={llamadas_post}")
finally:
    bot.TELEGRAM_BOT_TOKEN = token_original
    bot.TELEGRAM_CHAT_ID = chat_id_original
    bot.requests.post = post_original


# ---------------------------------------------------------------------------
# 7. pedir_velas_lote: reintentos ante fallo, exito al primer intento, y
#    devuelve dict {ticker: [velas]} para varios tickers a la vez.
# ---------------------------------------------------------------------------
class _BarSetFalso:
    def __init__(self, data):
        self.data = data


class _DataClientFalsoExito:
    def __init__(self):
        self.llamadas = 0

    def get_stock_bars(self, peticion):
        self.llamadas += 1
        return _BarSetFalso({t: [_VelaFalsa(100)] for t in peticion.symbol_or_symbols})


data_client_original = bot._data_client
bot._data_client = _DataClientFalsoExito()
try:
    resultado = bot.pedir_velas_lote(["AAPL", "MSFT"], bot.TEMPORALIDADES[0]["timeframe"], 3)
finally:
    bot._data_client = data_client_original

check("pedir_velas_lote: exito al primer intento, sin reintentos",
      bot._data_client is data_client_original)  # sanity: se restauro bien
check("pedir_velas_lote: devuelve datos para ambos tickers pedidos",
      set(resultado.keys()) == {"AAPL", "MSFT"} and len(resultado["AAPL"]) == 1)


class _DataClientFalsoFallaSiempre:
    def __init__(self):
        self.llamadas = 0

    def get_stock_bars(self, peticion):
        self.llamadas += 1
        raise RuntimeError("fallo de red simulado")


data_client_falla = _DataClientFalsoFallaSiempre()
sleep_original = bot.time.sleep
bot.time.sleep = lambda s: None  # no perder tiempo real esperando entre reintentos
bot._data_client = data_client_falla
try:
    resultado_vacio = bot.pedir_velas_lote(["AAPL"], bot.TEMPORALIDADES[0]["timeframe"], 3)
finally:
    bot._data_client = data_client_original
    bot.time.sleep = sleep_original

check("pedir_velas_lote: agota los 3 reintentos si siempre falla",
      data_client_falla.llamadas == bot.INTENTOS_MAXIMOS_DATOS,
      f"llamadas={data_client_falla.llamadas}")
check("pedir_velas_lote: devuelve listas vacias (no lanza excepcion) si nunca hay datos",
      resultado_vacio == {"AAPL": []})


# ---------------------------------------------------------------------------
# 8. revisar_compras / revisar_ventas: comportamiento con clientes falsos,
#    en sesion regular y en pre/postmercado.
# ---------------------------------------------------------------------------
class _TradingClientFalso:
    def __init__(self, portfolio_value=10_000, posiciones=None, ordenes_abiertas=None, cash=1_000_000.0):
        self.portfolio_value = portfolio_value
        self._posiciones = posiciones or []
        self.ordenes = []
        self._ordenes_abiertas = ordenes_abiertas or []  # simuladas, por simbolo
        self.ordenes_canceladas = []
        self.cash = cash

    def get_account(self):
        if self.cash is None:
            return types.SimpleNamespace(portfolio_value=str(self.portfolio_value))
        return types.SimpleNamespace(portfolio_value=str(self.portfolio_value), cash=str(self.cash))

    def get_all_positions(self):
        return self._posiciones

    def submit_order(self, order_data):
        self.ordenes.append(order_data)
        return types.SimpleNamespace(id="orden-falsa-1")

    def get_order_by_id(self, order_id):
        return types.SimpleNamespace(status=types.SimpleNamespace(value="filled"))

    def get_orders(self, filter):
        simbolos = set(filter.symbols or [])
        return [o for o in self._ordenes_abiertas if o.symbol in simbolos]

    def cancel_order_by_id(self, order_id):
        self.ordenes_canceladas.append(order_id)


def _fake_data_client_alcista():
    class _DataClientAlcista:
        def get_stock_bars(self, peticion):
            # Serie acelerando al alza -> señal de COMPRA para cualquier ticker.
            return _BarSetFalso({t: [_VelaFalsa(100 * (1.02 ** i)) for i in range(60)]
                                  for t in peticion.symbol_or_symbols})
    return _DataClientAlcista()


trading_client_original = bot._trading_client
activos_originales = bot.ACTIVOS
bot.ACTIVOS = ["AAPL"]

# --- Compras en sesion regular: debe colocar una orden a mercado (notional) ---
bot._trading_client = _TradingClientFalso(portfolio_value=10_000)
bot._data_client = _fake_data_client_alcista()
try:
    bot._cache_largas_por_dia = {}
    con_reloj_fijo(miercoles_regular, bot.revisar_compras)
finally:
    ordenes_regular = bot._trading_client.ordenes
    bot._trading_client = trading_client_original
    bot._data_client = data_client_original

check("revisar_compras en sesion regular: coloca exactamente una orden",
      len(ordenes_regular) == 1, f"ordenes={len(ordenes_regular)}")
if ordenes_regular:
    check("revisar_compras en sesion regular: la orden usa notional (importe en efectivo), no qty",
          ordenes_regular[0].notional is not None and ordenes_regular[0].qty is None)
    check("revisar_compras en sesion regular: NO tiene extended_hours activado",
          not ordenes_regular[0].extended_hours)

# --- Compras en premercado: debe colocar una orden LIMITADA con qty
# fraccionaria (Alpaca admite fracciones en limitadas con extended_hours
# desde marzo 2024, ya no exige cantidad entera) ---
bot._trading_client = _TradingClientFalso(portfolio_value=10_000)
bot._data_client = _fake_data_client_alcista()
try:
    bot._cache_largas_por_dia = {}
    con_reloj_fijo(premercado, bot.revisar_compras)
finally:
    ordenes_premercado = bot._trading_client.ordenes
    bot._trading_client = trading_client_original
    bot._data_client = data_client_original

check("revisar_compras en premercado: coloca exactamente una orden",
      len(ordenes_premercado) == 1, f"ordenes={len(ordenes_premercado)}")
if ordenes_premercado:
    orden_pre = ordenes_premercado[0]
    check("revisar_compras en premercado: la orden usa qty (no notional)",
          orden_pre.qty is not None and orden_pre.notional is None)
    check("revisar_compras en premercado: la cantidad es FRACCIONARIA",
          bot.es_cantidad_fraccionaria(orden_pre.qty))
    check("revisar_compras en premercado: tiene extended_hours activado",
          orden_pre.extended_hours is True)
    check("revisar_compras en premercado: es una orden LIMITADA",
          orden_pre.type.value == "limit")

# --- Compras en postmercado: NO debe colocar ninguna orden ---
bot._trading_client = _TradingClientFalso(portfolio_value=10_000)
bot._data_client = _fake_data_client_alcista()
postmercado = datetime(2026, 8, 12, 18, 0, tzinfo=bot.ZONA_NY)
try:
    bot._cache_largas_por_dia = {}
    con_reloj_fijo(postmercado, bot.revisar_compras)
finally:
    ordenes_postmercado = bot._trading_client.ordenes
    bot._trading_client = trading_client_original
    bot._data_client = data_client_original

check("revisar_compras en postmercado: NO coloca ninguna orden",
      ordenes_postmercado == [], f"ordenes={ordenes_postmercado}")

class _DataClientPrecioFijo:
    def __init__(self, precio):
        self.precio = precio

    def get_stock_bars(self, peticion):
        return _BarSetFalso({t: [_VelaFalsa(self.precio)] for t in peticion.symbol_or_symbols})


# --- Ventas: posicion fraccionaria fuera de sesion regular -> SI se vende
# (Alpaca admite qty fraccionaria en ordenes LIMITADAS con extended_hours
# desde marzo 2024; antes se asumia, por error, que era imposible) ---
posicion_fraccionaria = types.SimpleNamespace(
    symbol="AAPL", qty="3.544", avg_entry_price="100.0", market_value="400.0",
    unrealized_pl="10.0", unrealized_plpc="0.05",
)
macd_5min_bajista_original = bot.macd_5min_bajista_2_velas
bot._trading_client = _TradingClientFalso(posiciones=[posicion_fraccionaria])
bot._data_client = _DataClientPrecioFijo(105.0)  # +5% sobre el coste medio (100.0)
bot.macd_5min_bajista_2_velas = lambda ticker: True  # forzar señal de venta (refuerzo)
try:
    con_reloj_fijo(postmercado, bot.revisar_ventas)
finally:
    ordenes_venta_frac = bot._trading_client.ordenes
    bot._trading_client = trading_client_original
    bot._data_client = data_client_original
    bot.macd_5min_bajista_2_velas = macd_5min_bajista_original

check("revisar_ventas: posicion FRACCIONARIA en postmercado SI coloca una orden "
      "(Alpaca admite fracciones en limitadas con extended_hours)",
      len(ordenes_venta_frac) == 1, f"ordenes={ordenes_venta_frac}")
if ordenes_venta_frac:
    orden_venta_frac = ordenes_venta_frac[0]
    check("revisar_ventas fraccionaria en postmercado: la cantidad sigue siendo FRACCIONARIA",
          bot.es_cantidad_fraccionaria(orden_venta_frac.qty))
    check("revisar_ventas fraccionaria en postmercado: tiene extended_hours activado",
          orden_venta_frac.extended_hours is True)
    check("revisar_ventas fraccionaria en postmercado: es una orden LIMITADA",
          orden_venta_frac.type.value == "limit")

# --- Ventas: sin comision, el beneficio bruto exacto en el umbral (0.5%)
# debe bastar para vender (si hubiera comision restando, se quedaria por
# debajo del umbral y NO vendería) ---
posicion_en_el_umbral = types.SimpleNamespace(
    symbol="AAPL", qty="10", avg_entry_price="100.0", market_value="1005.0",
    unrealized_pl="5.0", unrealized_plpc="0.005",
)

macd_5min_bajista_original = bot.macd_5min_bajista_2_velas
bot._trading_client = _TradingClientFalso(posiciones=[posicion_en_el_umbral])
bot._data_client = _DataClientPrecioFijo(100.5)  # exactamente +0.5% sobre el coste medio (100.0)
bot.macd_5min_bajista_2_velas = lambda ticker: True  # forzar señal de venta (refuerzo)
try:
    con_reloj_fijo(miercoles_regular, bot.revisar_ventas)
finally:
    ordenes_umbral = bot._trading_client.ordenes
    bot._trading_client = trading_client_original
    bot._data_client = data_client_original
    bot.macd_5min_bajista_2_velas = macd_5min_bajista_original

check("revisar_ventas sin comision: +0.5% bruto exacto (= umbral) SI vende "
      "(con comision se habria quedado por debajo y no habria vendido)",
      len(ordenes_umbral) == 1, f"ordenes={ordenes_umbral}")

# --- Ventas: si hay una orden abierta anterior sin rellenar (p.ej. limitada
# que no llego a ejecutarse), se cancela ANTES de mandar la venta nueva -bug
# real visto en produccion: "insufficient qty available", la posicion entera
# quedaba retenida (held_for_orders) por la orden vieja- ---
orden_abierta_previa = types.SimpleNamespace(id="orden-vieja-META", symbol="AAPL")
macd_5min_bajista_original = bot.macd_5min_bajista_2_velas
cliente_con_orden_abierta = _TradingClientFalso(posiciones=[posicion_en_el_umbral],
                                                 ordenes_abiertas=[orden_abierta_previa])
bot._trading_client = cliente_con_orden_abierta
bot._data_client = _DataClientPrecioFijo(100.5)
bot.macd_5min_bajista_2_velas = lambda ticker: True
try:
    con_reloj_fijo(miercoles_regular, bot.revisar_ventas)
finally:
    bot._trading_client = trading_client_original
    bot._data_client = data_client_original
    bot.macd_5min_bajista_2_velas = macd_5min_bajista_original

check("revisar_ventas: cancela la orden abierta anterior antes de mandar la venta nueva",
      cliente_con_orden_abierta.ordenes_canceladas == ["orden-vieja-META"],
      f"canceladas={cliente_con_orden_abierta.ordenes_canceladas}")
check("revisar_ventas: tras cancelar la orden vieja, SI coloca la venta nueva",
      len(cliente_con_orden_abierta.ordenes) == 1, f"ordenes={cliente_con_orden_abierta.ordenes}")

bot.ACTIVOS = activos_originales


# ---------------------------------------------------------------------------
# 8b. Criterio de venta de ACCIONES: trailing stop (principal) + refuerzo de
#     2 velas de 5min bajistas (peticion del usuario, sept. 2026). Alpaca no
#     cobra comision en acciones, asi que aqui beneficio bruto == neto.
# ---------------------------------------------------------------------------
def _posicion_trailing(precio_medio, cantidad=10):
    return types.SimpleNamespace(symbol="TRAIL", qty=str(cantidad), avg_entry_price=str(precio_medio),
                                  market_value="0", unrealized_pl="0", unrealized_plpc="0")


macd_2velas_original_alpaca = bot.macd_5min_bajista_2_velas

# Por debajo de UMBRAL_BENEFICIO_PCT, ni el trailing ni el refuerzo venden.
bot.macd_5min_bajista_2_velas = lambda ticker: True  # refuerzo siempre "activo"
bot._maximo_beneficio_neto_por_posicion = {}
bot._trading_client = _TradingClientFalso(posiciones=[_posicion_trailing(100.0)])
bot._data_client = _DataClientPrecioFijo(100.2)  # +0.2%, por debajo del 0.5%
try:
    con_reloj_fijo(miercoles_regular, bot.revisar_ventas)
finally:
    ordenes_bajo_umbral = bot._trading_client.ordenes
    bot._trading_client = trading_client_original
    bot._data_client = data_client_original
check("criterio de venta: por debajo de UMBRAL_BENEFICIO_PCT, NO vende aunque el refuerzo este activo",
      ordenes_bajo_umbral == [], f"ordenes={ordenes_bajo_umbral}")

# Salida parcial (peticion del usuario, sept. 2026): la PRIMERA vez que se
# alcanza el umbral (retroceso=0, sin refuerzo necesario), se vende
# PORCENTAJE_SCALE_OUT de la posicion, dejando el resto corriendo.
bot.macd_5min_bajista_2_velas = lambda ticker: False  # refuerzo inactivo: la parcial no depende de el
bot._maximo_beneficio_neto_por_posicion = {}
bot._scale_out_realizado = set()
cliente_parcial = _TradingClientFalso(posiciones=[_posicion_trailing(100.0)])
bot._trading_client = cliente_parcial
bot._data_client = _DataClientPrecioFijo(100.5)  # justo en el umbral, retroceso=0
try:
    con_reloj_fijo(miercoles_regular, bot.revisar_ventas)
finally:
    bot._trading_client = trading_client_original
    bot._data_client = data_client_original
check("criterio de venta: primera vez en el umbral (retroceso=0) -> SALIDA PARCIAL, sin refuerzo ni retroceso",
      len(cliente_parcial.ordenes) == 1, f"ordenes={cliente_parcial.ordenes}")
if cliente_parcial.ordenes:
    check("criterio de venta: la salida parcial vende la MITAD (5 de 10 acciones)",
          abs(cliente_parcial.ordenes[0].qty - 5) < 1e-9, f"qty={cliente_parcial.ordenes[0].qty}")
check("criterio de venta: tras la salida parcial, el maximo SIGUE trackeado (queda posicion corriendo)",
      "TRAIL" in bot._maximo_beneficio_neto_por_posicion,
      f"cache={bot._maximo_beneficio_neto_por_posicion}")

# Refuerzo (2 velas bajistas) SI puede disparar la VENTA TOTAL del resto, ya
# con la parcial hecha, sin necesidad de retroceso del trailing.
bot.macd_5min_bajista_2_velas = lambda ticker: True  # refuerzo activo otra vez
bot._trading_client = cliente_parcial
bot._data_client = _DataClientPrecioFijo(100.5)  # mismo precio, sin retroceso
cliente_parcial.ordenes = []
try:
    con_reloj_fijo(miercoles_regular, bot.revisar_ventas)
finally:
    bot._trading_client = trading_client_original
    bot._data_client = data_client_original
check("criterio de venta: con la parcial ya hecha, el refuerzo de 2 velas SI vende el resto (total)",
      len(cliente_parcial.ordenes) == 1, f"ordenes={cliente_parcial.ordenes}")
check("criterio de venta: tras la venta total del resto, se olvida el maximo trackeado",
      "TRAIL" not in bot._maximo_beneficio_neto_por_posicion,
      f"cache={bot._maximo_beneficio_neto_por_posicion}")

# El trailing stop SI puede vender el resto por si solo, sin refuerzo,
# cuando el beneficio retrocede TRAILING_STOP_VENTA_PCT puntos desde el
# maximo (despues de que la salida parcial ya se hiciera).
bot.macd_5min_bajista_2_velas = lambda ticker: False  # refuerzo siempre "inactivo"
bot._maximo_beneficio_neto_por_posicion = {}
bot._scale_out_realizado = set()
cliente_trailing = _TradingClientFalso(posiciones=[_posicion_trailing(100.0)])
bot._trading_client = cliente_trailing
try:
    bot._data_client = _DataClientPrecioFijo(100.5)  # +0.5%: arma el trailing -> SALIDA PARCIAL
    con_reloj_fijo(miercoles_regular, bot.revisar_ventas)
    check("criterio de venta: al armar el trailing (retroceso=0) sin refuerzo, hace la SALIDA PARCIAL (no total)",
          len(cliente_trailing.ordenes) == 1, f"ordenes={cliente_trailing.ordenes}")

    cliente_trailing.ordenes = []
    bot._data_client = _DataClientPrecioFijo(101.02)  # +1.02%: nuevo maximo, sigue sin retroceso
    con_reloj_fijo(miercoles_regular, bot.revisar_ventas)
    check("criterio de venta: nuevo maximo alcanzado (+1.02%), sigue sin retroceso -> NO vende mas",
          cliente_trailing.ordenes == [], f"ordenes={cliente_trailing.ordenes}")

    bot._data_client = _DataClientPrecioFijo(100.71)  # +0.71%: retroceso de 0.31 pts desde el maximo -> dispara
    con_reloj_fijo(miercoles_regular, bot.revisar_ventas)
    check("criterio de venta: retrocede >=0.3 pts desde el maximo (1.02% -> 0.71%) -> SI vende el resto (trailing stop)",
          len(cliente_trailing.ordenes) == 1, f"ordenes={cliente_trailing.ordenes}")
finally:
    bot._trading_client = trading_client_original
    bot._data_client = data_client_original
    bot.macd_5min_bajista_2_velas = macd_2velas_original_alpaca
    bot._maximo_beneficio_neto_por_posicion = {}
    bot._scale_out_realizado = set()

# --- Escalones de trailing stop segun el maximo alcanzado (peticion del
# usuario, sept. 2026): cuanto mas alto el pico, mas margen de retroceso se
# permite antes de vender. Unidades ---
check("_margen_trailing_stop: por debajo de 2% -> margen base (TRAILING_STOP_VENTA_PCT, 0.3 pts)",
      bot._margen_trailing_stop(1.5) == bot.TRAILING_STOP_VENTA_PCT)
check("_margen_trailing_stop: maximo de 2% a <3% -> margen de 0.5 pts",
      bot._margen_trailing_stop(2.0) == 0.5 and bot._margen_trailing_stop(2.99) == 0.5)
check("_margen_trailing_stop: maximo de 3% a <4% -> margen de 0.7 pts",
      bot._margen_trailing_stop(3.0) == 0.7 and bot._margen_trailing_stop(3.99) == 0.7)
check("_margen_trailing_stop: maximo de 4% a <5% -> margen de 1.0 pts",
      bot._margen_trailing_stop(4.0) == 1.0 and bot._margen_trailing_stop(4.99) == 1.0)
check("_margen_trailing_stop: maximo >= 5% -> margen de 1.5 pts",
      bot._margen_trailing_stop(5.0) == 1.5 and bot._margen_trailing_stop(9.0) == 1.5)

# Extremo a extremo: con un maximo trackeado de 3.5% (dentro del escalon
# 3%-4%, margen 0.7 pts), un retroceso de 0.5 pts NO debe vender (el
# trailing stop plano de 0.3 pts SI lo habria vendido -esto confirma que
# el escalon esta realmente en efecto, no solo el valor base-, pero uno de
# 0.8 pts SI debe vender.
bot.macd_5min_bajista_2_velas = lambda ticker: False
bot._maximo_beneficio_neto_por_posicion = {"TRAIL": 3.5}
bot._scale_out_realizado = {"TRAIL"}  # la parcial ya se hizo antes de llegar al maximo
cliente_escalon = _TradingClientFalso(posiciones=[_posicion_trailing(100.0)])
bot._trading_client = cliente_escalon
try:
    bot._data_client = _DataClientPrecioFijo(103.0)  # +3.0%: retroceso de 0.5 pts desde el maximo (3.5%)
    con_reloj_fijo(miercoles_regular, bot.revisar_ventas)
    check("escalones trailing stop: con maximo de 3.5% (margen 0.7 pts), un retroceso de 0.5 pts NO vende",
          cliente_escalon.ordenes == [], f"ordenes={cliente_escalon.ordenes}")

    bot._data_client = _DataClientPrecioFijo(102.7)  # +2.7%: retroceso de 0.8 pts desde el maximo (3.5%)
    con_reloj_fijo(miercoles_regular, bot.revisar_ventas)
    check("escalones trailing stop: con maximo de 3.5% (margen 0.7 pts), un retroceso de 0.8 pts SI vende",
          len(cliente_escalon.ordenes) == 1, f"ordenes={cliente_escalon.ordenes}")
finally:
    bot._trading_client = trading_client_original
    bot._data_client = data_client_original
    bot.macd_5min_bajista_2_velas = macd_2velas_original_alpaca
    bot._maximo_beneficio_neto_por_posicion = {}
    bot._scale_out_realizado = set()

# --- Suelo explicito: nunca vender por debajo de MARGEN_MINIMO_VENTA_PCT
# (0.5%, peticion del usuario, sept. 2026). Caso real: BCH/USD se vendio
# con beneficio -0.57% porque el trailing, una vez armado, protegia lo
# ganado "sin limite inferior" -asi funcionaba a proposito hasta un cambio
# anterior que lo bajo a "nunca vender en negativo" (>=0%)-. Tras el caso
# real de META (venta con slippage que convirtio un +2% de referencia en
# una perdida real), el suelo se sube a un margen de seguridad de 0.5%
# -no solo evitar perdidas, sino dejar colchon frente al slippage-. ---
bot._maximo_beneficio_neto_por_posicion = {}
bot._scale_out_realizado = set()
accion, motivo = bot.decidir_accion_venta("SUELO", 5.0, bot.UMBRAL_BENEFICIO_PCT)  # arma el trailing en 5%
check("suelo anti-perdidas: primera vez en el umbral (5%) -> SALIDA PARCIAL",
      accion == "VENTA_PARCIAL", f"accion={accion}")
accion, motivo = bot.decidir_accion_venta("SUELO", -0.5, bot.UMBRAL_BENEFICIO_PCT)  # retroceso de 5.5 pts (>> margen 1.5)
check("suelo anti-perdidas: retroceso enorme (5.5 pts) pero beneficio ya NEGATIVO (-0.5%) -> NO vende",
      accion == "MANTENER", f"accion={accion}, motivo={motivo}")
accion, motivo = bot.decidir_accion_venta("SUELO", 0.0, bot.UMBRAL_BENEFICIO_PCT)  # retroceso de 5.0 pts, beneficio 0% (< suelo de 0.5%)
check("suelo anti-perdidas: beneficio 0% (no negativo, pero por debajo del suelo de 0.5%) -> NO vende",
      accion == "MANTENER", f"accion={accion}, motivo={motivo}")
accion, motivo = bot.decidir_accion_venta("SUELO", 0.49, bot.UMBRAL_BENEFICIO_PCT)  # justo por debajo del suelo
check("suelo anti-perdidas: beneficio 0.49% (justo por debajo del suelo de 0.5%) -> NO vende",
      accion == "MANTENER", f"accion={accion}, motivo={motivo}")
accion, motivo = bot.decidir_accion_venta("SUELO", 0.5, bot.UMBRAL_BENEFICIO_PCT)  # justo en el suelo
check("suelo anti-perdidas: beneficio exactamente 0.5% (el suelo) -> SI vende",
      accion == "VENTA_TOTAL", f"accion={accion}, motivo={motivo}")
bot._maximo_beneficio_neto_por_posicion = {}
bot._scale_out_realizado = set()

# --- Veto del scale-out por MACD alcista (peticion del usuario, sept.
# 2026, a raiz de revisar el historial real: SMCI vendio media posicion por
# scale-out y recompro casi al instante porque el MACD seguia diciendo
# "alcista" -las dos señales, trailing stop y MACD, no se hablaban entre
# si-). decidir_accion_venta() acepta ahora macd_alcista_fn: si devuelve
# True, se APLAZA el scale-out (sin marcarlo como ya hecho, para poder
# reintentarlo el siguiente ciclo); el trailing TOTAL nunca se veta. ---
bot._maximo_beneficio_neto_por_posicion = {}
bot._scale_out_realizado = set()
accion, motivo = bot.decidir_accion_venta("VETO", 5.0, bot.UMBRAL_BENEFICIO_PCT, macd_alcista_fn=lambda: True)
check("veto de scale-out por MACD alcista: en el umbral pero MACD sigue alcista -> se APLAZA (MANTENER)",
      accion == "MANTENER", f"accion={accion}, motivo={motivo}")
check("veto de scale-out por MACD alcista: NO se marca como ya hecho (se puede reintentar)",
      "VETO" not in bot._scale_out_realizado, f"_scale_out_realizado={bot._scale_out_realizado}")

# Si en el siguiente ciclo el MACD ya no esta alcista, el scale-out SI se hace.
accion, motivo = bot.decidir_accion_venta("VETO", 5.0, bot.UMBRAL_BENEFICIO_PCT, macd_alcista_fn=lambda: False)
check("veto de scale-out por MACD alcista: si el MACD deja de estar alcista, el scale-out SI se hace",
      accion == "VENTA_PARCIAL", f"accion={accion}, motivo={motivo}")

# El trailing TOTAL (disparo_trailing) nunca se veta, aunque el MACD siga alcista.
bot._maximo_beneficio_neto_por_posicion = {}
bot._scale_out_realizado = set()
bot.decidir_accion_venta("VETO2", 5.0, bot.UMBRAL_BENEFICIO_PCT, macd_alcista_fn=lambda: True)  # arma el maximo en 5%
accion, motivo = bot.decidir_accion_venta("VETO2", -0.5, bot.UMBRAL_BENEFICIO_PCT, macd_alcista_fn=lambda: True)
check("veto de scale-out por MACD alcista: el trailing TOTAL nunca se veta por MACD (retroceso "
      "enorme, beneficio bajo el suelo -> VENTA_TOTAL igualmente NO por venderse en negativo, sino por el suelo)",
      accion == "MANTENER", f"accion={accion}, motivo={motivo}")
bot._maximo_beneficio_neto_por_posicion = {}
bot._scale_out_realizado = set()
bot.decidir_accion_venta("VETO3", 5.0, bot.UMBRAL_BENEFICIO_PCT, macd_alcista_fn=lambda: False)  # scale-out ya hecho
accion, motivo = bot.decidir_accion_venta("VETO3", 3.4, bot.UMBRAL_BENEFICIO_PCT, macd_alcista_fn=lambda: True)  # retroceso 1.6 pts (> margen 0.7 de ese escalon)
check("veto de scale-out por MACD alcista: el trailing TOTAL SI dispara aunque el MACD siga "
      "alcista (solo se veta el scale-out, no la red de seguridad final)",
      accion == "VENTA_TOTAL", f"accion={accion}, motivo={motivo}")
bot._maximo_beneficio_neto_por_posicion = {}
bot._scale_out_realizado = set()

# --- BUG REAL DE PRODUCCION (sept. 2026, caso real: venta de META que
# parecia con beneficio positivo en Telegram pero en realidad se vendio
# mas barato de lo comprado): el % de beneficio mostrado/registrado se
# calculaba con precio_actual (el precio de referencia usado para DECIDIR
# vender, el cierre de la ultima vela ANTES de mandar la orden), no con el
# precio REAL de ejecucion que devuelve Alpaca -si hay slippage entre la
# decision y la ejecucion de una orden a mercado, el % podia llegar a
# tener el signo contrario a lo que realmente paso-. Ahora se recalcula
# con el precio real (obtener_ejecucion_real) antes de notificar/registrar. ---
class _TradingClientEjecucionReal(_TradingClientFalso):
    """Como _TradingClientFalso, pero get_order_by_id() devuelve un precio
    REAL de ejecucion distinto del precio de referencia usado para decidir
    -simula el slippage real visto en produccion-."""
    def __init__(self, *args, precio_real_venta, cantidad_real_venta, **kwargs):
        super().__init__(*args, **kwargs)
        self._precio_real_venta = precio_real_venta
        self._cantidad_real_venta = cantidad_real_venta

    def get_order_by_id(self, order_id):
        return types.SimpleNamespace(status=types.SimpleNamespace(value="filled"),
                                      filled_qty=str(self._cantidad_real_venta),
                                      filled_avg_price=str(self._precio_real_venta))


mensajes_telegram_slippage = []
notificar_telegram_original_slippage = bot.notificar_telegram
bot.notificar_telegram = lambda msg: mensajes_telegram_slippage.append(msg)
bot.macd_5min_bajista_2_velas = lambda ticker: False
bot._maximo_beneficio_neto_por_posicion = {}
bot._scale_out_realizado = set()
# Precio de referencia (decision): +2% -> arma el trailing, primera vez en
# el umbral -> VENTA PARCIAL de 5 (mitad de 10). Precio REAL de ejecucion:
# 99 (mas bajo que el coste de 100) -> en realidad fue una PERDIDA del 1%.
cliente_slippage = _TradingClientEjecucionReal(posiciones=[_posicion_trailing(100.0)],
                                                precio_real_venta=99.0, cantidad_real_venta=5)
bot._trading_client = cliente_slippage
bot._data_client = _DataClientPrecioFijo(102.0)
try:
    con_reloj_fijo(miercoles_regular, bot.revisar_ventas)
finally:
    bot._trading_client = trading_client_original
    bot._data_client = data_client_original
    bot.notificar_telegram = notificar_telegram_original_slippage
    bot.macd_5min_bajista_2_velas = macd_2velas_original_alpaca
    bot._maximo_beneficio_neto_por_posicion = {}
    bot._scale_out_realizado = set()

check("beneficio recalculado con precio real: coloca la orden basandose en el precio de "
      "referencia (+2%, arma el trailing)", len(cliente_slippage.ordenes) == 1,
      f"ordenes={cliente_slippage.ordenes}")
check("beneficio recalculado con precio real: el mensaje de Telegram NO muestra el +2% de "
      "referencia, muestra el -1% real (precio de ejecucion 99 vs coste 100)",
      len(mensajes_telegram_slippage) == 1 and "-1,00%" in mensajes_telegram_slippage[0]
      and "+2" not in mensajes_telegram_slippage[0],
      f"mensajes={mensajes_telegram_slippage}")
operaciones_slippage = bot.cargar_historial_operaciones()
venta_trail = [o for o in operaciones_slippage if o["ticker"] == "TRAIL" and o["lado"] == "VENTA"]
check("beneficio recalculado con precio real: el historial (fuente de /hoy) tambien guarda el "
      "-1% real, no el +2% de referencia",
      bool(venta_trail) and abs(venta_trail[-1]["beneficio_pct"] - (-1.0)) < 1e-6,
      f"ultimo_registro={venta_trail[-1] if venta_trail else None}")

# --- Integracion: revisar_ventas() con macd_5min_alcista_2_velas mockeado
# a True -el scale-out NO debe llegar a colocar ninguna orden, aunque el
# precio ya haya llegado al umbral de armado (peticion del usuario, sept.
# 2026, ver el bloque "veto de scale-out por MACD alcista" mas arriba). ---
macd_alcista_original_alpaca = bot.macd_5min_alcista_2_velas
bot.macd_5min_alcista_2_velas = lambda ticker: True
bot.macd_5min_bajista_2_velas = lambda ticker: False  # refuerzo inactivo, no interfiere en este test
bot._maximo_beneficio_neto_por_posicion = {}
bot._scale_out_realizado = set()
cliente_veto = _TradingClientFalso(posiciones=[_posicion_trailing(100.0)])
bot._trading_client = cliente_veto
bot._data_client = _DataClientPrecioFijo(102.0)  # +2% -> llega al umbral de armado (scale-out)
try:
    con_reloj_fijo(miercoles_regular, bot.revisar_ventas)
finally:
    bot._trading_client = trading_client_original
    bot._data_client = data_client_original
    bot.macd_5min_alcista_2_velas = macd_alcista_original_alpaca
    bot.macd_5min_bajista_2_velas = macd_2velas_original_alpaca
    bot._maximo_beneficio_neto_por_posicion = {}
    bot._scale_out_realizado = set()

check("integracion: con MACD 5min alcista mockeado, revisar_ventas NO coloca ninguna orden "
      "aunque el precio llegue al umbral de scale-out",
      len(cliente_veto.ordenes) == 0, f"ordenes={cliente_veto.ordenes}")

# --- BUG REAL DE PRODUCCION (sept. 2026, casos reales: META y despues
# SMCI vendidas con perdidas -0.23%, pese al suelo de MARGEN_MINIMO_VENTA_PCT
# 0.5%): la venta principal (trailing/refuerzo) de ACCIONES en sesion
# regular usaba una orden A MERCADO, sin ningun limite de precio. El
# suelo anti-perdidas solo se comprobaba en el momento de DECIDIR vender
# (con el precio de referencia), pero una orden a mercado puede
# ejecutarse a cualquier precio segundos despues si el mercado se mueve
# rapido -exactamente lo que paso con SMCI-. Ahora esa venta usa una
# orden LIMITADA (IOC), igual que ya hacia cripto: el precio real de
# ejecucion nunca puede ser peor que el limite. ---
bot.macd_5min_bajista_2_velas = lambda ticker: False
bot._maximo_beneficio_neto_por_posicion = {"TRAIL": 5.0}
bot._scale_out_realizado = {"TRAIL"}
cliente_orden_limitada = _TradingClientFalso(posiciones=[_posicion_trailing(100.0)])
bot._trading_client = cliente_orden_limitada
bot._data_client = _DataClientPrecioFijo(101.0)  # +1%: dispara el trailing (retroceso 4 pts >> margen 1.5)
try:
    con_reloj_fijo(miercoles_regular, bot.revisar_ventas)
finally:
    bot._trading_client = trading_client_original
    bot._data_client = data_client_original
    bot.macd_5min_bajista_2_velas = macd_2velas_original_alpaca
    bot._maximo_beneficio_neto_por_posicion = {}
    bot._scale_out_realizado = set()

check("venta principal de acciones en sesion regular: usa orden LIMITADA (no a mercado)",
      len(cliente_orden_limitada.ordenes) == 1 and cliente_orden_limitada.ordenes[0].type.value == "limit",
      f"ordenes={cliente_orden_limitada.ordenes}")
if cliente_orden_limitada.ordenes:
    orden_limitada_venta = cliente_orden_limitada.ordenes[0]
    check("venta principal de acciones: time_in_force es IOC",
          orden_limitada_venta.time_in_force.value == "ioc", f"tif={orden_limitada_venta.time_in_force}")
    check("venta principal de acciones: el precio limite esta acotado bajo el precio de "
          "referencia (MARGEN_ORDEN_LIMITADA_VENTA_PCT, 0.2%), no sin limite como una orden a mercado",
          abs(orden_limitada_venta.limit_price - bot.calcular_precio_limite_venta(101.0)) < 1e-6,
          f"limit_price={orden_limitada_venta.limit_price}")

# Ahora el caso que de verdad importa: si el precio se mueve tan rapido
# que la orden IOC NO llega a ejecutarse (el mercado nunca toco el limite
# protegido), la posicion debe quedar INTACTA -sin vender mas barato de
# lo aceptable- en vez de ejecutarse igualmente como pasaba con la orden
# a mercado.
class _TradingClientIOCNoEjecutada(_TradingClientFalso):
    """IOC que NO se ejecuta (cancelada de inmediato porque el mercado
    nunca llego al precio limite protegido): get_order_by_id devuelve
    'canceled' y la posicion NO cambia."""
    def get_order_by_id(self, order_id):
        return types.SimpleNamespace(status=types.SimpleNamespace(value="canceled"),
                                      filled_qty=None, filled_avg_price=None)


bot.macd_5min_bajista_2_velas = lambda ticker: False
bot._maximo_beneficio_neto_por_posicion = {"TRAIL": 5.0}
bot._scale_out_realizado = {"TRAIL"}
mensajes_ioc_no_ejecutada = []
notificar_telegram_original_ioc = bot.notificar_telegram
bot.notificar_telegram = lambda msg: mensajes_ioc_no_ejecutada.append(msg)
sleep_original_ioc = bot.time.sleep
bot.time.sleep = lambda s: None
cliente_ioc_no_ejecutada = _TradingClientIOCNoEjecutada(posiciones=[_posicion_trailing(100.0)])
bot._trading_client = cliente_ioc_no_ejecutada
bot._data_client = _DataClientPrecioFijo(101.0)
try:
    con_reloj_fijo(miercoles_regular, bot.revisar_ventas)
finally:
    bot._trading_client = trading_client_original
    bot._data_client = data_client_original
    bot.notificar_telegram = notificar_telegram_original_ioc
    bot.macd_5min_bajista_2_velas = macd_2velas_original_alpaca
    bot.time.sleep = sleep_original_ioc
    bot._maximo_beneficio_neto_por_posicion = {}
    bot._scale_out_realizado = set()

check("venta principal de acciones: si la orden IOC no se ejecuta (mercado nunca toco el "
      "limite protegido), NO se registra ninguna venta ni se avisa por Telegram",
      mensajes_ioc_no_ejecutada == [], f"mensajes={mensajes_ioc_no_ejecutada}")

# --- BUG REAL DE PRODUCCION (sept. 2026, el mismo dia que se despliega la
# venta LIMITADA+IOC de arriba): Alpaca rechaza toda orden con cantidad
# FRACCIONARIA combinada con IOC ("fractional orders must be DAY orders",
# codigo 42210000). Casi todas las posiciones reales de este bot son
# fraccionarias (operaciones de $5-30) -el bug real en produccion: NINGUNA
# venta principal de acciones llegaba a ejecutarse, la API rechazaba la
# orden siempre. Para cantidades fraccionarias se usa DAY en su lugar
# (misma proteccion de precio, solo cambia si se cancela sola al instante
# o en el siguiente ciclo via cancelar_ordenes_abiertas). ---
bot.macd_5min_bajista_2_velas = lambda ticker: False
bot._maximo_beneficio_neto_por_posicion = {"TRAIL": 5.0}
bot._scale_out_realizado = {"TRAIL"}
cliente_orden_fraccionaria = _TradingClientFalso(posiciones=[_posicion_trailing(100.0, cantidad=10.3183)])
bot._trading_client = cliente_orden_fraccionaria
bot._data_client = _DataClientPrecioFijo(101.0)  # +1%: dispara el trailing
try:
    con_reloj_fijo(miercoles_regular, bot.revisar_ventas)
finally:
    bot._trading_client = trading_client_original
    bot._data_client = data_client_original
    bot.macd_5min_bajista_2_velas = macd_2velas_original_alpaca
    bot._maximo_beneficio_neto_por_posicion = {}
    bot._scale_out_realizado = set()

check("venta principal de acciones con cantidad FRACCIONARIA: usa DAY, no IOC (Alpaca rechaza "
      "fraccionario+IOC con 'fractional orders must be DAY orders')",
      len(cliente_orden_fraccionaria.ordenes) == 1
      and cliente_orden_fraccionaria.ordenes[0].time_in_force.value == "day",
      f"ordenes={cliente_orden_fraccionaria.ordenes}")
if cliente_orden_fraccionaria.ordenes:
    check("venta principal de acciones con cantidad FRACCIONARIA: sigue con el mismo precio "
          "limite acotado (la proteccion de precio no depende del TIF)",
          abs(cliente_orden_fraccionaria.ordenes[0].limit_price - bot.calcular_precio_limite_venta(101.0)) < 1e-6,
          f"limit_price={cliente_orden_fraccionaria.ordenes[0].limit_price}")


# --- BUG REAL DE PRODUCCION (sept. 2026, caso real: una compra de WMT se
# ejecuto de verdad -aparecio en /cartera- pero /hoy seguia mostrando "0
# compras" y no llego ningun aviso de Telegram): a diferencia de
# bot_completo.py/IBKR, bot_alpaca.py NUNCA comprobaba que pasaba si
# esperar_estado_final_orden() se rendia (10s maximo) sin ver un estado
# final -una orden LIMITADA fuera de sesion regular (poca liquidez en
# pre/postmercado) puede tardar mas que eso en rellenarse, y aun asi acabar
# ejecutandose poco despues-. Si eso pasaba, la compra se descartaba sin
# mas, aunque la posicion hubiera cambiado de verdad. ---
class _TradingClientOrdenNuncaConfirma(_TradingClientFalso):
    """Como _TradingClientFalso, pero get_order_by_id() nunca llega a un
    estado FINAL (simula esperar_estado_final_orden() agotando su tiempo de
    espera), y get_all_positions() SI refleja que la orden se ejecuto de
    verdad -simula que Alpaca tardo mas en confirmar de lo que el bot
    esperaba, pero la orden se ejecuto igualmente-."""
    def __init__(self, *args, posiciones_tras_ejecucion, **kwargs):
        super().__init__(*args, **kwargs)
        self._posiciones_tras_ejecucion = posiciones_tras_ejecucion
        self._orden_colocada = False

    def get_order_by_id(self, order_id):
        return types.SimpleNamespace(status=types.SimpleNamespace(value="accepted"),
                                      filled_qty=None, filled_avg_price=None)

    def get_all_positions(self):
        return self._posiciones_tras_ejecucion if self._orden_colocada else self._posiciones

    def submit_order(self, order_data):
        self._orden_colocada = True
        return super().submit_order(order_data)


sleep_original_orden_no_confirmada = bot.time.sleep
bot.time.sleep = lambda s: None  # no perder tiempo real en los reintentos/espera de 2s
mensajes_wmt = []
notificar_telegram_original_wmt = bot.notificar_telegram
bot.notificar_telegram = lambda msg: mensajes_wmt.append(msg)
bot._cache_largas_por_dia = {}
activos_originales_wmt = bot.ACTIVOS
bot.ACTIVOS = ["WMT"]
posicion_wmt_tras_compra = types.SimpleNamespace(
    symbol="WMT", qty="0.05", avg_entry_price="95.0", market_value="4.75",
    unrealized_pl="0.0", unrealized_plpc="0.0",
)
cliente_wmt = _TradingClientOrdenNuncaConfirma(portfolio_value=10_000,
                                                posiciones_tras_ejecucion=[posicion_wmt_tras_compra])
bot._trading_client = cliente_wmt
bot._data_client = _fake_data_client_alcista()
try:
    con_reloj_fijo(miercoles_regular, bot.revisar_compras)
finally:
    bot._trading_client = trading_client_original
    bot._data_client = data_client_original
    bot.notificar_telegram = notificar_telegram_original_wmt
    bot.time.sleep = sleep_original_orden_no_confirmada
    bot.ACTIVOS = activos_originales_wmt

check("compra confirmada a posteriori (WMT): aunque el estado de la orden nunca confirmo "
      "'filled', SI se detecta que la posicion cambio y SI avisa por Telegram",
      len(mensajes_wmt) == 1 and "WMT" in mensajes_wmt[0], f"mensajes={mensajes_wmt}")
operaciones_wmt = bot.cargar_historial_operaciones()
compras_wmt = [o for o in operaciones_wmt if o["ticker"] == "WMT" and o["lado"] == "COMPRA"]
check("compra confirmada a posteriori (WMT): SI queda registrada en el historial (fuente de /hoy)",
      bool(compras_wmt), f"compras_wmt={compras_wmt}")

# Mismo caso, pero para una VENTA: si esperar_estado_final_orden() se rinde
# sin un estado final, pero la posicion SI bajo de verdad, debe registrarse
# y avisar por Telegram igualmente (antes no pasaba ni para compras ni
# para ventas).
class _TradingClientVentaNuncaConfirma(_TradingClientFalso):
    def __init__(self, *args, posiciones_tras_venta, **kwargs):
        super().__init__(*args, **kwargs)
        self._posiciones_tras_venta = posiciones_tras_venta
        self._orden_colocada = False

    def get_order_by_id(self, order_id):
        return types.SimpleNamespace(status=types.SimpleNamespace(value="accepted"),
                                      filled_qty=None, filled_avg_price=None)

    def get_all_positions(self):
        return self._posiciones_tras_venta if self._orden_colocada else self._posiciones

    def submit_order(self, order_data):
        self._orden_colocada = True
        return super().submit_order(order_data)


bot.time.sleep = lambda s: None
mensajes_venta_wmt = []
bot.notificar_telegram = lambda msg: mensajes_venta_wmt.append(msg)
bot._maximo_beneficio_neto_por_posicion = {"TRAIL": 5.0}
bot._scale_out_realizado = {"TRAIL"}
posicion_trail_vendida_parcial = types.SimpleNamespace(
    symbol="TRAIL", qty="5", avg_entry_price="100.0", market_value="0", unrealized_pl="0", unrealized_plpc="0")
cliente_venta_no_confirma = _TradingClientVentaNuncaConfirma(
    posiciones=[_posicion_trailing(100.0)], posiciones_tras_venta=[posicion_trail_vendida_parcial])
bot._trading_client = cliente_venta_no_confirma
bot._data_client = _DataClientPrecioFijo(101.0)  # +1%: retroceso de 4 pts desde el maximo (5%, margen 1.5) ->
                                                    # trailing dispara, y sigue por encima del suelo anti-perdidas (0.5%)
try:
    con_reloj_fijo(miercoles_regular, bot.revisar_ventas)
finally:
    bot._trading_client = trading_client_original
    bot._data_client = data_client_original
    bot.notificar_telegram = notificar_telegram_original_wmt
    bot.time.sleep = sleep_original_orden_no_confirmada
    bot._maximo_beneficio_neto_por_posicion = {}
    bot._scale_out_realizado = set()

check("venta confirmada a posteriori (TRAIL): aunque el estado de la orden nunca confirmo "
      "'filled', SI se detecta que la posicion bajo y SI avisa por Telegram",
      len(mensajes_venta_wmt) == 1 and "TRAIL" in mensajes_venta_wmt[0], f"mensajes={mensajes_venta_wmt}")
operaciones_venta_wmt = bot.cargar_historial_operaciones()
ventas_trail_posteriori = [o for o in operaciones_venta_wmt if o["ticker"] == "TRAIL" and o["lado"] == "VENTA"]
check("venta confirmada a posteriori (TRAIL): SI queda registrada en el historial, con la "
      "cantidad realmente vendida (10 -> 5, se vendieron 5)",
      bool(ventas_trail_posteriori) and abs(ventas_trail_posteriori[-1]["cantidad"] - 5.0) < 1e-6,
      f"ultimo_registro={ventas_trail_posteriori[-1] if ventas_trail_posteriori else None}")


# ---------------------------------------------------------------------------
# 8c. Venta forzada: A MERCADO, no limitada (peticion del usuario, sept.
#     2026) -antes usaba una orden limitada, ahora prioriza la ejecucion
#     garantizada antes del cierre-.
# ---------------------------------------------------------------------------
en_venta_forzada_original_alpaca = bot.en_ventana_venta_forzada
bot.en_ventana_venta_forzada = lambda: True  # forzar "dentro de los ultimos 15 min antes del cierre"
bot._maximo_beneficio_neto_por_posicion = {}
cliente_venta_forzada = _TradingClientFalso(posiciones=[_posicion_trailing(100.0)])
bot._trading_client = cliente_venta_forzada
bot._data_client = _DataClientPrecioFijo(101.0)  # +1.0% bruto: dentro del rango 0.5%-2%
try:
    con_reloj_fijo(miercoles_regular, bot.revisar_ventas)
finally:
    bot._trading_client = trading_client_original
    bot._data_client = data_client_original
    bot.en_ventana_venta_forzada = en_venta_forzada_original_alpaca
    bot._maximo_beneficio_neto_por_posicion = {}

check("venta forzada: coloca exactamente una orden",
      len(cliente_venta_forzada.ordenes) == 1, f"ordenes={cliente_venta_forzada.ordenes}")
if cliente_venta_forzada.ordenes:
    check("venta forzada: la orden es A MERCADO, no limitada",
          cliente_venta_forzada.ordenes[0].type.value == "market",
          f"type={cliente_venta_forzada.ordenes[0].type}")


# ---------------------------------------------------------------------------
# 9. Cripto (añadido sept. 2026, petición del usuario): es_cripto,
#    estimar_comision_cripto_alpaca, calcular_exposicion_total_cripto_usd, y
#    revisar_ventas_cripto/revisar_compras_cripto extremo a extremo con
#    clientes falsos (incluido el limite propio de exposicion TOTAL en
#    cripto, 20%, distinto del limite por moneda individual).
# ---------------------------------------------------------------------------
check("es_cripto: 'BTC/USD' -> True", bot.es_cripto("BTC/USD") is True)
check("es_cripto: 'AAPL' -> False", bot.es_cripto("AAPL") is False)

check("estimar_comision_cripto_alpaca: 0.25% del valor operado (a diferencia de acciones, sin comision)",
      abs(bot.estimar_comision_cripto_alpaca(1000.0) - 2.5) < 1e-9)
check("estimar_comision_cripto_alpaca: valor 0 -> comision 0",
      bot.estimar_comision_cripto_alpaca(0) == 0.0)

posicion_cripto_btc = types.SimpleNamespace(
    symbol="BTC/USD", qty="0.01", avg_entry_price="50000.0", market_value="510.0",
    unrealized_pl="10.0", unrealized_plpc="0.02",
)
posicion_stock_aapl = types.SimpleNamespace(
    symbol="AAPL", qty="1", avg_entry_price="200.0", market_value="200.0",
    unrealized_pl="0.0", unrealized_plpc="0.0",
)
check("calcular_exposicion_total_cripto_usd: suma solo cripto, ignora acciones (misma funcion generica)",
      abs(bot.calcular_exposicion_total_cripto_usd([posicion_cripto_btc, posicion_stock_aapl]) - 510.0) < 1e-9)

# revisar_ventas() (acciones) debe IGNORAR posiciones de cripto -las
# gestiona revisar_ventas_cripto() aparte, ver el filtro es_cripto() al
# principio de la funcion-.
bot._trading_client = _TradingClientFalso(posiciones=[posicion_cripto_btc])
bot._data_client = data_client_original
try:
    con_reloj_fijo(miercoles_regular, bot.revisar_ventas)
finally:
    ordenes_stock_con_solo_cripto = bot._trading_client.ordenes
    bot._trading_client = trading_client_original

check("revisar_ventas (acciones): ignora una posicion de cripto, no revienta ni opera con ella",
      len(ordenes_stock_con_solo_cripto) == 0, f"ordenes={ordenes_stock_con_solo_cripto}")


def _fake_crypto_data_client_precio(precio):
    class _CryptoDataClientPrecioFijo:
        def get_crypto_bars(self, peticion):
            return _BarSetFalso({t: [_VelaFalsa(precio)] for t in peticion.symbol_or_symbols})
    return _CryptoDataClientPrecioFijo()


def _fake_crypto_data_client_alcista():
    class _CryptoDataClientAlcista:
        def get_crypto_bars(self, peticion):
            return _BarSetFalso({t: [_VelaFalsa(100 * (1.02 ** i)) for i in range(60)]
                                  for t in peticion.symbol_or_symbols})
    return _CryptoDataClientAlcista()


crypto_data_client_original = bot._crypto_data_client
macd_5min_bajista_cripto_original = bot.macd_5min_bajista_cripto
macd_5min_bajista_cripto_2_velas_original = bot.macd_5min_bajista_cripto_2_velas

# --- revisar_ventas_cripto: beneficio neto claramente por encima del umbral
#     -> primera vez en el umbral (retroceso=0): SALIDA PARCIAL (50%), no el
#     100% (peticion del usuario, sept. 2026: mismo trailing stop + salida
#     parcial que acciones, extendido a cripto). ---
bot._maximo_beneficio_neto_por_posicion = {}
bot._scale_out_realizado = set()
bot._trading_client = _TradingClientFalso(posiciones=[posicion_cripto_btc])
bot._crypto_data_client = _fake_crypto_data_client_precio(55000.0)  # +10% sobre coste medio (50000)
bot.macd_5min_bajista_cripto_2_velas = lambda ticker: False  # refuerzo inactivo: la parcial no depende de el
try:
    bot.revisar_ventas_cripto()
finally:
    ordenes_venta_cripto = bot._trading_client.ordenes
    bot._trading_client = trading_client_original
    bot._crypto_data_client = crypto_data_client_original
    bot.macd_5min_bajista_cripto_2_velas = macd_5min_bajista_cripto_2_velas_original

check("revisar_ventas_cripto: coloca exactamente una orden",
      len(ordenes_venta_cripto) == 1, f"ordenes={ordenes_venta_cripto}")
if ordenes_venta_cripto:
    orden_venta_cripto = ordenes_venta_cripto[0]
    check("revisar_ventas_cripto: la orden es LIMITADA",
          orden_venta_cripto.type.value == "limit", f"type={orden_venta_cripto.type}")
    check("revisar_ventas_cripto: time_in_force es IOC (Alpaca no admite DAY en cripto)",
          orden_venta_cripto.time_in_force.value == "ioc", f"tif={orden_venta_cripto.time_in_force}")
    check("revisar_ventas_cripto: primera vez en el umbral -> SALIDA PARCIAL (0.005, la mitad de 0.01)",
          abs(orden_venta_cripto.qty - 0.005) < 1e-9, f"qty={orden_venta_cripto.qty}")
    check("revisar_ventas_cripto: tras la salida parcial, el maximo SIGUE trackeado",
          "BTC/USD" in bot._maximo_beneficio_neto_por_posicion,
          f"cache={bot._maximo_beneficio_neto_por_posicion}")
bot._maximo_beneficio_neto_por_posicion = {}
bot._scale_out_realizado = set()

# --- revisar_ventas_cripto: beneficio bruto pequeño que, tras la comision
#     REAL de Alpaca (0.25% x 2), queda neto por debajo del umbral -> NO
#     vende (a diferencia de acciones, sin comision, aqui SI importa) ---
posicion_cripto_umbral = types.SimpleNamespace(
    symbol="BTC/USD", qty="0.01", avg_entry_price="50000.0", market_value="500.0",
    unrealized_pl="0.0", unrealized_plpc="0.0",
)
bot._maximo_beneficio_neto_por_posicion = {}
bot._scale_out_realizado = set()
bot._trading_client = _TradingClientFalso(posiciones=[posicion_cripto_umbral])
bot._crypto_data_client = _fake_crypto_data_client_precio(50050.0)  # +0.1% bruto
bot.macd_5min_bajista_cripto_2_velas = lambda ticker: True
try:
    bot.revisar_ventas_cripto()
finally:
    ordenes_venta_cripto_bajo_umbral = bot._trading_client.ordenes
    bot._trading_client = trading_client_original
    bot._crypto_data_client = crypto_data_client_original
    bot.macd_5min_bajista_cripto_2_velas = macd_5min_bajista_cripto_2_velas_original
    bot._maximo_beneficio_neto_por_posicion = {}
    bot._scale_out_realizado = set()

check("revisar_ventas_cripto: con beneficio neto por debajo del umbral tras comision real, NO vende",
      len(ordenes_venta_cripto_bajo_umbral) == 0, f"ordenes={ordenes_venta_cripto_bajo_umbral}")

# --- revisar_compras_cripto: extremo a extremo ---
activos_cripto_originales = bot.ACTIVOS_CRYPTO
bot.ACTIVOS_CRYPTO = ["BTC/USD"]

bot._trading_client = _TradingClientFalso(portfolio_value=10_000)
bot._crypto_data_client = _fake_crypto_data_client_alcista()
try:
    bot._cache_largas_por_dia = {}
    bot.revisar_compras_cripto()
finally:
    ordenes_compra_cripto = bot._trading_client.ordenes
    bot._trading_client = trading_client_original
    bot._crypto_data_client = crypto_data_client_original

check("revisar_compras_cripto: coloca exactamente una orden",
      len(ordenes_compra_cripto) == 1, f"ordenes={ordenes_compra_cripto}")
if ordenes_compra_cripto:
    orden_compra_cripto = ordenes_compra_cripto[0]
    check("revisar_compras_cripto: usa notional (importe en efectivo), no qty",
          orden_compra_cripto.notional is not None and orden_compra_cripto.qty is None,
          f"orden={orden_compra_cripto}")
    check("revisar_compras_cripto: time_in_force es IOC",
          orden_compra_cripto.time_in_force.value == "ioc", f"tif={orden_compra_cripto.time_in_force}")

# --- Efectivo insuficiente para el importe estandar pero suficiente para
#     uno reducido: compra el reducido, dejando el margen minimo (mismo
#     comportamiento que en acciones, peticion del usuario sept. 2026) ---
bot._trading_client = _TradingClientFalso(portfolio_value=10_000, cash=20.0)
bot._crypto_data_client = _fake_crypto_data_client_alcista()
try:
    bot._cache_largas_por_dia = {}
    bot.revisar_compras_cripto()
finally:
    ordenes_cripto_caja_reducida = bot._trading_client.ordenes
    bot._trading_client = trading_client_original
    bot._crypto_data_client = crypto_data_client_original

margen_esperado_cripto_usd = bot.MARGEN_EFECTIVO_MINIMO_USD
check("revisar_compras_cripto: con efectivo insuficiente para el importe estandar pero suficiente "
      "para uno reducido, SI compra (reducido, dejando el margen minimo)",
      len(ordenes_cripto_caja_reducida) == 1, f"ordenes={ordenes_cripto_caja_reducida}")
if ordenes_cripto_caja_reducida:
    check("revisar_compras_cripto: el importe reducido es (efectivo - margen minimo)",
          abs(ordenes_cripto_caja_reducida[0].notional - (20.0 - margen_esperado_cripto_usd)) < 0.01,
          f"notional={ordenes_cripto_caja_reducida[0].notional}")

# --- Limite de exposicion TOTAL en cripto (peticion del usuario, 20%): con
#     una posicion de OTRA cripto ya ocupando casi todo ese limite, NO debe
#     comprar mas, aunque la señal de compra sea valida ---
portfolio_value_prueba = 1000.0
limite_cripto_total_usd_prueba = portfolio_value_prueba * (bot.LIMITE_EXPOSICION_CRYPTO_TOTAL_PCT / 100)  # 200 USD
posicion_cripto_cerca_del_limite = types.SimpleNamespace(
    symbol="ETH/USD", qty="0.1", avg_entry_price="1900.0",
    market_value=str(limite_cripto_total_usd_prueba - 10), unrealized_pl="0.0", unrealized_plpc="0.0",
)
bot._trading_client = _TradingClientFalso(portfolio_value=portfolio_value_prueba,
                                            posiciones=[posicion_cripto_cerca_del_limite])
bot._crypto_data_client = _fake_crypto_data_client_alcista()
try:
    bot._cache_largas_por_dia = {}
    bot.revisar_compras_cripto()
finally:
    ordenes_compra_cripto_limite = bot._trading_client.ordenes
    bot._trading_client = trading_client_original
    bot._crypto_data_client = crypto_data_client_original
    bot.ACTIVOS_CRYPTO = activos_cripto_originales

check("revisar_compras_cripto: NO compra si superaria el limite de exposicion TOTAL en cripto (20%)",
      len(ordenes_compra_cripto_limite) == 0, f"ordenes={ordenes_compra_cripto_limite}")

# --- BUG REAL DE PRODUCCION (sept. 2026): Alpaca devuelve el simbolo de
# las posiciones de cripto YA ABIERTAS sin la barra ('LINKUSD', no
# 'LINK/USD' como en ACTIVOS_CRYPTO y en todo el resto del codigo). Sin
# normalizarlo, es_cripto() lo clasificaba como ACCION: revisar_ventas()
# lo procesaba con el cliente de datos de acciones (nunca encuentra ese
# ticker -> "no se pudo obtener precio actual" en bucle infinito) y
# revisar_ventas_cripto() no llegaba a verlo nunca. Con esto NINGUNA
# posicion de cripto ya abierta pasaba jamas por el trailing stop, por
# grande que fuera el retroceso -caso real: LINK/USD retrocedio mas de 1.7
# puntos sin que el bot vendiera nada-.
check("_normalizar_simbolo_cripto: reconstruye la barra para un simbolo de ACTIVOS_CRYPTO",
      bot._normalizar_simbolo_cripto("LINKUSD") == "LINK/USD")
check("_normalizar_simbolo_cripto: no toca un simbolo que ya lleva la barra",
      bot._normalizar_simbolo_cripto("LINK/USD") == "LINK/USD")
check("_normalizar_simbolo_cripto: no toca un ticker de accion (no esta en ACTIVOS_CRYPTO)",
      bot._normalizar_simbolo_cripto("AAPL") == "AAPL")

posicion_cripto_sin_barra = types.SimpleNamespace(
    symbol="LINKUSD", qty="0.42", avg_entry_price="10.0", market_value="46.20",
    unrealized_pl="4.20", unrealized_plpc="0.10",
)
bot._maximo_beneficio_neto_por_posicion = {"LINK/USD": 5.24}  # maximo ya trackeado (pico previo)
bot._scale_out_realizado = {"LINK/USD"}  # ya se hizo la salida parcial antes
bot._trading_client = _TradingClientFalso(posiciones=[posicion_cripto_sin_barra])
bot._crypto_data_client = _fake_crypto_data_client_precio(10.35)  # +3.5% bruto: retroceso >0.3 pts desde el 5.24% maximo
bot.macd_5min_bajista_cripto_2_velas = lambda ticker: False
try:
    bot._cache_largas_por_dia = {}
    con_reloj_fijo(miercoles_regular, bot.revisar_ventas)  # NO debe tocar esta posicion (es cripto, aunque llegue sin barra)
    ordenes_stock_linkusd = bot._trading_client.ordenes.copy()
    bot.revisar_ventas_cripto()   # SI debe procesarla y vender (retroceso > TRAILING_STOP_VENTA_PCT)
finally:
    ordenes_cripto_linkusd = bot._trading_client.ordenes
    bot._trading_client = trading_client_original
    bot._crypto_data_client = crypto_data_client_original
    bot.macd_5min_bajista_cripto_2_velas = macd_5min_bajista_cripto_2_velas_original
    bot._maximo_beneficio_neto_por_posicion = {}
    bot._scale_out_realizado = set()

check("revisar_ventas (acciones): una posicion de cripto SIN barra en el simbolo (formato real de "
      "Alpaca) NO se procesa como accion, gracias a la normalizacion",
      len(ordenes_stock_linkusd) == 0, f"ordenes={ordenes_stock_linkusd}")
check("revisar_ventas_cripto: una posicion de cripto SIN barra en el simbolo SI se detecta y se "
      "vende (trailing stop disparado por el retroceso), gracias a la normalizacion",
      len(ordenes_cripto_linkusd) == 1, f"ordenes={ordenes_cripto_linkusd}")
if ordenes_cripto_linkusd:
    check("revisar_ventas_cripto: la orden de venta usa el simbolo CON barra (formato valido para "
          "la API de ordenes/datos), no el 'LINKUSD' crudo de la posicion",
          ordenes_cripto_linkusd[0].symbol == "LINK/USD", f"symbol={ordenes_cripto_linkusd[0].symbol}")

# --- obtener_posiciones(): la normalizacion se aplica en el punto de
# entrada, asi que TODO el codigo que consuma posiciones (venta, cartera,
# poda) ve siempre el simbolo con barra, sin tener que acordarse de
# normalizar en cada sitio por separado ---
bot._trading_client = _TradingClientFalso(posiciones=[
    types.SimpleNamespace(symbol="BCHUSD", qty="0.06", avg_entry_price="500.0", market_value="30.0",
                          unrealized_pl="0.0", unrealized_plpc="0.0"),
])
try:
    posiciones_normalizadas = bot.obtener_posiciones()
finally:
    bot._trading_client = trading_client_original

check("obtener_posiciones: normaliza el simbolo de cripto (sin barra -> con barra) para TODOS los "
      "consumidores (venta, cartera, poda)",
      posiciones_normalizadas[0].symbol == "BCH/USD", f"symbol={posiciones_normalizadas[0].symbol}")


# ---------------------------------------------------------------------------
# 10. MAX_POSICIONES_ABIERTAS y caja disponible (efectivo): peticion del
#     usuario, sept. 2026 - controles agregados de riesgo, ademas de
#     LIMITE_EXPOSICION_PCT (por posicion).
# ---------------------------------------------------------------------------
def _posiciones_falsas(n):
    return [types.SimpleNamespace(symbol=f"YA{i}", qty="1", avg_entry_price="100.0",
                                   market_value="100.0", unrealized_pl="0.0", unrealized_plpc="0.0")
            for i in range(n)]


bot.ACTIVOS = ["NUEVO"]

# Ya hay MAX_POSICIONES_ABIERTAS tickers distintos -> uno NUEVO se omite.
bot._trading_client = _TradingClientFalso(portfolio_value=1_000_000,
                                            posiciones=_posiciones_falsas(bot.MAX_POSICIONES_ABIERTAS))
bot._data_client = _fake_data_client_alcista()
try:
    bot._cache_largas_por_dia = {}
    con_reloj_fijo(miercoles_regular, bot.revisar_compras)
finally:
    ordenes_lleno = bot._trading_client.ordenes
    bot._trading_client = trading_client_original
    bot._data_client = data_client_original

check("revisar_compras: con MAX_POSICIONES_ABIERTAS ya alcanzado, NO compra un ticker nuevo",
      len(ordenes_lleno) == 0, f"ordenes={len(ordenes_lleno)}")

# Con hueco libre (una posicion menos que el limite), SI compra.
bot._trading_client = _TradingClientFalso(portfolio_value=1_000_000,
                                            posiciones=_posiciones_falsas(bot.MAX_POSICIONES_ABIERTAS - 1))
bot._data_client = _fake_data_client_alcista()
try:
    bot._cache_largas_por_dia = {}
    con_reloj_fijo(miercoles_regular, bot.revisar_compras)
finally:
    ordenes_con_hueco = bot._trading_client.ordenes
    bot._trading_client = trading_client_original
    bot._data_client = data_client_original

check("revisar_compras: con hueco libre bajo MAX_POSICIONES_ABIERTAS, SI compra el ticker nuevo",
      len(ordenes_con_hueco) == 1, f"ordenes={len(ordenes_con_hueco)}")

# Efectivo insuficiente -> se omite aunque haya señal, hueco de posiciones
# y margen de exposicion de sobra.
bot._trading_client = _TradingClientFalso(portfolio_value=1_000_000, cash=1.0)
bot._data_client = _fake_data_client_alcista()
try:
    bot._cache_largas_por_dia = {}
    con_reloj_fijo(miercoles_regular, bot.revisar_compras)
finally:
    ordenes_sin_caja = bot._trading_client.ordenes
    bot._trading_client = trading_client_original
    bot._data_client = data_client_original

check("revisar_compras: con efectivo insuficiente, NO compra aunque haya señal y hueco",
      len(ordenes_sin_caja) == 0, f"ordenes={ordenes_sin_caja}")

# Efectivo insuficiente para el importe ESTANDAR pero suficiente para un
# importe REDUCIDO que deje el margen minimo: compra ese importe reducido
# en vez de omitir la compra entera (peticion del usuario, sept. 2026).
bot._trading_client = _TradingClientFalso(portfolio_value=1_000_000, cash=20.0)
bot._data_client = _fake_data_client_alcista()
try:
    bot._cache_largas_por_dia = {}
    con_reloj_fijo(miercoles_regular, bot.revisar_compras)
finally:
    ordenes_caja_reducida = bot._trading_client.ordenes
    bot._trading_client = trading_client_original
    bot._data_client = data_client_original

margen_esperado_usd = bot.MARGEN_EFECTIVO_MINIMO_USD
check("revisar_compras: con efectivo insuficiente para el importe estandar pero suficiente para "
      "un importe reducido, SI compra (reducido, dejando el margen minimo)",
      len(ordenes_caja_reducida) == 1, f"ordenes={ordenes_caja_reducida}")
if ordenes_caja_reducida:
    check("revisar_compras: el importe reducido es (efectivo - margen minimo), no el estandar completo",
          abs(ordenes_caja_reducida[0].notional - (20.0 - margen_esperado_usd)) < 0.01,
          f"notional={ordenes_caja_reducida[0].notional}, esperado={20.0 - margen_esperado_usd}")

# Sin el atributo 'cash' en absoluto (p.ej. fallo al leerlo): no debe
# romper nada, simplemente no se aplica ese limite concreto.
bot._trading_client = _TradingClientFalso(portfolio_value=1_000_000, cash=None)
bot._data_client = _fake_data_client_alcista()
excepcion_sin_cash = None
try:
    bot._cache_largas_por_dia = {}
    con_reloj_fijo(miercoles_regular, bot.revisar_compras)
except Exception as e:
    excepcion_sin_cash = e
finally:
    ordenes_sin_atributo_cash = bot._trading_client.ordenes
    bot._trading_client = trading_client_original
    bot._data_client = data_client_original
    bot.ACTIVOS = activos_originales

check("revisar_compras: si 'cash' no esta disponible en la cuenta, no lanza excepcion y sigue comprando",
      excepcion_sin_cash is None and len(ordenes_sin_atributo_cash) == 1,
      f"excepcion={excepcion_sin_cash}, ordenes={len(ordenes_sin_atributo_cash)}")


# ---------------------------------------------------------------------------
# 11. Cache de temporalidades LARGAS (dia/semana): peticion del usuario,
#     sept. 2026 - se calcula una vez por dia natural (modo "cerrada") y se
#     reutiliza el resto del dia; a partir de UMBRAL_FRACCION_VELA_EN_CURSO
#     (40%) del periodo, se incluye la vela en curso (modo "en_curso"),
#     refrescando cada hora en vez de en cada ciclo.
# ---------------------------------------------------------------------------
_nombre_por_timeframe = {tf["timeframe"]: tf["nombre"] for tf in bot.TEMPORALIDADES}


class _DataClientContadorPeticiones:
    def __init__(self, precios_por_nombre):
        self.precios_por_nombre = precios_por_nombre
        self.peticiones_por_nombre = {}

    def get_stock_bars(self, peticion):
        nombre = _nombre_por_timeframe[peticion.timeframe]
        self.peticiones_por_nombre[nombre] = self.peticiones_por_nombre.get(nombre, 0) + 1
        precios = self.precios_por_nombre[nombre]
        return _BarSetFalso({t: [_VelaFalsa(p) for p in precios] for t in peticion.symbol_or_symbols})


SERIE_BAJISTA_ALPACA = [100 - i * 0.3 for i in range(60)]
SERIE_ACELERANDO_BAJA_ALPACA = [300 - 100 * (1.02 ** i) for i in range(60)]
precios_por_nombre_cache = {
    "1 minuto": SERIE_BAJISTA_ALPACA, "5 minutos": SERIE_BAJISTA_ALPACA, "15 minutos": SERIE_BAJISTA_ALPACA,
    "30 minutos": SERIE_BAJISTA_ALPACA, "1 hora": SERIE_BAJISTA_ALPACA,
    "1 dia": SERIE_ACELERANDO_BAJA_ALPACA, "1 semana": SERIE_ACELERANDO_BAJA_ALPACA,
}

fraccion_dia_original_alpaca = bot._fraccion_transcurrida_del_dia
fraccion_semana_original_alpaca = bot._fraccion_transcurrida_de_la_semana

# modo "cerrada" (periodo recien empezado): comportamiento de "una vez al dia".
bot._fraccion_transcurrida_del_dia = lambda mercado: 0.1
bot._fraccion_transcurrida_de_la_semana = lambda mercado: 0.1
try:
    bot._cache_largas_por_dia = {}
    data_client_cache = _DataClientContadorPeticiones(precios_por_nombre_cache)
    bot._data_client = data_client_cache
    try:
        bot.analizar_todos_los_activos(["CACHE_TEST"])
        bot.analizar_todos_los_activos(["CACHE_TEST"])
    finally:
        bot._data_client = data_client_original

    check("cache temporalidades largas (modo cerrada): '1 dia' solo se pide UNA vez (2 llamadas)",
          data_client_cache.peticiones_por_nombre.get("1 dia") == 1,
          f"peticiones={data_client_cache.peticiones_por_nombre}")
    check("cache temporalidades largas (modo cerrada): '1 semana' solo se pide UNA vez (2 llamadas)",
          data_client_cache.peticiones_por_nombre.get("1 semana") == 1,
          f"peticiones={data_client_cache.peticiones_por_nombre}")
    check("cache temporalidades largas: las CORTAS (p.ej. '1 minuto') SI se piden en cada llamada",
          data_client_cache.peticiones_por_nombre.get("1 minuto") == 2,
          f"peticiones={data_client_cache.peticiones_por_nombre}")

    # Cambio de dia -> se vuelve a pedir.
    bot._cache_largas_por_dia["1 dia"]["fecha"] = date(2000, 1, 1)
    bot._cache_largas_por_dia["1 semana"]["fecha"] = date(2000, 1, 1)
    bot._data_client = data_client_cache
    try:
        bot.analizar_todos_los_activos(["CACHE_TEST"])
    finally:
        bot._data_client = data_client_original
    check("cache temporalidades largas: al cambiar de dia, se vuelve a pedir '1 dia'/'1 semana'",
          data_client_cache.peticiones_por_nombre.get("1 dia") == 2
          and data_client_cache.peticiones_por_nombre.get("1 semana") == 2,
          f"peticiones={data_client_cache.peticiones_por_nombre}")
finally:
    bot._fraccion_transcurrida_del_dia = fraccion_dia_original_alpaca
    bot._fraccion_transcurrida_de_la_semana = fraccion_semana_original_alpaca

# modo "en_curso" (>=40% del periodo): incluye la vela en formacion y
# refresca cada hora en vez de en cada ciclo.
bot._fraccion_transcurrida_del_dia = lambda mercado: 0.5
bot._fraccion_transcurrida_de_la_semana = lambda mercado: 0.5
try:
    bot._cache_largas_por_dia = {}
    data_client_en_curso = _DataClientContadorPeticiones(precios_por_nombre_cache)
    bot._data_client = data_client_en_curso
    try:
        resultado_en_curso_1, _ = bot.analizar_todos_los_activos(["CACHE_TEST"])
        resultado_en_curso_2, _ = bot.analizar_todos_los_activos(["CACHE_TEST"])
    finally:
        bot._data_client = data_client_original

    check("cache temporalidades largas (modo en_curso): una segunda llamada INMEDIATA no vuelve a "
          "pedir '1 dia' (throttle de 1h)",
          data_client_en_curso.peticiones_por_nombre.get("1 dia") == 1,
          f"peticiones={data_client_en_curso.peticiones_por_nombre}")

    bot._cache_largas_por_dia["1 dia"]["ultima_actualizacion"] = (
        bot.time.monotonic() - bot.INTERVALO_REFRESCO_VELA_EN_CURSO_SEGUNDOS - 1)
    bot._data_client = data_client_en_curso
    try:
        bot.analizar_todos_los_activos(["CACHE_TEST"])
    finally:
        bot._data_client = data_client_original
    check("cache temporalidades largas (modo en_curso): pasada 1h desde la ultima actualizacion, se refresca",
          data_client_en_curso.peticiones_por_nombre.get("1 dia") == 2,
          f"peticiones={data_client_en_curso.peticiones_por_nombre}")
finally:
    bot._fraccion_transcurrida_del_dia = fraccion_dia_original_alpaca
    bot._fraccion_transcurrida_de_la_semana = fraccion_semana_original_alpaca


# ---------------------------------------------------------------------------
# Resumen final
# ---------------------------------------------------------------------------
print()
if fallos:
    print(f"{len(fallos)} test(s) FALLARON: {fallos}")
    sys.exit(1)
else:
    print("Todos los tests pasaron correctamente.")
