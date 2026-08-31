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

    if not posiciones:
        titulo = "📈 <b>POSICIONES ABIERTAS</b>" if html else "📈 POSICIONES ABIERTAS"
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
        lineas_tabla = [f"  {'Ticker':<7}{'Cant.':>8}{'P/L %':>9}{'P/L USD':>10}"]
        for symbol, cantidad, coste_medio, precio_actual, invertido, pl_usd, pl_eur, pl_pct in filas:
            lineas_tabla.append(f"{_emoji_pl(pl_usd)} {symbol:<6}{cantidad:>8.2f}{pl_pct:>+8.2f}%{pl_usd:>+10.2f}")
        tabla = "<pre>" + "\n".join(lineas_tabla) + "</pre>"
        resumen = (f"<b>TOTAL</b> invertido: {total_invertido:.2f} USD ({total_invertido / bot.TIPO_CAMBIO_EUR_USD:.2f} EUR)\n"
                  f"P/L: {pl_total_usd:+.2f} USD ({pl_total_usd / bot.TIPO_CAMBIO_EUR_USD:+.2f} EUR, {pl_total_pct:+.2f}%)")
        return f"📈 <b>POSICIONES ABIERTAS</b>\n{tabla}\n{resumen}"

    lineas = ["📈 POSICIONES ABIERTAS"]
    for symbol, cantidad, coste_medio, precio_actual, invertido, pl_usd, pl_eur, pl_pct in filas:
        lineas.append(f"{symbol}: {cantidad:g} acciones a {coste_medio:.4f} USD "
                      f"(invertido {invertido:.2f} USD, ahora {precio_actual:.4f} USD) "
                      f"P/L {pl_usd:+.2f} USD / {pl_eur:+.2f} EUR ({pl_pct:+.2f}%)")
    lineas.append(f"TOTAL invertido: {total_invertido:.2f} USD ({total_invertido / bot.TIPO_CAMBIO_EUR_USD:.2f} EUR) | "
                  f"valor actual: {total_actual:.2f} USD | "
                  f"P/L: {pl_total_usd:+.2f} USD ({pl_total_usd / bot.TIPO_CAMBIO_EUR_USD:+.2f} EUR, {pl_total_pct:+.2f}%)")
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
        filas.append((o["fecha_hora"], o["ticker"], cantidad, precio, ganancia_usd, ganancia_eur, beneficio_pct))

    if html:
        lineas_tabla = [f"  {'Fecha':<12}{'Ticker':<7}{'Cant.':>7}{'Gan. USD':>10}"]
        for fecha_hora, ticker, cantidad, precio, ganancia_usd, ganancia_eur, beneficio_pct in filas:
            fecha_corta = fecha_hora[5:16].replace("T", " ")  # MM-DD HH:MM (11 caracteres)
            emoji = _emoji_pl(ganancia_usd) if ganancia_usd is not None else "⚪"
            ganancia_str = f"{ganancia_usd:>+10.2f}" if ganancia_usd is not None else f"{'N/D':>10}"
            lineas_tabla.append(f"{emoji} {fecha_corta:<12}{ticker:<7}{cantidad:>7.2f}{ganancia_str}")
        tabla = "<pre>" + "\n".join(lineas_tabla) + "</pre>"
        resumen = (f"<b>TOTAL</b> ganancia/perdida realizada: {ganancia_total_usd:+.2f} USD "
                  f"({ganancia_total_usd / bot.TIPO_CAMBIO_EUR_USD:+.2f} EUR)")
        return f"{titulo_html}\n{tabla}\n{resumen}"

    lineas = [titulo_plano]
    for fecha_hora, ticker, cantidad, precio, ganancia_usd, ganancia_eur, beneficio_pct in filas:
        fecha_str = fecha_hora[:16].replace("T", " ")
        ganancia_str = f", ganancia {ganancia_usd:+.2f} USD / {ganancia_eur:+.2f} EUR" if ganancia_usd is not None else ""
        beneficio_pct_str = f" ({beneficio_pct:+.2f}%)" if beneficio_pct is not None else ""
        lineas.append(f"{fecha_str} {ticker}: {cantidad:g} acciones a {precio:.4f} USD"
                      f"{ganancia_str}{beneficio_pct_str}")
    lineas.append(f"TOTAL ganancia/perdida realizada: {ganancia_total_usd:+.2f} USD "
                  f"({ganancia_total_usd / bot.TIPO_CAMBIO_EUR_USD:+.2f} EUR)")
    return "\n".join(lineas)


def main():
    args = parsear_argumentos()
    desde, hasta = calcular_rango(args)

    modo = "PAPER (simulado)" if bot.ALPACA_PAPER else "REAL"
    print(f"Cartera Alpaca [{modo}] - operaciones cerradas: {desde} a {hasta}\n")
    print(formatear_posiciones_abiertas())
    print()
    print(formatear_operaciones_cerradas(desde, hasta))


if __name__ == "__main__":
    main()
