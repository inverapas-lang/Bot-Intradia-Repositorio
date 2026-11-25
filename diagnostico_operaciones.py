"""
diagnostico_operaciones.py - herramienta de analisis A POSTERIORI de las
compras/ventas reales del bot de Alpaca, para poder responder "¿esta
operando bien?" con datos en vez de intuicion.

Hace dos cosas independientes:

1. Emparejamiento de operaciones (SIEMPRE, sin API): a partir de un listado
   de operaciones (compra/venta, cantidad, importe, fecha/hora -tal cual se
   puede copiar de la pestaña "Activity"/"Historial" de la app de Alpaca-),
   reconstruye los "round trips" (compra(s) -> venta(s) del mismo ticker,
   FIFO) y calcula el P&L real de cada uno, el tiempo en cartera, y avisa si
   alguna venta rompe las reglas que el bot deberia cumplir (nunca vender
   por debajo de MARGEN_MINIMO_VENTA_PCT).

2. Contexto de velas reales (OPCIONAL, con --velas, necesita
   ALPACA_API_KEY/ALPACA_SECRET_KEY en el entorno, igual que bot_alpaca.py):
   para cada venta, pide las velas de 1 minuto reales alrededor de la hora
   de la operacion y muestra si el precio de venta quedo cerca del maximo de
   esa ventana (buena venta) o si el precio sigo subiendo justo despues
   (venta prematura) o bajando (buena venta que evito mas perdidas).

Uso:
    python3 diagnostico_operaciones.py --archivo actividad.txt
    python3 diagnostico_operaciones.py --archivo actividad.txt --velas

Formato esperado de cada linea de operacion en el archivo (tal cual las
copia/pega la app de Alpaca, separadas por tabulador; las lineas de FEE,
CSD (deposito) etc. se ignoran automaticamente):
    Sell 0.3183 INTC<TAB>FILL<TAB>0.3183<TAB>+$32.32<TAB>Sep 11, 2026, 11:24:55 AM
    Buy 0.7223 SMCI<TAB>FILL<TAB>0.7223<TAB>-$27.19<TAB>Sep 11, 2026, 10:16:59 AM

No requiere pytest; el propio modulo se puede importar y usar sus funciones
sueltas desde otro script.
"""
import argparse
import os
import re
import sys
from datetime import timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# MARGEN_MINIMO_VENTA_PCT vive en bot_alpaca.py; se importa aqui (no de forma
# perezosa) porque las funciones offline (parsear/emparejar) tambien lo usan
# para las comprobaciones de reglas. Requiere las mismas variables de
# entorno que bot_alpaca.py (ALPACA_API_KEY/ALPACA_SECRET_KEY) para poder
# importarlo, aunque el modo offline (sin --velas) no llegue a llamar a la
# API real.
os.environ.setdefault("ALPACA_API_KEY", "diagnostico-sin-api-key")
os.environ.setdefault("ALPACA_SECRET_KEY", "diagnostico-sin-api-key")
import bot_alpaca as bot  # noqa: E402


_PATRON_LINEA = re.compile(
    r"^(Buy|Sell)\s+([\d.]+)\s+(\S+)\s+FILL\s+[\d.]+\s+([+-])\$([\d.]+)\s+(.+)$"
)


def parsear_actividad(texto):
    """Convierte el texto pegado de la app de Alpaca en una lista de
    operaciones (ignora FEE/CSD/lineas que no sean compra/venta con FILL).
    Cada operacion: {"lado", "ticker", "cantidad", "importe" (con signo,
    +venta/-compra), "fecha_hora" (datetime)}."""
    operaciones = []
    for linea in texto.splitlines():
        linea = linea.strip()
        if not linea:
            continue
        m = _PATRON_LINEA.match(linea)
        if not m:
            continue
        lado, cantidad, ticker, signo, importe, fecha_texto = m.groups()
        fecha_hora = _parsear_fecha(fecha_texto.strip())
        operaciones.append({
            "lado": "COMPRA" if lado == "Buy" else "VENTA",
            "ticker": ticker,
            "cantidad": float(cantidad),
            "importe": (1 if signo == "+" else -1) * float(importe),
            "fecha_hora": fecha_hora,
        })
    # Orden cronologico ascendente (la app de Alpaca las da mas reciente primero).
    operaciones.sort(key=lambda o: o["fecha_hora"])
    return operaciones


_MESES = {
    "Jan": 1, "Feb": 2, "Mar": 3, "Apr": 4, "May": 5, "Jun": 6,
    "Jul": 7, "Aug": 8, "Sep": 9, "Oct": 10, "Nov": 11, "Dec": 12,
}
_PATRON_FECHA = re.compile(
    r"^(\w{3}) (\d{1,2}), (\d{4}), (\d{1,2}):(\d{2}):(\d{2}) (AM|PM)$"
)


def _parsear_fecha(texto):
    from datetime import datetime
    m = _PATRON_FECHA.match(texto)
    if not m:
        raise ValueError(f"Fecha no reconocida: {texto!r}")
    mes_txt, dia, anio, hora, minuto, segundo, ampm = m.groups()
    hora = int(hora) % 12
    if ampm == "PM":
        hora += 12
    return datetime(int(anio), _MESES[mes_txt], int(dia), hora, int(minuto), int(segundo))


def emparejar_round_trips(operaciones):
    """Empareja compras y ventas de cada ticker por FIFO (primero en entrar,
    primero en salir) y devuelve una lista de round trips cerrados: cada uno
    con el coste total, el ingreso total, el P&L, el % de beneficio/perdida
    sobre el coste, y el tiempo entre la PRIMERA compra que contribuyo y la
    ultima venta que lo cerro. Las posiciones que quedan abiertas (compradas
    pero sin vender del todo en el rango de datos) se devuelven aparte."""
    colas = {}  # ticker -> lista de {"cantidad_restante", "coste_unitario", "fecha_hora"}
    round_trips = []

    for op in operaciones:
        ticker = op["ticker"]
        colas.setdefault(ticker, [])
        if op["lado"] == "COMPRA":
            coste_unitario = -op["importe"] / op["cantidad"]
            colas[ticker].append({
                "cantidad_restante": op["cantidad"],
                "coste_unitario": coste_unitario,
                "fecha_hora": op["fecha_hora"],
            })
        else:
            cantidad_por_vender = op["cantidad"]
            ingreso_unitario = op["importe"] / op["cantidad"] if op["cantidad"] else 0
            coste_total = 0.0
            cantidad_casada = 0.0
            primera_compra = None
            while cantidad_por_vender > 1e-9 and colas[ticker]:
                lote = colas[ticker][0]
                usado = min(lote["cantidad_restante"], cantidad_por_vender)
                coste_total += usado * lote["coste_unitario"]
                cantidad_casada += usado
                primera_compra = primera_compra or lote["fecha_hora"]
                lote["cantidad_restante"] -= usado
                cantidad_por_vender -= usado
                if lote["cantidad_restante"] <= 1e-9:
                    colas[ticker].pop(0)
            if cantidad_casada <= 1e-9:
                continue  # venta sin compra previa registrada en este rango (dato incompleto)
            ingreso_total = cantidad_casada * ingreso_unitario
            ganancia = ingreso_total - coste_total
            round_trips.append({
                "ticker": ticker,
                "cantidad": cantidad_casada,
                "coste": coste_total,
                "ingreso": ingreso_total,
                "ganancia_usd": ganancia,
                "beneficio_pct": (ganancia / coste_total * 100) if coste_total else 0.0,
                "compra": primera_compra,
                "venta": op["fecha_hora"],
                "duracion": op["fecha_hora"] - primera_compra,
            })

    posiciones_abiertas = [
        {"ticker": t, "cantidad": l["cantidad_restante"], "coste_unitario": l["coste_unitario"],
         "fecha_hora": l["fecha_hora"]}
        for t, lotes in colas.items() for l in lotes if l["cantidad_restante"] > 1e-9
    ]
    return round_trips, posiciones_abiertas


def informe_round_trips(round_trips, posiciones_abiertas, margen_minimo_pct=None):
    if margen_minimo_pct is None:
        margen_minimo_pct = bot.MARGEN_MINIMO_VENTA_PCT

    lineas = []
    lineas.append(f"{'Ticker':<10}{'Cant.':>10}{'Coste':>10}{'Ingreso':>10}{'P&L $':>9}{'P&L %':>9}{'Duracion':>14}")
    total_coste = total_ingreso = total_ganancia = 0.0
    ganadoras = perdedoras = violaciones = 0
    for rt in round_trips:
        total_coste += rt["coste"]
        total_ingreso += rt["ingreso"]
        total_ganancia += rt["ganancia_usd"]
        if rt["ganancia_usd"] >= 0:
            ganadoras += 1
        else:
            perdedoras += 1
        aviso = ""
        if rt["beneficio_pct"] < margen_minimo_pct:
            violaciones += 1
            aviso = "  <-- por debajo del suelo del %.1f%%!" % margen_minimo_pct
        lineas.append(
            f"{rt['ticker']:<10}{rt['cantidad']:>10.4f}{rt['coste']:>10.2f}{rt['ingreso']:>10.2f}"
            f"{rt['ganancia_usd']:>+9.2f}{rt['beneficio_pct']:>+8.2f}% {str(rt['duracion']):>16}{aviso}"
        )

    lineas.append("")
    lineas.append(f"Round trips cerrados: {len(round_trips)} ({ganadoras} con ganancia, {perdedoras} con perdida)")
    lineas.append(f"Coste total: {total_coste:.2f} USD | Ingreso total: {total_ingreso:.2f} USD | "
                   f"P&L neto: {total_ganancia:+.2f} USD ({(total_ganancia / total_coste * 100) if total_coste else 0:+.2f}%)")
    if violaciones:
        lineas.append(f"⚠️  {violaciones} venta(s) por debajo del suelo de seguridad "
                       f"({margen_minimo_pct}%) configurado en MARGEN_MINIMO_VENTA_PCT.")
    else:
        lineas.append(f"✅ Ninguna venta viola el suelo de seguridad configurado ({margen_minimo_pct}%).")

    if posiciones_abiertas:
        lineas.append("")
        lineas.append(f"Posiciones que quedaron abiertas al final del rango de datos ({len(posiciones_abiertas)}):")
        for p in posiciones_abiertas:
            lineas.append(f"  {p['ticker']}: {p['cantidad']:.4f} a coste {p['coste_unitario']:.4f} "
                           f"(comprado {p['fecha_hora']})")

    return "\n".join(lineas)


def obtener_velas_contexto(ticker, momento, ventana_minutos=10):
    """Pide las velas de 1 minuto reales alrededor de 'momento' (hora local,
    NAIVE -se asume ya en hora de NY, la misma que usa el bot-). Requiere
    ALPACA_API_KEY/ALPACA_SECRET_KEY reales en el entorno. Devuelve una
    lista de velas (puede estar vacia si no hay datos, p.ej. fuera de
    sesion para acciones)."""
    from alpaca.data.requests import StockBarsRequest, CryptoBarsRequest
    from alpaca.data.timeframe import TimeFrame

    inicio = momento - timedelta(minutes=ventana_minutos)
    fin = momento + timedelta(minutes=ventana_minutos)
    if bot.es_cripto(ticker):
        peticion = CryptoBarsRequest(symbol_or_symbols=[ticker], timeframe=TimeFrame.Minute, start=inicio, end=fin)
        barset = bot._crypto_data_client.get_crypto_bars(peticion)
    else:
        peticion = StockBarsRequest(symbol_or_symbols=[ticker], timeframe=TimeFrame.Minute, start=inicio, end=fin)
        barset = bot._data_client.get_stock_bars(peticion)
    return list(barset.data.get(ticker, []))


def analizar_venta_con_velas(rt, ventana_minutos=10):
    """Para un round trip ya cerrado, pide las velas alrededor de la venta y
    dice si el precio de venta quedo cerca del maximo de la ventana (venta
    bien cronometrada) o si el precio seguia subiendo poco despues (venta
    prematura, se dejo beneficio en la mesa)."""
    try:
        velas = obtener_velas_contexto(rt["ticker"], rt["venta"], ventana_minutos)
    except Exception as e:
        return f"  (no se pudieron pedir velas para {rt['ticker']}: {type(e).__name__}: {e})"

    if not velas:
        return f"  (sin velas disponibles alrededor de {rt['venta']} para {rt['ticker']})"

    precio_venta = rt["ingreso"] / rt["cantidad"]
    maximo_ventana = max(float(v.high) for v in velas)
    minimo_ventana = min(float(v.low) for v in velas)
    velas_despues = [v for v in velas if v.timestamp.replace(tzinfo=None) >= rt["venta"]]
    maximo_despues = max((float(v.high) for v in velas_despues), default=precio_venta)

    beneficio_dejado_pct = (maximo_despues - precio_venta) / precio_venta * 100 if precio_venta else 0
    texto = (f"  Contexto de precio ±{ventana_minutos}min: min {minimo_ventana:.4f} / max {maximo_ventana:.4f} "
             f"| vendido a {precio_venta:.4f}")
    if beneficio_dejado_pct > 0.3:
        texto += f"\n  -> el precio siguio subiendo hasta {maximo_despues:.4f} despues de vender " \
                  f"(+{beneficio_dejado_pct:.2f}% dejado en la mesa)"
    return texto


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--archivo", required=True, help="Fichero con el texto de 'Activity' de Alpaca")
    parser.add_argument("--velas", action="store_true",
                         help="Ademas, pide velas reales de Alpaca para dar contexto a cada venta (necesita API keys reales)")
    parser.add_argument("--ventana-minutos", type=int, default=10)
    args = parser.parse_args()

    with open(args.archivo, "r", encoding="utf-8") as f:
        texto = f.read()

    operaciones = parsear_actividad(texto)
    if not operaciones:
        print("No se ha reconocido ninguna operacion en el archivo.")
        return

    round_trips, posiciones_abiertas = emparejar_round_trips(operaciones)
    print(informe_round_trips(round_trips, posiciones_abiertas))

    if args.velas:
        print("\n--- Contexto de velas reales por venta ---")
        for rt in round_trips:
            print(f"\n{rt['ticker']} vendido el {rt['venta']}:")
            print(analizar_venta_con_velas(rt, args.ventana_minutos))


if __name__ == "__main__":
    main()
