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
bot.obtener_posiciones = lambda: []
check("formatear_posiciones_abiertas sin posiciones -> '(ninguna)'",
      "(ninguna)" in cartera.formatear_posiciones_abiertas())

bot.obtener_posiciones = lambda: [FakePos("AAPL", 3.5, 100.0, 385.0, 35.0, 0.10)]
resultado_plano = cartera.formatear_posiciones_abiertas(html=False)
check("formatear_posiciones_abiertas (plano): incluye el ticker y el signo +",
      "AAPL" in resultado_plano and "+35,00" in resultado_plano, f"resultado={resultado_plano!r}")
check("formatear_posiciones_abiertas (plano): formato español (coma decimal)",
      "35,00" in resultado_plano and "35.00" not in resultado_plano)

resultado_html = cartera.formatear_posiciones_abiertas(html=True)
check("formatear_posiciones_abiertas (html): usa <pre> y negrita",
      "<pre>" in resultado_html and "<b>" in resultado_html)
bot.obtener_posiciones = obtener_posiciones_original


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
      "1 operaciones, 5,00 acciones" in actividad_hoy, f"resultado={actividad_hoy!r}")
check("formatear_actividad hoy: NO cuenta la compra de TSLA de ayer",
      "TSLA" not in actividad_hoy)

actividad_rango_completo = cartera.formatear_actividad(ayer_dt.date(), hoy_dt.date())
check("formatear_actividad con rango de 2 dias: cuenta las 3 compras (5,5 + 1 = 6,5 acciones)",
      "3 operaciones, 6,50 acciones" in actividad_rango_completo, f"resultado={actividad_rango_completo!r}")

actividad_vacia = cartera.formatear_actividad(date(2000, 1, 1), date(2000, 1, 1))
check("formatear_actividad sin operaciones en el rango -> 0 y 0",
      "0 operaciones, 0,00 acciones" in actividad_vacia, f"resultado={actividad_vacia!r}")


if fallos:
    print(f"\n{len(fallos)} test(s) FALLARON: {fallos}")
    sys.exit(1)
else:
    print("\nTodos los tests pasaron correctamente.")
