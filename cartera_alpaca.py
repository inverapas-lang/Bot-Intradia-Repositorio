"""
cartera_alpaca.py - consulta A DEMANDA del estado de la cartera de Alpaca
(bot_alpaca.py), sin tocar el bot que este corriendo en ese momento:

  - Posiciones ABIERTAS: cantidad, precio medio, total invertido (0
    comision, a peticion del usuario -ver bot_alpaca.py-), precio actual,
    beneficio/perdida no realizado en USD y en EUR, y en %.
  - Posiciones CERRADAS en un rango de fechas (hoy por defecto): se leen del
    historial persistente que bot_alpaca.py va guardando en
    "historial_operaciones_alpaca.json" cada vez que una venta se ejecuta
    con exito (la API de Alpaca no da directamente el beneficio realizado
    de cada venta, asi que se registra en el momento en que se conoce).

Usa las mismas credenciales (ALPACA_API_KEY/ALPACA_SECRET_KEY/ALPACA_PAPER)
que bot_alpaca.py. Es de solo lectura: no coloca, modifica ni cancela
ninguna orden.

Las funciones formatear_*() devuelven texto plano (sin tablas de ancho fijo,
pensado para caber bien en un mensaje de Telegram) y son las que reutiliza
telegram_bot.py para los comandos /cartera, /hoy, /ayer, /semana. main()
las usa igual para la version de terminal.

Uso:
    python cartera_alpaca.py                        # hoy
    python cartera_alpaca.py --ayer                  # dia anterior
    python cartera_alpaca.py --semana                # semana laboral actual (lunes a hoy)
    python cartera_alpaca.py --desde 2026-08-01 --hasta 2026-08-15
"""
import argparse
from datetime import date, datetime, timedelta

import bot_alpaca as bot


def parsear_argumentos():
    parser = argparse.ArgumentParser(description="Estado de cartera de Alpaca (bot_alpaca.py)")
    grupo = parser.add_mutually_exclusive_group()
    grupo.add_argument("--ayer", action="store_true", help="operaciones cerradas del dia anterior")
    grupo.add_argument("--semana", action="store_true", help="operaciones cerradas de la semana laboral actual (lunes a hoy)")
    grupo.add_argument("--desde", metavar="YYYY-MM-DD", help="fecha inicial del rango (usar junto a --hasta)")
    parser.add_argument("--hasta", metavar="YYYY-MM-DD", help="fecha final del rango (con --desde, por defecto hoy)")
    return parser.parse_args()


def calcular_rango(args):
    hoy = date.today()
    if args.ayer:
        d = hoy - timedelta(days=1)
        return d, d
    if args.semana:
        inicio_semana = hoy - timedelta(days=hoy.weekday())  # lunes de esta semana
        return inicio_semana, hoy
    if args.desde:
        desde = date.fromisoformat(args.desde)
        hasta = date.fromisoformat(args.hasta) if args.hasta else hoy
        return desde, hasta
    return hoy, hoy


def _emoji_pl(valor):
    return "🟢" if valor >= 0 else "🔴"


def formatear_posiciones_abiertas(html=False):
    """Si html=True, devuelve el texto listo para mandar a Telegram con
    parse_mode=HTML: una tabla monoespaciada (<pre>) mas facil de leer en el
    movil que una lista de frases largas. Si html=False (uso desde la
    terminal, cartera_alpaca.py --*), devuelve texto plano sin ninguna
    etiqueta, igual que antes."""
    posiciones = bot.obtener_posiciones()

    # Las posiciones ABIERTAS vienen en vivo de la API de Alpaca, siempre en
    # el modo con el que esta conectado el bot AHORA MISMO (a diferencia del
    # historial de cerradas, que se acumula entre cambios de modo) - se deja
    # claro en el titulo para no confundirlo con una operacion PAPER antigua.
    modo_actual = "PAPER" if bot.ALPACA_PAPER else "REAL"
    if not posiciones:
        titulo = f"📈 <b>POSICIONES ABIERTAS</b> [{modo_actual}]" if html else f"📈 POSICIONES ABIERTAS [{modo_actual}]"
        return titulo + "\n(ninguna)"

    total_invertido = 0.0
    total_actual = 0.0
    filas = []

    for p in sorted(posiciones, key=lambda p: p.symbol):
        cantidad = float(p.qty)
        coste_medio = float(p.avg_entry_price)
        valor_actual = float(p.market_value)
        precio_actual = valor_actual / cantidad if cantidad else 0.0
        invertido = cantidad * coste_medio  # sin comision: se asume 0 EUR, ver bot_alpaca.py
        pl_usd = float(p.unrealized_pl)
        pl_pct = float(p.unrealized_plpc) * 100
        pl_eur = pl_usd / bot.TIPO_CAMBIO_EUR_USD

        total_invertido += invertido
        total_actual += valor_actual
        filas.append((p.symbol, cantidad, coste_medio, precio_actual, invertido, pl_usd, pl_eur, pl_pct))

    pl_total_usd = total_actual - total_invertido
    pl_total_pct = (pl_total_usd / total_invertido * 100) if total_invertido else 0.0

    if html:
        lineas_tabla = [f"  {'Ticker':<7}{'Cant.':>9}{'P/L %':>10}{'P/L USD':>12}"]
        for symbol, cantidad, coste_medio, precio_actual, invertido, pl_usd, pl_eur, pl_pct in filas:
            lineas_tabla.append(f"{_emoji_pl(pl_usd)} {symbol:<6}{bot.formato_es(cantidad, 2):>9}"
                                f"{bot.formato_es(pl_pct, signo=True):>9}%{bot.formato_es(pl_usd, signo=True):>12}")
        tabla = "<pre>" + "\n".join(lineas_tabla) + "</pre>"
        resumen = (f"<b>TOTAL</b> invertido: {bot.formato_es(total_invertido)} USD "
                  f"({bot.formato_es(total_invertido / bot.TIPO_CAMBIO_EUR_USD)} EUR)\n"
                  f"P/L: {bot.formato_es(pl_total_usd, signo=True)} USD "
                  f"({bot.formato_es(pl_total_usd / bot.TIPO_CAMBIO_EUR_USD, signo=True)} EUR, "
                  f"{bot.formato_es(pl_total_pct, signo=True)}%)")
        return f"📈 <b>POSICIONES ABIERTAS</b> [{modo_actual}]\n{tabla}\n{resumen}"

    lineas = [f"📈 POSICIONES ABIERTAS [{modo_actual}]"]
    for symbol, cantidad, coste_medio, precio_actual, invertido, pl_usd, pl_eur, pl_pct in filas:
        lineas.append(f"{symbol}: {bot.formato_es(cantidad, 4)} acciones a {bot.formato_es(coste_medio, 4)} USD "
                      f"(invertido {bot.formato_es(invertido)} USD, ahora {bot.formato_es(precio_actual, 4)} USD) "
                      f"P/L {bot.formato_es(pl_usd, signo=True)} USD / {bot.formato_es(pl_eur, signo=True)} EUR "
                      f"({bot.formato_es(pl_pct, signo=True)}%)")
    lineas.append(f"TOTAL invertido: {bot.formato_es(total_invertido)} USD "
                  f"({bot.formato_es(total_invertido / bot.TIPO_CAMBIO_EUR_USD)} EUR) | "
                  f"valor actual: {bot.formato_es(total_actual)} USD | "
                  f"P/L: {bot.formato_es(pl_total_usd, signo=True)} USD "
                  f"({bot.formato_es(pl_total_usd / bot.TIPO_CAMBIO_EUR_USD, signo=True)} EUR, "
                  f"{bot.formato_es(pl_total_pct, signo=True)}%)")
    return "\n".join(lineas)


def formatear_operaciones_cerradas(desde, hasta, html=False):
    """Ver formatear_posiciones_abiertas() para el significado de html=."""
    operaciones = bot.cargar_historial_operaciones()
    ventas = sorted(
        (o for o in operaciones
         if o["lado"] == "VENTA" and desde <= datetime.fromisoformat(o["fecha_hora"]).date() <= hasta),
        key=lambda o: o["fecha_hora"]
    )

    titulo_plano = f"📉 OPERACIONES CERRADAS ({desde} a {hasta})"
    titulo_html = f"📉 <b>OPERACIONES CERRADAS</b> ({desde} a {hasta})"
    if not ventas:
        return (titulo_html if html else titulo_plano) + "\n(ninguna)"

    filas = []
    ganancia_total_usd = 0.0
    for o in ventas:
        cantidad = o["cantidad"]
        coste_medio = o.get("coste_medio")
        precio = o["precio"]
        beneficio_pct = o.get("beneficio_pct")

        if coste_medio is not None:
            ganancia_usd = (precio - coste_medio) * cantidad  # sin comision, ver bot_alpaca.py
            ganancia_eur = ganancia_usd / bot.TIPO_CAMBIO_EUR_USD
        else:
            ganancia_usd = ganancia_eur = None
        ganancia_total_usd += ganancia_usd or 0.0
        filas.append((o["fecha_hora"], o["ticker"], cantidad, precio, ganancia_usd, ganancia_eur,
                      beneficio_pct, _modo_operacion(o)))

    if html:
        lineas_tabla = [f"  {'Fecha':<12}{'Ticker':<7}{'Cant.':>8}{'Gan. USD':>12}  Modo"]
        for fecha_hora, ticker, cantidad, precio, ganancia_usd, ganancia_eur, beneficio_pct, modo in filas:
            fecha_corta = fecha_hora[5:16].replace("T", " ")  # MM-DD HH:MM (11 caracteres)
            emoji = _emoji_pl(ganancia_usd) if ganancia_usd is not None else "⚪"
            ganancia_str = f"{bot.formato_es(ganancia_usd, signo=True):>12}" if ganancia_usd is not None else f"{'N/D':>12}"
            modo_emoji = "💰" if modo == "REAL" else "🧪"
            lineas_tabla.append(f"{emoji} {fecha_corta:<12}{ticker:<7}{bot.formato_es(cantidad, 2):>8}{ganancia_str}  {modo_emoji}")
        tabla = "<pre>" + "\n".join(lineas_tabla) + "</pre>"
        resumen = (f"<b>TOTAL</b> ganancia/perdida realizada: {bot.formato_es(ganancia_total_usd, signo=True)} USD "
                  f"({bot.formato_es(ganancia_total_usd / bot.TIPO_CAMBIO_EUR_USD, signo=True)} EUR)\n"
                  f"💰 = REAL, 🧪 = PAPER (simulado){_resumen_por_modo(ventas)}")
        return f"{titulo_html}\n{tabla}\n{resumen}"

    lineas = [titulo_plano]
    for fecha_hora, ticker, cantidad, precio, ganancia_usd, ganancia_eur, beneficio_pct, modo in filas:
        fecha_str = fecha_hora[:16].replace("T", " ")
        ganancia_str = (f", ganancia {bot.formato_es(ganancia_usd, signo=True)} USD / "
                        f"{bot.formato_es(ganancia_eur, signo=True)} EUR") if ganancia_usd is not None else ""
        beneficio_pct_str = f" ({bot.formato_es(beneficio_pct, signo=True)}%)" if beneficio_pct is not None else ""
        lineas.append(f"[{modo}] {fecha_str} {ticker}: {bot.formato_es(cantidad, 4)} acciones a "
                      f"{bot.formato_es(precio, 4)} USD{ganancia_str}{beneficio_pct_str}")
    lineas.append(f"TOTAL ganancia/perdida realizada: {bot.formato_es(ganancia_total_usd, signo=True)} USD "
                  f"({bot.formato_es(ganancia_total_usd / bot.TIPO_CAMBIO_EUR_USD, signo=True)} EUR)")
    return "\n".join(lineas)


def _modo_operacion(o):
    """Modo (REAL/PAPER) de una operacion del historial. Las operaciones
    anteriores a que este campo existiera (antes de sept. 2026, cuando el
    bot solo corria en PAPER) no lo tienen -se asume PAPER para esas, que
    es el unico modo que existia entonces."""
    return o.get("modo", "PAPER")


def _resumen_por_modo(operaciones):
    """'(N REAL, M PAPER)', omitiendo el lado que tenga 0 -para no repetir
    '0 REAL' cuando todo el rango es de un solo modo-."""
    reales = sum(1 for o in operaciones if _modo_operacion(o) == "REAL")
    paper = sum(1 for o in operaciones if _modo_operacion(o) == "PAPER")
    partes = [p for p in (f"{reales} REAL" if reales else "", f"{paper} PAPER" if paper else "") if p]
    return f" ({', '.join(partes)})" if partes else ""


def formatear_actividad(desde, hasta, html=False):
    """Cuenta cuantas COMPRAS y VENTAS se han ejecutado en el rango de
    fechas (numero de operaciones y acciones totales de cada lado) — un
    resumen rapido de "cuanta actividad ha habido", antes del detalle
    linea a linea de las ventas cerradas que ya da
    formatear_operaciones_cerradas(). Distingue REAL de PAPER (peticion del
    usuario, sept. 2026: el historial se acumula entre cambios de modo del
    bot, sin distinguirlos no se podia saber desde Telegram cuales fueron
    operaciones de verdad)."""
    operaciones = bot.cargar_historial_operaciones()
    en_rango = [o for o in operaciones if desde <= datetime.fromisoformat(o["fecha_hora"]).date() <= hasta]
    compras = [o for o in en_rango if o["lado"] == "COMPRA"]
    ventas = [o for o in en_rango if o["lado"] == "VENTA"]

    acciones_compradas = sum(o["cantidad"] for o in compras)
    acciones_vendidas = sum(o["cantidad"] for o in ventas)

    titulo = f"📊 <b>ACTIVIDAD</b> ({desde} a {hasta})" if html else f"📊 ACTIVIDAD ({desde} a {hasta})"
    return (f"{titulo}\n"
            f"🟢 Compras: {len(compras)} operaciones{_resumen_por_modo(compras)}, "
            f"{bot.formato_es(acciones_compradas, 2)} acciones\n"
            f"🔴 Ventas: {len(ventas)} operaciones{_resumen_por_modo(ventas)}, "
            f"{bot.formato_es(acciones_vendidas, 2)} acciones")


def main():
    args = parsear_argumentos()
    desde, hasta = calcular_rango(args)

    modo = "PAPER (simulado)" if bot.ALPACA_PAPER else "REAL"
    print(f"Cartera Alpaca [{modo}] - operaciones cerradas: {desde} a {hasta}\n")
    print(formatear_posiciones_abiertas())
    print()
    print(formatear_actividad(desde, hasta))
    print()
    print(formatear_operaciones_cerradas(desde, hasta))


if __name__ == "__main__":
    main()
