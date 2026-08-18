"""
cartera_ibkr.py - consulta A DEMANDA del estado de la cartera de IBKR
(bot_completo.py), sin tocar el bot que este corriendo en ese momento:

  - Posiciones ABIERTAS en todos los mercados activos (US/HK/KR): cantidad,
    precio medio, total invertido (con comision estimada), precio actual,
    beneficio/perdida no realizado en moneda local y en EUR, y en %.
  - Posiciones CERRADAS en un rango de fechas (hoy por defecto): se leen del
    historial persistente que bot_completo.py va guardando en
    "historial_operaciones_ibkr.json" cada vez que una venta se ejecuta con
    exito -reqExecutions() de IBKR solo devuelve las ejecuciones del dia
    actual, asi que no sirve para consultar dias anteriores-.

Se conecta a IB Gateway/TWS con un clientId DISTINTO al del bot en marcha
(bot_completo.py usa clientId=1) para poder ejecutarse a la vez sin
pisarse. Es de solo lectura: no coloca, modifica ni cancela ninguna orden.

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


def imprimir_posiciones_abiertas(ib):
    ib.reqPositions()
    ib.sleep(1)
    posiciones = sorted(
        [p for p in ib.positions() if p.position > 0],
        key=lambda p: (bot.CURRENCY_A_MERCADO.get(p.contract.currency, "?"), p.contract.symbol)
    )

    print("\n=== POSICIONES ABIERTAS ===")
    if not posiciones:
        print("(ninguna)")
        return

    cab = (f"{'Mercado':<9}{'Ticker':<10}{'Cantidad':>10}{'Precio medio':>15}{'Invertido':>15}"
           f"{'Precio actual':>15}{'P/L local':>13}{'P/L EUR':>12}{'P/L %':>9}")
    print(cab)

    total_invertido_eur = 0.0
    total_actual_eur = 0.0

    for pos in posiciones:
        contrato = pos.contract
        mercado = bot.CURRENCY_A_MERCADO.get(contrato.currency, "?")
        cantidad = pos.position
        coste_medio = pos.avgCost

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
            precio_str = f"{precio_actual:.4f}"
            pl_local_str = f"{pl_local:.2f}"
            pl_eur_str = f"{pl_eur:.2f}"
            pl_pct_str = f"{pl_pct:.2f}%"
        else:
            total_actual_eur += invertido_eur  # sin dato: asumimos sin cambio
            precio_str = pl_local_str = pl_eur_str = pl_pct_str = "N/D"

        print(f"{mercado:<9}{contrato.symbol:<10}{cantidad:>10.4g}{coste_medio_con_comision:>15.4f}"
              f"{invertido:>15.2f}{precio_str:>15}{pl_local_str:>13}{pl_eur_str:>12}{pl_pct_str:>9}")

    pl_total_eur = total_actual_eur - total_invertido_eur
    pl_total_pct = (pl_total_eur / total_invertido_eur * 100) if total_invertido_eur else 0.0
    print("-" * len(cab))
    print(f"TOTAL invertido: {total_invertido_eur:.2f} EUR | valor actual: {total_actual_eur:.2f} EUR | "
          f"P/L: {pl_total_eur:.2f} EUR ({pl_total_pct:.2f}%)")


def imprimir_operaciones_cerradas(desde, hasta):
    operaciones = bot.cargar_historial_operaciones()
    ventas = sorted(
        (o for o in operaciones
         if o["lado"] == "VENTA" and desde <= datetime.fromisoformat(o["fecha_hora"]).date() <= hasta),
        key=lambda o: o["fecha_hora"]
    )

    print(f"\n=== OPERACIONES CERRADAS ({desde} a {hasta}) ===")
    if not ventas:
        print("(ninguna)")
        return

    cab = (f"{'Fecha/hora':<17}{'Mercado':<9}{'Ticker':<10}{'Cantidad':>10}{'Coste medio':>13}"
           f"{'Precio venta':>13}{'Ganancia local':>15}{'Ganancia EUR':>13}{'%':>8}")
    print(cab)

    ganancia_total_eur = 0.0
    for o in ventas:
        cantidad = o["cantidad"]
        coste_medio = o.get("coste_medio")
        precio = o["precio"]
        comision = o.get("comision", 0.0)
        currency = o.get("currency", "?")
        beneficio_pct = o.get("beneficio_pct")

        if coste_medio is not None:
            ganancia_local = (precio - coste_medio) * cantidad - comision
            ganancia_eur = bot.valor_en_eur(ganancia_local, currency)
        else:
            ganancia_local = ganancia_eur = None
        ganancia_total_eur += ganancia_eur or 0.0

        fecha_str = o["fecha_hora"][:16].replace("T", " ")
        ganancia_local_str = f"{ganancia_local:.2f}" if ganancia_local is not None else "N/D"
        ganancia_eur_str = f"{ganancia_eur:.2f}" if ganancia_eur is not None else "N/D"
        beneficio_pct_str = f"{beneficio_pct:.2f}%" if beneficio_pct is not None else "N/D"
        print(f"{fecha_str:<17}{o.get('mercado', '?'):<9}{o['ticker']:<10}{cantidad:>10.4g}"
              f"{(coste_medio or 0):>13.4f}{precio:>13.4f}{ganancia_local_str:>15}"
              f"{ganancia_eur_str:>13}{beneficio_pct_str:>8}")

    print("-" * len(cab))
    print(f"TOTAL ganancia/perdida realizada: {ganancia_total_eur:.2f} EUR")


def main():
    args = parsear_argumentos()
    desde, hasta = calcular_rango(args)

    ib = bot.IB()
    ib.connect('127.0.0.1', 4002, clientId=CLIENT_ID_CARTERA)
    try:
        print(f"Cartera IBKR - operaciones cerradas: {desde} a {hasta}")
        imprimir_posiciones_abiertas(ib)
        imprimir_operaciones_cerradas(desde, hasta)
    finally:
        ib.disconnect()


if __name__ == "__main__":
    main()
