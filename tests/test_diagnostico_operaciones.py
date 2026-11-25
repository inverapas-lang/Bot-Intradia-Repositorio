"""
Tests manuales (sin pytest) para diagnostico_operaciones.py: solo la parte
offline (parsear_actividad + emparejar_round_trips), sin llamar a la API de
Alpaca.
"""
import os
import sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("ALPACA_API_KEY", "test-key")
os.environ.setdefault("ALPACA_SECRET_KEY", "test-secret")

import diagnostico_operaciones as diag

fallos = []


def check(nombre, condicion, detalle=""):
    estado = "OK  " if condicion else "FAIL"
    print(f"[{estado}] {nombre}" + (f" -- {detalle}" if detalle and not condicion else ""))
    if not condicion:
        fallos.append(nombre)


TEXTO_PRUEBA = """
Buy 1.0 AAPL	FILL	1.0	-$100.00	Sep 08, 2026, 10:00:00 AM
Sell 1.0 AAPL	FILL	1.0	+$99.00	Sep 08, 2026, 11:00:00 AM
CAT fee for proceed of 1 trades on 2026-09-08 by 12345	FEE	–	-$0.01	Sep 08, 2026
Buy 2.0 MSFT	FILL	2.0	-$50.00	Sep 09, 2026, 09:00:00 AM
Sell 1.0 MSFT	FILL	1.0	+$26.00	Sep 09, 2026, 09:30:00 AM
"""

operaciones = diag.parsear_actividad(TEXTO_PRUEBA)
check("parsear_actividad: reconoce las 4 operaciones de compra/venta (ignora la linea FEE)",
      len(operaciones) == 4, f"operaciones={operaciones!r}")
check("parsear_actividad: orden cronologico ascendente",
      operaciones[0]["ticker"] == "AAPL" and operaciones[0]["lado"] == "COMPRA"
      and operaciones[-1]["ticker"] == "MSFT" and operaciones[-1]["lado"] == "VENTA",
      f"operaciones={operaciones!r}")
check("parsear_actividad: fecha/hora parseada correctamente (AM/PM)",
      operaciones[0]["fecha_hora"] == datetime(2026, 9, 8, 10, 0, 0), f"operaciones={operaciones!r}")

round_trips, abiertas = diag.emparejar_round_trips(operaciones)
check("emparejar_round_trips: 2 round trips cerrados (AAPL completo, MSFT parcial)",
      len(round_trips) == 2, f"round_trips={round_trips!r}")

rt_aapl = next(rt for rt in round_trips if rt["ticker"] == "AAPL")
check("emparejar_round_trips: AAPL con perdida de -1 USD (-1%)",
      abs(rt_aapl["ganancia_usd"] - (-1.0)) < 1e-6 and abs(rt_aapl["beneficio_pct"] - (-1.0)) < 1e-6,
      f"rt_aapl={rt_aapl!r}")

rt_msft = next(rt for rt in round_trips if rt["ticker"] == "MSFT")
check("emparejar_round_trips: MSFT (venta parcial) usa solo el coste de la fraccion vendida (25 USD), ganancia +1 USD",
      abs(rt_msft["coste"] - 25.0) < 1e-6 and abs(rt_msft["ganancia_usd"] - 1.0) < 1e-6,
      f"rt_msft={rt_msft!r}")

check("emparejar_round_trips: queda 1 unidad de MSFT abierta (2 compradas, 1 vendida)",
      len(abiertas) == 1 and abiertas[0]["ticker"] == "MSFT" and abs(abiertas[0]["cantidad"] - 1.0) < 1e-6,
      f"abiertas={abiertas!r}")

informe = diag.informe_round_trips(round_trips, abiertas, margen_minimo_pct=0.5)
check("informe_round_trips: avisa de la venta de AAPL por debajo del suelo (perdida)",
      "AAPL" in informe and "por debajo del suelo" in informe, f"informe={informe!r}")
lineas_informe = informe.splitlines()
linea_msft = next(l for l in lineas_informe if l.startswith("MSFT"))
check("informe_round_trips: NO avisa de MSFT (ganancia del 4%, por encima del suelo del 0.5%)",
      "por debajo del suelo" not in linea_msft, f"linea_msft={linea_msft!r}")

if fallos:
    print(f"\n{len(fallos)} test(s) FALLARON: {fallos}")
    sys.exit(1)
else:
    print("\nTodos los tests pasaron correctamente.")
