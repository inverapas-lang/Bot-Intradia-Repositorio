"""
cartera_ibkr.py - consulta A DEMANDA del estado de la cartera de IBKR
(bot_completo.py), sin tocar el bot que este corriendo en ese momento:

  - Posiciones ABIERTAS en todos los mercados activos (US/HK/KR/CRYPTO):
    cantidad, precio medio, total invertido (con comision estimada), precio
    actual, beneficio/perdida no realizado en moneda local y en EUR, y en %.
    Distingue cripto de US aunque ambos coticen en USD (via
    bot.mercado_de_posicion(), que mira el secType del contrato).
  - Posiciones CERRADAS en un rango de fechas (hoy por defecto): se leen del
    historial persistente que bot_completo.py va guardando en
    "historial_operaciones_ibkr.json" cada vez que una venta se ejecuta con
    exito -reqExecutions() de IBKR solo devuelve las ejecuciones del dia
    actual, asi que no sirve para consultar dias anteriores-.
  - Actividad (numero de compras/ventas y cantidad total de cada lado) en un
    rango de fechas, igual que cartera_alpaca.py.

Se conecta a IB Gateway/TWS con un clientId DISTINTO al del bot en marcha
(bot_completo.py usa clientId=1) para poder ejecutarse a la vez sin
pisarse. Es de solo lectura: no coloca, modifica ni cancela ninguna orden.

Las funciones formatear_*() devuelven texto (html=True para <pre>/<b> de
Telegram, html=False para texto plano de terminal) y son las que reutiliza
telegram_bot_ibkr.py para los comandos /cartera, /hoy, /ayer, /semana.
formatear_posiciones_abiertas() necesita una conexion `ib` YA ABIERTA (pide
precios actuales en vivo); las demas solo leen el historial en disco.

Uso:
    python cartera_ibkr.py                        # hoy
    python cartera_ibkr.py --ayer                  # dia anterior
    python cartera_ibkr.py --semana                # semana laboral actual (lunes a hoy)
    python cartera_ibkr.py --desde 2026-08-01 --hasta 2026-08-15
"""
import argparse
from datetime import date, datetime, timedelta

import bot_completo as bot

CLIENT_ID_CARTERA = 9  # distinto del clientId=1 que usa bot_completo.py, para poder correr a la vez


def parsear_argumentos():
    parser = argparse.ArgumentParser(description="Estado de cartera de IBKR (bot_completo.py)")
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


_MESES_ES = ["ENE", "FEB", "MAR", "ABR", "MAY", "JUN", "JUL", "AGO", "SEP", "OCT", "NOV", "DIC"]


def _formatear_fecha_corta(fecha_hora_iso):
    """'2026-09-03T15:09:12' -> '03 SEP'. Igual que cartera_alpaca.py (peticion
    del usuario, sept. 2026: mismo formato de fecha en los dos bots)."""
    fecha = datetime.fromisoformat(fecha_hora_iso)
    return f"{fecha.day:02d} {_MESES_ES[fecha.month - 1]}"


def _formatear_duracion(delta):
    """Igual que cartera_alpaca.py."""
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


def _fecha_apertura_posicion(operaciones_clave_ordenadas, hasta_fecha_hora):
    """Igual que cartera_alpaca.py, pero sin distinguir REAL/PAPER: IBKR no
    guarda ese campo en el historial (la cuenta paper/real se distingue por
    el puerto de conexion, no por un campo en el registro), asi que no hace
    falta filtrar por eso aqui."""
    cantidad_actual = 0.0
    fecha_apertura = None
    for o in operaciones_clave_ordenadas:
        if o["fecha_hora"] > hasta_fecha_hora:
            break
        if o["lado"] == "COMPRA":
            if cantidad_actual <= 1e-9:
                fecha_apertura = o["fecha_hora"]
            cantidad_actual += o["cantidad"]
        else:
            cantidad_actual = max(0.0, cantidad_actual - o["cantidad"])
    return fecha_apertura


def formatear_posiciones_abiertas(ib, html=False):
    """Necesita una conexion `ib` ya abierta y conectada: pide reqPositions()
    y el precio actual de cada una en vivo. Ver formatear_posiciones_abiertas
    de cartera_alpaca.py para el significado de html=."""
    ib.reqPositions()
    ib.sleep(1)
    posiciones = sorted(
        [p for p in ib.positions() if p.position > 0],
        key=lambda p: (bot.mercado_de_posicion(p), p.contract.symbol)
    )

    titulo_html = "📈 <b>POSICIONES ABIERTAS</b>"
    titulo_plano = "📈 POSICIONES ABIERTAS"
    if not posiciones:
        return (titulo_html if html else titulo_plano) + "\n(ninguna)"

    total_invertido_eur = 0.0
    total_actual_eur = 0.0
    filas = []

    for pos in posiciones:
        contrato = pos.contract
        mercado = bot.mercado_de_posicion(pos)
        cantidad = pos.position
        coste_medio = pos.avgCost

        # Igual que revisar_ventas() en bot_completo.py: el contrato de una
        # posicion CRYPTO llega de ib.positions() con el campo `exchange`
        # vacio, y reqHistoricalData lo rechaza/se queda sin datos sin este
        # relleno (bug real de produccion, sept. 2026 - visto aqui en
        # /cartera y /hoy de telegram_bot_ibkr.py, que usan su propia
        # conexion de solo lectura y no pasan por revisar_ventas).
        if mercado == "CRYPTO" and not contrato.exchange:
            ib.qualifyContracts(contrato)

        if mercado == "CRYPTO":
            comision_estimada = bot.estimar_comision_cripto(cantidad * coste_medio)
        else:
            comision_estimada = bot.estimar_comision(cantidad * coste_medio, contrato.currency, cantidad)
        coste_medio_con_comision = coste_medio + (comision_estimada / cantidad)
        invertido = cantidad * coste_medio_con_comision
        invertido_eur = bot.valor_en_eur(invertido, contrato.currency)
        total_invertido_eur += invertido_eur

        velas = bot.pedir_velas(ib, contrato, '1 D', '1 min')
        if velas:
            precio_actual = velas[-1].close
            valor_actual = cantidad * precio_actual
            valor_actual_eur = bot.valor_en_eur(valor_actual, contrato.currency)
            total_actual_eur += valor_actual_eur
            pl_local = valor_actual - invertido
            pl_eur = valor_actual_eur - invertido_eur
            pl_pct = (pl_local / invertido * 100) if invertido else 0.0
        else:
            total_actual_eur += invertido_eur  # sin dato: asumimos sin cambio
            precio_actual = pl_local = pl_eur = pl_pct = None

        filas.append((mercado, contrato.symbol, cantidad, coste_medio_con_comision, invertido,
                      contrato.currency, precio_actual, pl_local, pl_eur, pl_pct))

    pl_total_eur = total_actual_eur - total_invertido_eur
    pl_total_pct = (pl_total_eur / total_invertido_eur * 100) if total_invertido_eur else 0.0

    if html:
        # Mismo formato de tabla que cartera_alpaca.py y que
        # formatear_operaciones_cerradas() de aqui mismo (peticion del
        # usuario, sept. 2026): Merc. + Ticker + Cant. (max 4 decimales) +
        # Precio (actual, moneda local) + % + EUR, con "abierta desde"
        # debajo de cada fila y una linea en blanco entre posiciones.
        operaciones_todas = bot.cargar_historial_operaciones()
        operaciones_por_clave = {}
        for o in operaciones_todas:
            if o["lado"] in ("COMPRA", "VENTA"):
                clave = bot.clave_historial(o.get("mercado", "?"), o["ticker"])
                operaciones_por_clave.setdefault(clave, []).append(o)
        for lista in operaciones_por_clave.values():
            lista.sort(key=lambda o: o["fecha_hora"])
        ahora_iso = datetime.now().isoformat(timespec="seconds")

        lineas_tabla = [f"  {'Merc.':<6}{'Ticker':<7}{'Cant.':>7}{'Precio':>8}{'%':>8}{'EUR':>8}"]
        for mercado, symbol, cantidad, coste_medio, invertido, currency, precio_actual, pl_local, pl_eur, pl_pct in filas:
            cantidad_str = f"{round(cantidad, 4):g}"
            if pl_eur is None:
                lineas_tabla.append(f"⚪ {mercado:<6}{symbol:<7}{cantidad_str:>7}{'N/D':>8}{'N/D':>8}{'N/D':>8}")
            else:
                precio_str = bot.formato_es(precio_actual)
                pct_str = f"{bot.formato_es(pl_pct, signo=True)}%"
                pl_str = bot.formato_es(pl_eur, signo=True)
                lineas_tabla.append(f"{_emoji_pl(pl_eur)} {mercado:<6}{symbol:<7}{cantidad_str:>7}{precio_str:>8}{pct_str:>8}{pl_str:>8}")
            clave = bot.clave_historial(mercado, symbol)
            fecha_apertura = _fecha_apertura_posicion(operaciones_por_clave.get(clave, []), ahora_iso)
            if fecha_apertura is not None:
                duracion = _formatear_duracion(datetime.now() - datetime.fromisoformat(fecha_apertura))
                lineas_tabla.append(f"  abierta desde {_formatear_fecha_corta(fecha_apertura)} ({duracion})")
            lineas_tabla.append("")
        tabla = "<pre>" + "\n".join(lineas_tabla).rstrip() + "</pre>"
        resumen = (f"<b>TOTAL</b> invertido: {bot.formato_es(total_invertido_eur)} EUR\n"
                  f"P/L: {bot.formato_es(pl_total_eur, signo=True)} EUR "
                  f"({bot.formato_es(pl_total_pct, signo=True)}%)")
        return f"{titulo_html}\n{tabla}\n{resumen}"

    lineas = [titulo_plano]
    for mercado, symbol, cantidad, coste_medio, invertido, currency, precio_actual, pl_local, pl_eur, pl_pct in filas:
        if pl_eur is None:
            lineas.append(f"{mercado} {symbol}: {bot.formato_es(cantidad, 6)} a {bot.formato_es(coste_medio, 4)} "
                          f"{currency} (invertido {bot.formato_es(invertido)} {currency}) - precio actual N/D")
        else:
            lineas.append(f"{mercado} {symbol}: {bot.formato_es(cantidad, 6)} a {bot.formato_es(coste_medio, 4)} "
                          f"{currency} (invertido {bot.formato_es(invertido)} {currency}, ahora "
                          f"{bot.formato_es(precio_actual, 4)} {currency}) "
                          f"P/L {bot.formato_es(pl_local, signo=True)} {currency} / {bot.formato_es(pl_eur, signo=True)} EUR "
                          f"({bot.formato_es(pl_pct, signo=True)}%)")
    lineas.append(f"TOTAL invertido: {bot.formato_es(total_invertido_eur)} EUR | "
                  f"P/L: {bot.formato_es(pl_total_eur, signo=True)} EUR ({bot.formato_es(pl_total_pct, signo=True)}%)")
    return "\n".join(lineas)


def formatear_operaciones_cerradas(desde, hasta, html=False):
    """Solo lee el historial en disco, no necesita conexion `ib`. Ver
    formatear_posiciones_abiertas() para el significado de html=."""
    operaciones = bot.cargar_historial_operaciones()
    ventas = sorted(
        (o for o in operaciones
         if o["lado"] == "VENTA" and desde <= datetime.fromisoformat(o["fecha_hora"]).date() <= hasta),
        key=lambda o: o["fecha_hora"]
    )

    titulo_html = f"📉 <b>OPERACIONES CERRADAS</b> ({desde} a {hasta})"
    titulo_plano = f"📉 OPERACIONES CERRADAS ({desde} a {hasta})"
    if not ventas:
        return (titulo_html if html else titulo_plano) + "\n(ninguna)"

    # Historial COMPLETO por clave (mercado:ticker, sin restringir al rango
    # desde/hasta): hace falta para encontrar la compra de apertura de una
    # posicion que pudo abrirse antes del rango consultado (ver
    # _fecha_apertura_posicion(), igual que en cartera_alpaca.py).
    operaciones_por_clave = {}
    for o in operaciones:
        if o["lado"] in ("COMPRA", "VENTA"):
            clave = bot.clave_historial(o.get("mercado", "?"), o["ticker"])
            operaciones_por_clave.setdefault(clave, []).append(o)
    for lista in operaciones_por_clave.values():
        lista.sort(key=lambda o: o["fecha_hora"])

    filas = []
    ganancia_total_eur = 0.0
    coste_total_eur = 0.0
    importe_total_vendido_eur = 0.0
    for o in ventas:
        cantidad = o["cantidad"]
        coste_medio = o.get("coste_medio")
        precio = o["precio"]
        comision = o.get("comision", 0.0)
        currency = o.get("currency", "?")
        mercado = o.get("mercado", "?")
        beneficio_pct = o.get("beneficio_pct")

        if coste_medio is not None:
            ganancia_local = (precio - coste_medio) * cantidad - comision
            ganancia_eur = bot.valor_en_eur(ganancia_local, currency)
            coste_total_eur += bot.valor_en_eur(coste_medio * cantidad, currency)
        else:
            ganancia_local = ganancia_eur = None
        ganancia_total_eur += ganancia_eur or 0.0
        importe_total_vendido_eur += bot.valor_en_eur(cantidad * precio, currency)
        clave = bot.clave_historial(mercado, o["ticker"])
        fecha_apertura = _fecha_apertura_posicion(operaciones_por_clave.get(clave, []), o["fecha_hora"])
        filas.append((o["fecha_hora"], mercado, o["ticker"], cantidad, precio,
                      currency, ganancia_local, ganancia_eur, beneficio_pct, fecha_apertura))

    beneficio_total_pct = (ganancia_total_eur / coste_total_eur * 100) if coste_total_eur else None

    if html:
        # Mismo formato de tabla que cartera_alpaca.py (peticion del usuario,
        # sept. 2026: que los dos bots den la misma informacion/formato) -
        # Merc. + Ticker + Cant. (max 4 decimales) + Precio (moneda local) +
        # % + EUR (aqui en EUR en vez de USD, porque IBKR opera en varias
        # monedas distintas y el EUR es la unica comun a todas). Debajo de
        # cada fila, junto al emoji verde/rojo, cuanto llevaba abierta la
        # posicion, y una linea en blanco entre operaciones.
        lineas_tabla = [f"  {'Merc.':<6}{'Ticker':<7}{'Cant.':>7}{'Precio':>8}{'%':>8}{'EUR':>8}"]
        for fecha_hora, mercado, ticker, cantidad, precio, currency, ganancia_local, ganancia_eur, beneficio_pct, fecha_apertura in filas:
            emoji = _emoji_pl(ganancia_eur) if ganancia_eur is not None else "⚪"
            cantidad_str = f"{round(cantidad, 4):g}"
            precio_str = bot.formato_es(precio)
            pct_str = f"{bot.formato_es(beneficio_pct, signo=True)}%" if beneficio_pct is not None else "N/D"
            ganancia_str = bot.formato_es(ganancia_eur, signo=True) if ganancia_eur is not None else "N/D"
            lineas_tabla.append(f"{emoji} {mercado:<6}{ticker:<7}{cantidad_str:>7}{precio_str:>8}{pct_str:>8}{ganancia_str:>8}")
            if fecha_apertura is not None:
                duracion = _formatear_duracion(datetime.fromisoformat(fecha_hora) - datetime.fromisoformat(fecha_apertura))
                lineas_tabla.append(f"  abierta desde {_formatear_fecha_corta(fecha_apertura)} ({duracion})")
            lineas_tabla.append("")
        tabla = "<pre>" + "\n".join(lineas_tabla).rstrip() + "</pre>"
        pct_total_str = f" ({bot.formato_es(beneficio_total_pct, signo=True)}% sobre lo invertido, {bot.formato_es(coste_total_eur)} EUR)" \
            if beneficio_total_pct is not None else ""
        resumen = (f"Importe total vendido: {bot.formato_es(importe_total_vendido_eur)} EUR\n"
                  f"<b>TOTAL</b> ganancia/perdida realizada: {bot.formato_es(ganancia_total_eur, signo=True)} EUR{pct_total_str}")
        return f"{titulo_html}\n{tabla}\n{resumen}"

    lineas = [titulo_plano]
    for fecha_hora, mercado, ticker, cantidad, precio, currency, ganancia_local, ganancia_eur, beneficio_pct, fecha_apertura in filas:
        fecha_str = fecha_hora[:16].replace("T", " ")
        ganancia_str = (f", ganancia {bot.formato_es(ganancia_local, signo=True)} {currency} / "
                        f"{bot.formato_es(ganancia_eur, signo=True)} EUR") if ganancia_local is not None else ""
        beneficio_pct_str = f" ({bot.formato_es(beneficio_pct, signo=True)}%)" if beneficio_pct is not None else ""
        lineas.append(f"{fecha_str} {mercado} {ticker}: {bot.formato_es(cantidad, 6)} a {bot.formato_es(precio, 4)} "
                      f"{currency}{ganancia_str}{beneficio_pct_str}")
    pct_total_str = f" ({bot.formato_es(beneficio_total_pct, signo=True)}% sobre lo invertido)" if beneficio_total_pct is not None else ""
    lineas.append(f"TOTAL ganancia/perdida realizada: {bot.formato_es(ganancia_total_eur, signo=True)} EUR{pct_total_str}")
    return "\n".join(lineas)


def formatear_actividad(desde, hasta, html=False):
    """Cuenta cuantas COMPRAS y VENTAS se han ejecutado en el rango de
    fechas, igual que formatear_actividad() de cartera_alpaca.py."""
    operaciones = bot.cargar_historial_operaciones()
    en_rango = [o for o in operaciones if desde <= datetime.fromisoformat(o["fecha_hora"]).date() <= hasta]
    compras = [o for o in en_rango if o["lado"] == "COMPRA"]
    ventas = [o for o in en_rango if o["lado"] == "VENTA"]

    acciones_compradas = sum(o["cantidad"] for o in compras)
    acciones_vendidas = sum(o["cantidad"] for o in ventas)

    titulo = f"📊 <b>ACTIVIDAD</b> ({desde} a {hasta})" if html else f"📊 ACTIVIDAD ({desde} a {hasta})"
    return (f"{titulo}\n"
            f"🟢 Compras: {len(compras)} operaciones, {bot.formato_es(acciones_compradas, 4)} unidades\n"
            f"🔴 Ventas: {len(ventas)} operaciones, {bot.formato_es(acciones_vendidas, 4)} unidades")


def main():
    args = parsear_argumentos()
    desde, hasta = calcular_rango(args)

    ib = bot.IB()
    ib.connect('127.0.0.1', 4002, clientId=CLIENT_ID_CARTERA)
    try:
        print(f"Cartera IBKR - operaciones cerradas: {desde} a {hasta}\n")
        print(formatear_posiciones_abiertas(ib))
        print()
        print(formatear_actividad(desde, hasta))
        print()
        print(formatear_operaciones_cerradas(desde, hasta))
    finally:
        ib.disconnect()


if __name__ == "__main__":
    main()
