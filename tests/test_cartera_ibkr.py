"""
Tests manuales (sin pytest) para cartera_ibkr.py: el bug real de produccion
de sept. 2026 en formatear_posiciones_abiertas() (contrato CRYPTO con
exchange vacio), y el formato de tabla (igual al de cartera_alpaca.py,
peticion del usuario sept. 2026). No hace ninguna llamada de red real a IBKR.
"""
import json
import os
import sys
import tempfile
import types
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import cartera_ibkr as cartera
import bot_completo as bot

_DIR_TEMP = tempfile.mkdtemp()
bot.ARCHIVO_HISTORIAL_OPERACIONES = os.path.join(_DIR_TEMP, "historial_test.json")

fallos = []


def check(nombre, condicion, detalle=""):
    estado = "OK  " if condicion else "FAIL"
    print(f"[{estado}] {nombre}" + (f" -- {detalle}" if detalle and not condicion else ""))
    if not condicion:
        fallos.append(nombre)


class _ContratoCriptoSinExchange:
    def __init__(self, symbol):
        self.symbol = symbol
        self.currency = "USD"
        self.secType = "CRYPTO"
        self.exchange = ""  # tal cual lo devuelve ib.positions() para CRYPTO


class _PosicionCriptoSinExchange:
    def __init__(self, symbol, position, avgCost):
        self.contract = _ContratoCriptoSinExchange(symbol)
        self.position = position
        self.avgCost = avgCost


class _IBFalsoCarteraCripto:
    def __init__(self):
        self.qualify_llamado_con = None

    def reqPositions(self):
        pass

    def sleep(self, segundos):
        pass

    def positions(self):
        return [_PosicionCriptoSinExchange("BTC", 0.0002, 80000.0)]

    def qualifyContracts(self, contrato):
        # Simula que IBKR resuelve el exchange real a partir del conId de
        # la posicion, igual que en produccion.
        self.qualify_llamado_con = contrato
        contrato.exchange = bot.EXCHANGE_CRYPTO

    def reqHistoricalData(self, contrato, **kwargs):
        return [types.SimpleNamespace(close=81000.0)]


ib_falso = _IBFalsoCarteraCripto()
resultado = cartera.formatear_posiciones_abiertas(ib_falso)

check("formatear_posiciones_abiertas: rellena el exchange vacio de una posicion CRYPTO "
      "antes de pedir sus velas (bug real: reqHistoricalData se quedaba sin datos, "
      "error 366 'No historical data query found')",
      ib_falso.qualify_llamado_con is not None, f"qualify_llamado_con={ib_falso.qualify_llamado_con}")
check("formatear_posiciones_abiertas: tras el relleno, consigue precio y no muestra N/D",
      "N/D" not in resultado, f"resultado={resultado!r}")
check("formatear_posiciones_abiertas: incluye el ticker BTC",
      "BTC" in resultado, f"resultado={resultado!r}")


# ---------------------------------------------------------------------------
# formatear_operaciones_cerradas: mismo formato de tabla que
# cartera_alpaca.py (peticion del usuario, sept. 2026) - Merc./Ticker/
# Cant./Precio/%/EUR, "abierta desde" debajo de cada fila, linea en blanco
# entre operaciones, y % TOTAL sobre lo invertido.
# ---------------------------------------------------------------------------
hoy_dt = datetime.now()
compra_hace_3h = hoy_dt - timedelta(hours=3)
operaciones_cerradas_ibkr = [
    {"fecha_hora": compra_hace_3h.isoformat(timespec="seconds"), "mercado": "US", "ticker": "AAPL",
     "lado": "COMPRA", "cantidad": 2.0, "precio": 100.0, "comision": 1.0, "currency": "USD"},
    {"fecha_hora": hoy_dt.isoformat(timespec="seconds"), "mercado": "US", "ticker": "AAPL",
     "lado": "VENTA", "cantidad": 2.0, "precio": 105.0, "comision": 1.0, "currency": "USD",
     "coste_medio": 100.0, "beneficio_pct": 4.0},
    {"fecha_hora": compra_hace_3h.isoformat(timespec="seconds"), "mercado": "HK", "ticker": "700",
     "lado": "COMPRA", "cantidad": 10.0, "precio": 50.0, "comision": 2.0, "currency": "HKD"},
    {"fecha_hora": hoy_dt.isoformat(timespec="seconds"), "mercado": "HK", "ticker": "700",
     "lado": "VENTA", "cantidad": 10.0, "precio": 52.0, "comision": 2.0, "currency": "HKD",
     "coste_medio": 50.0, "beneficio_pct": 4.0},
]
with open(bot.ARCHIVO_HISTORIAL_OPERACIONES, "w") as f:
    json.dump(operaciones_cerradas_ibkr, f)

cerradas_html_ibkr = cartera.formatear_operaciones_cerradas(hoy_dt.date(), hoy_dt.date(), html=True)
check("formatear_operaciones_cerradas (html): cabecera con Merc./Ticker/Cant./Precio/%/EUR "
      "(mismo formato que cartera_alpaca.py)",
      "Merc." in cerradas_html_ibkr and "Ticker" in cerradas_html_ibkr and "Cant." in cerradas_html_ibkr
      and "Precio" in cerradas_html_ibkr and "EUR" in cerradas_html_ibkr,
      f"resultado={cerradas_html_ibkr!r}")
check("formatear_operaciones_cerradas (html): muestra el mercado (US) y el precio de venta",
      "US" in cerradas_html_ibkr and "105,00" in cerradas_html_ibkr, f"resultado={cerradas_html_ibkr!r}")
check("formatear_operaciones_cerradas (html): indica desde cuando estaba abierta la posicion "
      "(encontrando la COMPRA en el historial completo)",
      "abierta desde" in cerradas_html_ibkr and "3h" in cerradas_html_ibkr, f"resultado={cerradas_html_ibkr!r}")
check("formatear_operaciones_cerradas (html): hay una linea en blanco tras la operacion",
      "\n\n" in cerradas_html_ibkr, f"resultado={cerradas_html_ibkr!r}")
check("formatear_operaciones_cerradas (html): el importe total vendido y el % sobre lo "
      "invertido se muestran en EUR",
      "Importe total vendido" in cerradas_html_ibkr and "sobre lo invertido" in cerradas_html_ibkr,
      f"resultado={cerradas_html_ibkr!r}")

cerradas_plano_ibkr = cartera.formatear_operaciones_cerradas(hoy_dt.date(), hoy_dt.date())
check("formatear_operaciones_cerradas (plano): el TOTAL incluye el % sobre lo invertido",
      "sobre lo invertido" in cerradas_plano_ibkr, f"resultado={cerradas_plano_ibkr!r}")


if fallos:
    print(f"\n{len(fallos)} test(s) FALLARON: {fallos}")
    sys.exit(1)
else:
    print("\nTodos los tests pasaron correctamente.")
