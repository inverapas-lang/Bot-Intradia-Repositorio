"""
Tests manuales (sin pytest) para cartera_ibkr.py: por ahora solo cubre el
bug real de produccion de sept. 2026 en formatear_posiciones_abiertas()
(contrato CRYPTO con exchange vacio). No hace ninguna llamada de red real
a IBKR.
"""
import os
import sys
import types

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import cartera_ibkr as cartera
import bot_completo as bot

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


if fallos:
    print(f"\n{len(fallos)} test(s) FALLARON: {fallos}")
    sys.exit(1)
else:
    print("\nTodos los tests pasaron correctamente.")
