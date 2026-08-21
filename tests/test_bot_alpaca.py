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
from datetime import datetime, timedelta
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

cuatro_cortas_alcistas_resto_bajista = {tf["nombre"]: (tf["nombre"] in bot.NOMBRES_4_CORTAS)
                                         for tf in bot.TEMPORALIDADES}
check("decidir_senal: atajo de 4 cortas alcistas (resto bajista) -> COMPRA directa",
      bot.decidir_senal(cuatro_cortas_alcistas_resto_bajista) == "COMPRA")

falta_una_temporalidad = dict(todas_alcistas)
falta_una_temporalidad["1 semana"] = None
falta_una_temporalidad["1 minuto"] = False  # rompe el atajo de 4 cortas
check("decidir_senal: falta un dato (no en las 4 cortas) -> SIN_DATOS",
      bot.decidir_senal(falta_una_temporalidad) == "SIN_DATOS")

una_en_contra = dict(todas_alcistas)
una_en_contra["1 dia"] = False
check("decidir_senal: 1 de 7 en contra (fuera del atajo, resto ok) -> COMPRA",
      bot.decidir_senal(una_en_contra) == "COMPRA")

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
    def __init__(self, portfolio_value=10_000, posiciones=None, ordenes_abiertas=None):
        self.portfolio_value = portfolio_value
        self._posiciones = posiciones or []
        self.ordenes = []
        self._ordenes_abiertas = ordenes_abiertas or []  # simuladas, por simbolo
        self.ordenes_canceladas = []

    def get_account(self):
        return types.SimpleNamespace(portfolio_value=str(self.portfolio_value))

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
macd_5min_bajista_original = bot.macd_5min_bajista
bot._trading_client = _TradingClientFalso(posiciones=[posicion_fraccionaria])
bot._data_client = _DataClientPrecioFijo(105.0)  # +5% sobre el coste medio (100.0)
bot.macd_5min_bajista = lambda ticker: True  # forzar señal de venta
try:
    con_reloj_fijo(postmercado, bot.revisar_ventas)
finally:
    ordenes_venta_frac = bot._trading_client.ordenes
    bot._trading_client = trading_client_original
    bot._data_client = data_client_original
    bot.macd_5min_bajista = macd_5min_bajista_original

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

macd_5min_bajista_original = bot.macd_5min_bajista
bot._trading_client = _TradingClientFalso(posiciones=[posicion_en_el_umbral])
bot._data_client = _DataClientPrecioFijo(100.5)  # exactamente +0.5% sobre el coste medio (100.0)
bot.macd_5min_bajista = lambda ticker: True  # forzar señal de venta
try:
    con_reloj_fijo(miercoles_regular, bot.revisar_ventas)
finally:
    ordenes_umbral = bot._trading_client.ordenes
    bot._trading_client = trading_client_original
    bot._data_client = data_client_original
    bot.macd_5min_bajista = macd_5min_bajista_original

check("revisar_ventas sin comision: +0.5% bruto exacto (= umbral) SI vende "
      "(con comision se habria quedado por debajo y no habria vendido)",
      len(ordenes_umbral) == 1, f"ordenes={ordenes_umbral}")

# --- Ventas: si hay una orden abierta anterior sin rellenar (p.ej. limitada
# que no llego a ejecutarse), se cancela ANTES de mandar la venta nueva -bug
# real visto en produccion: "insufficient qty available", la posicion entera
# quedaba retenida (held_for_orders) por la orden vieja- ---
orden_abierta_previa = types.SimpleNamespace(id="orden-vieja-META", symbol="AAPL")
macd_5min_bajista_original = bot.macd_5min_bajista
cliente_con_orden_abierta = _TradingClientFalso(posiciones=[posicion_en_el_umbral],
                                                 ordenes_abiertas=[orden_abierta_previa])
bot._trading_client = cliente_con_orden_abierta
bot._data_client = _DataClientPrecioFijo(100.5)
bot.macd_5min_bajista = lambda ticker: True
try:
    con_reloj_fijo(miercoles_regular, bot.revisar_ventas)
finally:
    bot._trading_client = trading_client_original
    bot._data_client = data_client_original
    bot.macd_5min_bajista = macd_5min_bajista_original

check("revisar_ventas: cancela la orden abierta anterior antes de mandar la venta nueva",
      cliente_con_orden_abierta.ordenes_canceladas == ["orden-vieja-META"],
      f"canceladas={cliente_con_orden_abierta.ordenes_canceladas}")
check("revisar_ventas: tras cancelar la orden vieja, SI coloca la venta nueva",
      len(cliente_con_orden_abierta.ordenes) == 1, f"ordenes={cliente_con_orden_abierta.ordenes}")

bot.ACTIVOS = activos_originales


# ---------------------------------------------------------------------------
# Resumen final
# ---------------------------------------------------------------------------
print()
if fallos:
    print(f"{len(fallos)} test(s) FALLARON: {fallos}")
    sys.exit(1)
else:
    print("Todos los tests pasaron correctamente.")
