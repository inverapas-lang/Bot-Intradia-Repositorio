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


def formatear_posiciones_abiertas(html=False, client=None, modo_etiqueta=None):
    """Si html=True, devuelve el texto listo para mandar a Telegram con
    parse_mode=HTML: una tabla monoespaciada (<pre>) mas facil de leer en el
    movil que una lista de frases largas. Si html=False (uso desde la
    terminal, cartera_alpaca.py --*), devuelve texto plano sin ninguna
    etiqueta, igual que antes.

    `client` y `modo_etiqueta` permiten consultar una cuenta DISTINTA de la
    que el bot usa para operar -usado por /carterapaper de telegram_bot.py
    para consultar la cuenta PAPER incluso con el bot operando en REAL
    (peticion del usuario, sept. 2026). Si no se pasan, se usa la cuenta
    activa del bot como siempre."""
    posiciones = bot.obtener_posiciones(client)

    # Las posiciones ABIERTAS vienen en vivo de la API de Alpaca, siempre en
    # el modo de la cuenta consultada (a diferencia del historial de
    # cerradas, que se acumula entre cambios de modo) - se deja claro en el
    # titulo para no confundirlo con una operacion PAPER antigua.
    modo_actual = modo_etiqueta or ("PAPER" if bot.ALPACA_PAPER else "REAL")
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
        # Mismo formato de tabla que formatear_operaciones_cerradas() (peticion
        # del usuario, sept. 2026: que abiertas y cerradas se vean igual) -
        # Ticker + Cant. (maximo 4 decimales) + Precio (actual, no de venta) +
        # % + USD, con la fecha de apertura debajo de cada fila junto al
        # emoji de modo, y una linea en blanco entre una posicion y la
        # siguiente.
        operaciones_todas = bot.cargar_historial_operaciones()
        operaciones_por_ticker = {}
        for o in operaciones_todas:
            if o["lado"] in ("COMPRA", "VENTA"):
                operaciones_por_ticker.setdefault(o["ticker"], []).append(o)
        for lista in operaciones_por_ticker.values():
            lista.sort(key=lambda o: o["fecha_hora"])
        ahora_iso = datetime.now().isoformat(timespec="seconds")

        lineas_tabla = [f"  {'Ticker':<7}{'Cant.':>7}{'Precio':>8}{'%':>8}{'USD':>8}"]
        modo_emoji = "💰" if modo_actual == "REAL" else "🧪"
        for symbol, cantidad, coste_medio, precio_actual, invertido, pl_usd, pl_eur, pl_pct in filas:
            cantidad_str = f"{round(cantidad, 4):g}"
            precio_str = bot.formato_es(precio_actual)
            pct_str = f"{bot.formato_es(pl_pct, signo=True)}%"
            pl_str = bot.formato_es(pl_usd, signo=True)
            lineas_tabla.append(f"{_emoji_pl(pl_usd)} {symbol:<6}{cantidad_str:>7}{precio_str:>8}{pct_str:>8}{pl_str:>8}")
            fecha_apertura = _fecha_apertura_posicion(operaciones_por_ticker.get(symbol, []), ahora_iso, modo_actual)
            apertura_str = ""
            if fecha_apertura is not None:
                duracion = _formatear_duracion(datetime.now() - datetime.fromisoformat(fecha_apertura))
                apertura_str = f" abierta desde {_formatear_fecha_corta(fecha_apertura)} ({duracion})"
            lineas_tabla.append(f"  {modo_emoji}{apertura_str}")
            lineas_tabla.append("")
        tabla = "<pre>" + "\n".join(lineas_tabla).rstrip() + "</pre>"
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


def _fecha_apertura_posicion(operaciones_ticker_ordenadas, hasta_fecha_hora, modo):
    """Recorre TODAS las operaciones (ya ordenadas cronologicamente, de UN
    solo ticker, sin restringir por rango de fechas -una posicion puede
    haberse abierto antes del rango que se esta consultando-) y devuelve la
    fecha/hora de la COMPRA que abrio la racha actual: la cantidad se va
    acumulando con cada COMPRA y descontando con cada VENTA, y cada vez que
    cae a ~0 (posicion totalmente cerrada) se olvida la apertura anterior -
    la siguiente COMPRA cuenta como una posicion nueva. None si no hay
    ninguna compra registrada antes de 'hasta_fecha_hora' (dato incompleto,
    p.ej. si el historial no llega tan atras).

    BUG REAL DE PRODUCCION (sept. 2026, caso real: CVX aparecia "abierta
    desde" hace mas de una semana cuando en realidad se habia comprado esa
    misma mañana): solo se cuentan operaciones del MISMO modo (REAL/PAPER)
    que la venta que se esta mirando -son carteras independientes-. Sin
    este filtro, un historial que mezcla una epoca PAPER antigua con la
    REAL actual para el mismo ticker arrastraba la apertura de una posicion
    PAPER de hace dias que no tenia nada que ver con la posicion REAL de
    hoy."""
    cantidad_actual = 0.0
    fecha_apertura = None
    for o in operaciones_ticker_ordenadas:
        if o["fecha_hora"] > hasta_fecha_hora:
            break
        if _modo_operacion(o) != modo:
            continue
        if o["lado"] == "COMPRA":
            if cantidad_actual <= 1e-9:
                fecha_apertura = o["fecha_hora"]
            cantidad_actual += o["cantidad"]
        else:
            cantidad_actual = max(0.0, cantidad_actual - o["cantidad"])
    return fecha_apertura


_MESES_ES = ["ENE", "FEB", "MAR", "ABR", "MAY", "JUN", "JUL", "AGO", "SEP", "OCT", "NOV", "DIC"]


def _formatear_fecha_corta(fecha_hora_iso):
    """'2026-09-03T15:09:12' -> '03 SEP' (peticion del usuario, sept. 2026:
    formato mas legible que la fecha ISO para la linea de 'abierta desde')."""
    fecha = datetime.fromisoformat(fecha_hora_iso)
    return f"{fecha.day:02d} {_MESES_ES[fecha.month - 1]}"


def _formatear_duracion(delta):
    segundos = int(delta.total_seconds())
    if segundos < 60:
        return f"{segundos}s"
    minutos = segundos // 60
    if minutos < 60:
        return f"{minutos}min"
    horas, minutos_resto = divmod(minutos, 60)
    if horas < 24:
        return f"{horas}h {minutos_resto:02d}min" if minutos_resto else f"{horas}h"
    dias, horas_resto = divmod(horas, 24)
    return f"{dias}d {horas_resto}h" if horas_resto else f"{dias}d"


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

    # Historial COMPLETO por ticker (sin restringir al rango desde/hasta):
    # hace falta para encontrar la compra de apertura de una posicion que
    # pudo abrirse antes del rango que se esta consultando (ver
    # _fecha_apertura_posicion()).
    operaciones_por_ticker = {}
    for o in operaciones:
        if o["lado"] in ("COMPRA", "VENTA"):
            operaciones_por_ticker.setdefault(o["ticker"], []).append(o)
    for lista in operaciones_por_ticker.values():
        lista.sort(key=lambda o: o["fecha_hora"])

    filas = []
    ganancia_total_usd = 0.0
    coste_total_usd = 0.0
    importe_total_vendido_usd = 0.0
    for o in ventas:
        cantidad = o["cantidad"]
        coste_medio = o.get("coste_medio")
        precio = o["precio"]
        beneficio_pct = o.get("beneficio_pct")

        if coste_medio is not None:
            ganancia_usd = (precio - coste_medio) * cantidad  # sin comision, ver bot_alpaca.py
            ganancia_eur = ganancia_usd / bot.TIPO_CAMBIO_EUR_USD
            coste_total_usd += coste_medio * cantidad
        else:
            ganancia_usd = ganancia_eur = None
        ganancia_total_usd += ganancia_usd or 0.0
        importe_total_vendido_usd += cantidad * precio
        modo_op = _modo_operacion(o)
        fecha_apertura = _fecha_apertura_posicion(operaciones_por_ticker.get(o["ticker"], []), o["fecha_hora"], modo_op)
        filas.append((o["fecha_hora"], o["ticker"], cantidad, precio, ganancia_usd, ganancia_eur,
                      beneficio_pct, modo_op, fecha_apertura))

    beneficio_total_pct = (ganancia_total_usd / coste_total_usd * 100) if coste_total_usd else None

    if html:
        # Tabla (peticion del usuario, sept. 2026): Ticker + Cant. (maximo 4
        # decimales, redondeada para que la tabla quede alineada pese a que
        # el historial guarda muchos mas decimales de precision) + Precio de
        # venta + % + USD. Debajo de cada fila, en la MISMA linea que el
        # emoji de modo (no en su propia linea aparte -en el movil el emoji
        # solo se veia envuelto de forma rara-), cuanto llevaba abierta la
        # posicion (desde la ultima compra que la abrio hasta esta venta), y
        # una linea en blanco entre una operacion y la siguiente.
        lineas_tabla = [f"  {'Ticker':<7}{'Cant.':>7}{'Precio':>8}{'%':>8}{'USD':>8}"]
        for fecha_hora, ticker, cantidad, precio, ganancia_usd, ganancia_eur, beneficio_pct, modo, fecha_apertura in filas:
            emoji = _emoji_pl(ganancia_usd) if ganancia_usd is not None else "⚪"
            cantidad_str = f"{round(cantidad, 4):g}"
            precio_str = bot.formato_es(precio)
            pct_str = f"{bot.formato_es(beneficio_pct, signo=True)}%" if beneficio_pct is not None else "N/D"
            ganancia_str = bot.formato_es(ganancia_usd, signo=True) if ganancia_usd is not None else "N/D"
            modo_emoji = "💰" if modo == "REAL" else "🧪"
            lineas_tabla.append(f"{emoji} {ticker:<6}{cantidad_str:>7}{precio_str:>8}{pct_str:>8}{ganancia_str:>8}")
            apertura_str = ""
            if fecha_apertura is not None:
                duracion = _formatear_duracion(datetime.fromisoformat(fecha_hora) - datetime.fromisoformat(fecha_apertura))
                apertura_str = f" abierta desde {_formatear_fecha_corta(fecha_apertura)} ({duracion})"
            lineas_tabla.append(f"  {modo_emoji}{apertura_str}")
            lineas_tabla.append("")
        tabla = "<pre>" + "\n".join(lineas_tabla).rstrip() + "</pre>"
        pct_total_str = f" ({bot.formato_es(beneficio_total_pct, signo=True)}% sobre lo invertido, {bot.formato_es(coste_total_usd)} USD)" \
            if beneficio_total_pct is not None else ""
        resumen = (f"Importe total vendido: {bot.formato_es(importe_total_vendido_usd)} USD\n"
                  f"<b>TOTAL</b> ganancia/perdida realizada: {bot.formato_es(ganancia_total_usd, signo=True)} USD "
                  f"({bot.formato_es(ganancia_total_usd / bot.TIPO_CAMBIO_EUR_USD, signo=True)} EUR){pct_total_str}\n"
                  f"💰 = REAL, 🧪 = PAPER (simulado){_resumen_por_modo(ventas)}")
        return f"{titulo_html}\n{tabla}\n{resumen}"

    lineas = [titulo_plano]
    for fecha_hora, ticker, cantidad, precio, ganancia_usd, ganancia_eur, beneficio_pct, modo, fecha_apertura in filas:
        fecha_str = fecha_hora[:16].replace("T", " ")
        ganancia_str = (f", ganancia {bot.formato_es(ganancia_usd, signo=True)} USD / "
                        f"{bot.formato_es(ganancia_eur, signo=True)} EUR") if ganancia_usd is not None else ""
        beneficio_pct_str = f" ({bot.formato_es(beneficio_pct, signo=True)}%)" if beneficio_pct is not None else ""
        lineas.append(f"[{modo}] {fecha_str} {ticker}: {bot.formato_es(cantidad, 4)} acciones a "
                      f"{bot.formato_es(precio, 4)} USD{ganancia_str}{beneficio_pct_str}")
    pct_total_str = f" ({bot.formato_es(beneficio_total_pct, signo=True)}% sobre lo invertido)" if beneficio_total_pct is not None else ""
    lineas.append(f"TOTAL ganancia/perdida realizada: {bot.formato_es(ganancia_total_usd, signo=True)} USD "
                  f"({bot.formato_es(ganancia_total_usd / bot.TIPO_CAMBIO_EUR_USD, signo=True)} EUR){pct_total_str}")
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
