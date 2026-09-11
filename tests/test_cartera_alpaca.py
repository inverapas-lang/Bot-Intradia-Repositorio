"""
Tests manuales (sin pytest) para cartera_alpaca.py: calculo de rangos de
fecha y las funciones formatear_*() con datos de prueba (posiciones y
operaciones falsas, sin llamar a la API real de Alpaca).
"""
import json
import os
import sys
import tempfile
import types
from datetime import date, datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("ALPACA_API_KEY", "test-key")
os.environ.setdefault("ALPACA_SECRET_KEY", "test-secret")

import bot_alpaca as bot
import cartera_alpaca as cartera

_DIR_TEMP = tempfile.mkdtemp()
bot.ARCHIVO_LATIDO = os.path.join(_DIR_TEMP, "latido_test.txt")
bot.ARCHIVO_PID = os.path.join(_DIR_TEMP, "test.pid")
bot.ARCHIVO_HISTORIAL_OPERACIONES = os.path.join(_DIR_TEMP, "historial_test.json")

fallos = []


def check(nombre, condicion, detalle=""):
    estado = "OK  " if condicion else "FAIL"
    print(f"[{estado}] {nombre}" + (f" -- {detalle}" if detalle and not condicion else ""))
    if not condicion:
        fallos.append(nombre)


# ---------------------------------------------------------------------------
# 1. calcular_rango
# ---------------------------------------------------------------------------
Args = lambda ayer=False, semana=False, desde=None, hasta=None: types.SimpleNamespace(
    ayer=ayer, semana=semana, desde=desde, hasta=hasta)

hoy = date.today()
check("calcular_rango por defecto -> (hoy, hoy)", cartera.calcular_rango(Args()) == (hoy, hoy))
check("calcular_rango --ayer -> (ayer, ayer)",
      cartera.calcular_rango(Args(ayer=True)) == (hoy - timedelta(days=1), hoy - timedelta(days=1)))
lunes_actual = hoy - timedelta(days=hoy.weekday())
check("calcular_rango --semana -> (lunes de esta semana, hoy)",
      cartera.calcular_rango(Args(semana=True)) == (lunes_actual, hoy))
check("calcular_rango --desde/--hasta -> rango explicito",
      cartera.calcular_rango(Args(desde="2026-08-01", hasta="2026-08-15")) == (date(2026, 8, 1), date(2026, 8, 15)))
check("calcular_rango --desde sin --hasta -> hasta = hoy",
      cartera.calcular_rango(Args(desde="2026-08-01")) == (date(2026, 8, 1), hoy))


# ---------------------------------------------------------------------------
# 2. formatear_posiciones_abiertas
# ---------------------------------------------------------------------------
class FakePos:
    def __init__(self, symbol, qty, avg, market_value, unrealized_pl, unrealized_plpc):
        self.symbol = symbol
        self.qty = str(qty)
        self.avg_entry_price = str(avg)
        self.market_value = str(market_value)
        self.unrealized_pl = str(unrealized_pl)
        self.unrealized_plpc = str(unrealized_plpc)


obtener_posiciones_original = bot.obtener_posiciones
bot.obtener_posiciones = lambda client=None: []
check("formatear_posiciones_abiertas sin posiciones -> '(ninguna)'",
      "(ninguna)" in cartera.formatear_posiciones_abiertas())

bot.obtener_posiciones = lambda client=None: [FakePos("AAPL", 3.5, 100.0, 385.0, 35.0, 0.10)]
resultado_plano = cartera.formatear_posiciones_abiertas(html=False)
check("formatear_posiciones_abiertas (plano): incluye el ticker y el signo +",
      "AAPL" in resultado_plano and "+35,00" in resultado_plano, f"resultado={resultado_plano!r}")
check("formatear_posiciones_abiertas (plano): formato español (coma decimal)",
      "35,00" in resultado_plano and "35.00" not in resultado_plano)

resultado_html = cartera.formatear_posiciones_abiertas(html=True)
check("formatear_posiciones_abiertas (html): usa <pre> y negrita",
      "<pre>" in resultado_html and "<b>" in resultado_html)

# El titulo debe indicar el modo ACTUAL de conexion del bot (peticion del
# usuario, sept. 2026): las posiciones abiertas vienen en vivo de la API,
# siempre en el modo con el que esta conectado ahora mismo.
paper_original = bot.ALPACA_PAPER
bot.ALPACA_PAPER = True
check("formatear_posiciones_abiertas: titulo indica [PAPER] cuando ALPACA_PAPER=True",
      "[PAPER]" in cartera.formatear_posiciones_abiertas(),
      f"resultado={cartera.formatear_posiciones_abiertas()!r}")
bot.ALPACA_PAPER = False
check("formatear_posiciones_abiertas: titulo indica [REAL] cuando ALPACA_PAPER=False",
      "[REAL]" in cartera.formatear_posiciones_abiertas(),
      f"resultado={cartera.formatear_posiciones_abiertas()!r}")
bot.ALPACA_PAPER = paper_original
bot.obtener_posiciones = obtener_posiciones_original


# --- Mismo formato de tabla que las operaciones CERRADAS (peticion del
# usuario, sept. 2026): Ticker + Cant. (max 4 decimales) + Precio (actual) +
# % + USD, con "abierta desde" junto al emoji de modo debajo de cada fila,
# y linea en blanco entre posiciones. ---
compra_wfc_hace_7d = (datetime.now() - timedelta(days=7, hours=3)).isoformat(timespec="seconds")
with open(bot.ARCHIVO_HISTORIAL_OPERACIONES, "w") as f:
    json.dump([{"fecha_hora": compra_wfc_hace_7d, "ticker": "WFC", "lado": "COMPRA",
                "cantidad": 0.058288282, "precio": 89.90, "modo": "REAL"}], f)

bot.ALPACA_PAPER = False
bot.obtener_posiciones = lambda client=None: [FakePos("WFC", 0.058288282, 89.90, 5.35, 0.15, 0.0288)]
resultado_abiertas_formato = cartera.formatear_posiciones_abiertas(html=True)
bot.obtener_posiciones = obtener_posiciones_original
bot.ALPACA_PAPER = paper_original

check("formatear_posiciones_abiertas (html): la cabecera coincide con la de operaciones "
      "cerradas (Ticker/Cant./Precio/%/USD)",
      "Ticker" in resultado_abiertas_formato and "Cant." in resultado_abiertas_formato
      and "Precio" in resultado_abiertas_formato,
      f"resultado={resultado_abiertas_formato!r}")
check("formatear_posiciones_abiertas (html): la cantidad se redondea a maximo 4 decimales",
      "0.0583" in resultado_abiertas_formato and "0.058288282" not in resultado_abiertas_formato,
      f"resultado={resultado_abiertas_formato!r}")
check("formatear_posiciones_abiertas (html): muestra desde cuando esta abierta la posicion, "
      "junto al emoji de modo (💰)",
      "💰 abierta desde" in resultado_abiertas_formato and "7d 3h" in resultado_abiertas_formato,
      f"resultado={resultado_abiertas_formato!r}")


# ---------------------------------------------------------------------------
# 3. formatear_actividad: cuenta compras y ventas del rango correctamente
# ---------------------------------------------------------------------------
hoy_dt = datetime.now()
ayer_dt = hoy_dt - timedelta(days=1)
operaciones_prueba = [
    {"fecha_hora": hoy_dt.isoformat(timespec="seconds"), "ticker": "AAPL", "lado": "COMPRA", "cantidad": 3.5, "precio": 100.0},
    {"fecha_hora": hoy_dt.isoformat(timespec="seconds"), "ticker": "MSFT", "lado": "COMPRA", "cantidad": 2.0, "precio": 400.0},
    {"fecha_hora": hoy_dt.isoformat(timespec="seconds"), "ticker": "NVDA", "lado": "VENTA", "cantidad": 5.0, "precio": 110.0,
     "coste_medio": 100.0, "beneficio_pct": 10.0},
    {"fecha_hora": ayer_dt.isoformat(timespec="seconds"), "ticker": "TSLA", "lado": "COMPRA", "cantidad": 1.0, "precio": 300.0},
]
with open(bot.ARCHIVO_HISTORIAL_OPERACIONES, "w") as f:
    json.dump(operaciones_prueba, f)

actividad_hoy = cartera.formatear_actividad(hoy_dt.date(), hoy_dt.date())
check("formatear_actividad hoy: cuenta 2 compras",
      "2 operaciones" in actividad_hoy and "5,50 acciones" in actividad_hoy, f"resultado={actividad_hoy!r}")
check("formatear_actividad hoy: cuenta 1 venta",
      "1 operaciones" in actividad_hoy and "5,00 acciones" in actividad_hoy, f"resultado={actividad_hoy!r}")
check("formatear_actividad hoy: NO cuenta la compra de TSLA de ayer",
      "TSLA" not in actividad_hoy)

actividad_rango_completo = cartera.formatear_actividad(ayer_dt.date(), hoy_dt.date())
check("formatear_actividad con rango de 2 dias: cuenta las 3 compras (5,5 + 1 = 6,5 acciones)",
      "3 operaciones" in actividad_rango_completo and "6,50 acciones" in actividad_rango_completo,
      f"resultado={actividad_rango_completo!r}")

actividad_vacia = cartera.formatear_actividad(date(2000, 1, 1), date(2000, 1, 1))
check("formatear_actividad sin operaciones en el rango -> 0 y 0",
      "0 operaciones, 0,00 acciones" in actividad_vacia, f"resultado={actividad_vacia!r}")


# ---------------------------------------------------------------------------
# 4. Distincion REAL/PAPER (peticion del usuario, sept. 2026): el historial
#    se acumula entre cambios de modo del bot, sin este campo no se podia
#    saber desde Telegram cuales operaciones fueron con dinero real.
# ---------------------------------------------------------------------------
operaciones_modo_mixto = [
    {"fecha_hora": hoy_dt.isoformat(timespec="seconds"), "ticker": "AAPL", "lado": "COMPRA",
     "cantidad": 1.0, "precio": 100.0, "modo": "REAL"},
    {"fecha_hora": hoy_dt.isoformat(timespec="seconds"), "ticker": "MSFT", "lado": "COMPRA",
     "cantidad": 2.0, "precio": 400.0, "modo": "PAPER"},
    {"fecha_hora": hoy_dt.isoformat(timespec="seconds"), "ticker": "NVDA", "lado": "VENTA",
     "cantidad": 5.0, "precio": 110.0, "coste_medio": 100.0, "beneficio_pct": 10.0, "modo": "REAL"},
    # Operacion SIN campo "modo" (anterior a que existiera este campo):
    # debe tratarse como PAPER, el unico modo que existia entonces.
    {"fecha_hora": hoy_dt.isoformat(timespec="seconds"), "ticker": "TSLA", "lado": "VENTA",
     "cantidad": 1.0, "precio": 300.0, "coste_medio": 290.0, "beneficio_pct": 3.4},
]
with open(bot.ARCHIVO_HISTORIAL_OPERACIONES, "w") as f:
    json.dump(operaciones_modo_mixto, f)

actividad_mixta = cartera.formatear_actividad(hoy_dt.date(), hoy_dt.date())
check("formatear_actividad: distingue compras REAL de PAPER",
      "1 REAL" in actividad_mixta and "1 PAPER" in actividad_mixta, f"resultado={actividad_mixta!r}")
check("formatear_actividad: una operacion de venta SIN campo 'modo' cuenta como PAPER",
      "1 REAL, 1 PAPER" in actividad_mixta or "2 PAPER" in actividad_mixta,
      f"resultado={actividad_mixta!r}")

cerradas_plano = cartera.formatear_operaciones_cerradas(hoy_dt.date(), hoy_dt.date())
check("formatear_operaciones_cerradas (plano): etiqueta NVDA como [REAL]",
      "[REAL] " in cerradas_plano and "NVDA" in cerradas_plano, f"resultado={cerradas_plano!r}")
check("formatear_operaciones_cerradas (plano): TSLA sin campo 'modo' se etiqueta como [PAPER]",
      "[PAPER]" in cerradas_plano and "TSLA" in cerradas_plano, f"resultado={cerradas_plano!r}")

cerradas_html = cartera.formatear_operaciones_cerradas(hoy_dt.date(), hoy_dt.date(), html=True)
check("formatear_operaciones_cerradas (html): usa el emoji de REAL (💰) y de PAPER (🧪)",
      "💰" in cerradas_html and "🧪" in cerradas_html, f"resultado={cerradas_html!r}")

# Tabla simplificada (peticion del usuario, sept. 2026): antes tenia Fecha
# completa + Ticker + Cant. + Gan. USD + Modo en columna aparte y no cabia
# en el ancho de un movil (la columna "Modo" se desbordaba a la siguiente
# linea). Ahora es Ticker + Cant. + % + USD, con el % pedido por el
# usuario, sin columna "Fecha"/"Hora" ni columna "Modo" aparte (el emoji
# de modo va pegado al final de la fila).
check("formatear_operaciones_cerradas (html): la cabecera es mas simple (Ticker/Cant./%/USD, "
      "sin columnas 'Fecha'/'Modo')",
      "%" in cerradas_html and "Cant." in cerradas_html and "Ticker" in cerradas_html
      and "Fecha" not in cerradas_html,
      f"resultado={cerradas_html!r}")
check("formatear_operaciones_cerradas (html): SI muestra el % de beneficio de cada operacion",
      "+10,00%" in cerradas_html, f"resultado={cerradas_html!r}")
check("formatear_operaciones_cerradas (html): SI muestra la cantidad vendida de cada operacion",
      "5" in cerradas_html, f"resultado={cerradas_html!r}")

# Peticion del usuario (sept. 2026): tambien quiere ver el importe TOTAL en $
# que se ha vendido (no solo la ganancia/perdida neta).
# NVDA: 5 * 110 = 550, TSLA: 1 * 300 = 300 -> total 850.
check("formatear_operaciones_cerradas (html): muestra el importe total vendido en USD",
      "Importe total vendido" in cerradas_html and "850,00" in cerradas_html,
      f"resultado={cerradas_html!r}")


# ---------------------------------------------------------------------------
# 5. formatear_operaciones_cerradas (html): precio de venta, cantidad a
#    maximo 4 decimales, tiempo abierta la posicion y % TOTAL sobre lo
#    invertido (peticion del usuario, sept. 2026, a partir de una captura
#    real de /ayer donde pidio ver esto añadido a la tabla).
# ---------------------------------------------------------------------------
compra_hace_3h58 = hoy_dt - timedelta(hours=3, minutes=58)
compra_hace_9h04 = hoy_dt - timedelta(hours=9, minutes=4)
operaciones_con_apertura = [
    {"fecha_hora": compra_hace_3h58.isoformat(timespec="seconds"), "ticker": "C", "lado": "COMPRA",
     "cantidad": 0.116369, "precio": 135.71, "modo": "REAL"},
    {"fecha_hora": hoy_dt.isoformat(timespec="seconds"), "ticker": "C", "lado": "VENTA",
     "cantidad": 0.116369, "precio": 137.71, "coste_medio": 135.71, "beneficio_pct": 1.46, "modo": "REAL"},
    {"fecha_hora": compra_hace_9h04.isoformat(timespec="seconds"), "ticker": "SMCI", "lado": "COMPRA",
     "cantidad": 0.426837, "precio": 37.35, "modo": "REAL"},
    {"fecha_hora": hoy_dt.isoformat(timespec="seconds"), "ticker": "SMCI", "lado": "VENTA",
     "cantidad": 0.426837, "precio": 37.73, "coste_medio": 37.35, "beneficio_pct": 1.00, "modo": "REAL"},
]
with open(bot.ARCHIVO_HISTORIAL_OPERACIONES, "w") as f:
    json.dump(operaciones_con_apertura, f)

cerradas_html_apertura = cartera.formatear_operaciones_cerradas(hoy_dt.date(), hoy_dt.date(), html=True)
check("formatear_operaciones_cerradas (html): la cabecera incluye la columna 'Precio'",
      "Precio" in cerradas_html_apertura, f"resultado={cerradas_html_apertura!r}")
check("formatear_operaciones_cerradas (html): muestra el precio de venta de cada operacion",
      "137,71" in cerradas_html_apertura and "37,73" in cerradas_html_apertura,
      f"resultado={cerradas_html_apertura!r}")
check("formatear_operaciones_cerradas (html): la cantidad se redondea a maximo 4 decimales "
      "(0.116369 -> 0.1164, no los 6 decimales guardados en el historial)",
      "0.1164" in cerradas_html_apertura and "0.116369" not in cerradas_html_apertura,
      f"resultado={cerradas_html_apertura!r}")
check("formatear_operaciones_cerradas (html): debajo de cada fila indica desde cuando estaba "
      "abierta la posicion (encontrando la COMPRA que la abrio en el historial completo)",
      "abierta desde" in cerradas_html_apertura and "3h 58min" in cerradas_html_apertura
      and "9h 04min" in cerradas_html_apertura,
      f"resultado={cerradas_html_apertura!r}")
# C: coste 0.116369*135.71=15.79, ganancia (137.71-135.71)*0.116369=0.23 -> +1,46%
# SMCI: coste 0.426837*37.35=15.94, ganancia (37.73-37.35)*0.426837=0.16 -> +1,00%
# TOTAL: coste=31.73, ganancia=0.39 -> +1.24% sobre lo invertido
check("formatear_operaciones_cerradas (html): el resumen TOTAL incluye el % sobre lo invertido "
      "(no solo el $/EUR neto)",
      "sobre lo invertido" in cerradas_html_apertura and "+1,24%" in cerradas_html_apertura,
      f"resultado={cerradas_html_apertura!r}")

cerradas_plano_apertura = cartera.formatear_operaciones_cerradas(hoy_dt.date(), hoy_dt.date())
check("formatear_operaciones_cerradas (plano): el TOTAL tambien incluye el % sobre lo invertido",
      "sobre lo invertido" in cerradas_plano_apertura and "+1,24%" in cerradas_plano_apertura,
      f"resultado={cerradas_plano_apertura!r}")

# Venta sin ninguna COMPRA previa en el historial (dato incompleto, p.ej. si
# el historial no llega tan atras): no debe reventar, simplemente no muestra
# la linea de "abierta desde".
with open(bot.ARCHIVO_HISTORIAL_OPERACIONES, "w") as f:
    json.dump([{"fecha_hora": hoy_dt.isoformat(timespec="seconds"), "ticker": "ZZZ", "lado": "VENTA",
                "cantidad": 1.0, "precio": 10.0, "coste_medio": 9.0, "beneficio_pct": 11.11, "modo": "REAL"}], f)
cerradas_sin_apertura = cartera.formatear_operaciones_cerradas(hoy_dt.date(), hoy_dt.date(), html=True)
check("formatear_operaciones_cerradas (html): sin COMPRA previa en el historial, no revienta "
      "y simplemente no muestra 'abierta desde'",
      "ZZZ" in cerradas_sin_apertura and "abierta desde" not in cerradas_sin_apertura,
      f"resultado={cerradas_sin_apertura!r}")

check("formatear_operaciones_cerradas (html): la fecha de apertura va en formato 'DD MES' "
      "(p.ej. '11 SEP'), no 'MM-DD'",
      "11 SEP" in cerradas_html_apertura, f"resultado={cerradas_html_apertura!r}")
check("formatear_operaciones_cerradas (html): hay una linea en blanco entre una operacion y la siguiente",
      "\n\n" in cerradas_html_apertura, f"resultado={cerradas_html_apertura!r}")

# --- BUG REAL DE PRODUCCION (sept. 2026, caso real: CVX aparecia "abierta
# desde" hace mas de una semana en Telegram, pero segun el propio historial
# de Alpaca se habia comprado esa misma mañana): _fecha_apertura_posicion()
# no distinguia PAPER de REAL, asi que una compra/venta PAPER antigua del
# mismo ticker contaminaba el calculo de la posicion REAL actual. ---
compra_cvx_paper = hoy_dt - timedelta(days=9)
venta_cvx_paper = compra_cvx_paper + timedelta(minutes=30)
compra_cvx_real = hoy_dt - timedelta(hours=5, minutes=29)
operaciones_paper_contamina = [
    {"fecha_hora": compra_cvx_paper.isoformat(timespec="seconds"), "ticker": "CVX", "lado": "COMPRA",
     "cantidad": 0.05, "precio": 200.0, "modo": "PAPER"},
    {"fecha_hora": venta_cvx_paper.isoformat(timespec="seconds"), "ticker": "CVX", "lado": "VENTA",
     "cantidad": 0.05, "precio": 201.0, "coste_medio": 200.0, "beneficio_pct": 0.5, "modo": "PAPER"},
    {"fecha_hora": compra_cvx_real.isoformat(timespec="seconds"), "ticker": "CVX", "lado": "COMPRA",
     "cantidad": 0.1505, "precio": 212.16, "modo": "REAL"},
    {"fecha_hora": hoy_dt.isoformat(timespec="seconds"), "ticker": "CVX", "lado": "VENTA",
     "cantidad": 0.1505, "precio": 213.86, "coste_medio": 212.16, "beneficio_pct": 0.82, "modo": "REAL"},
]
with open(bot.ARCHIVO_HISTORIAL_OPERACIONES, "w") as f:
    json.dump(operaciones_paper_contamina, f)
cerradas_html_cvx = cartera.formatear_operaciones_cerradas(hoy_dt.date(), hoy_dt.date(), html=True)
check("formatear_operaciones_cerradas (html): una compra/venta PAPER antigua del mismo ticker "
      "NO contamina la apertura de la posicion REAL actual (CVX: abierta hace 5h29min, no 9 dias)",
      "5h 29min" in cerradas_html_cvx and "9d" not in cerradas_html_cvx,
      f"resultado={cerradas_html_cvx!r}")


if fallos:
    print(f"\n{len(fallos)} test(s) FALLARON: {fallos}")
    sys.exit(1)
else:
    print("\nTodos los tests pasaron correctamente.")
