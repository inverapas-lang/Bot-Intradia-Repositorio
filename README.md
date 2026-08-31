# Bot-Intradia-Repositorio

Dos bots de trading intradía con señales MACD multi-temporalidad, pensados
para correr **en paralelo**:

- **`bot_completo.py`** — sobre Interactive Brokers (IBKR), usando
  `ib_async`. Soporta los mercados US (NYSE/Nasdaq), HK (Hong Kong) y KR
  (Corea). El mercado EU está definido en el código pero desactivado hasta
  contratar la suscripción de datos correspondiente en IBKR.
- **`bot_alpaca.py`** — sobre Alpaca, usando `alpaca-py`. Solo mercado US
  (Alpaca no cubre HK/KR), pero con comisión 0€ en acciones/ETFs y API
  pensada para trading algorítmico (sin necesitar una app de escritorio
  como IB Gateway). Ver `ALPACA_NOTES.md` para el detalle completo.

## Requisitos (bot_completo.py — IBKR)

- Python 3.10+
- IB Gateway o Trader Workstation (TWS) corriendo y con la API habilitada
  (puerto por defecto `4002` para IB Gateway paper trading, configurado en
  `main()` dentro de `bot_completo.py`)
- Dependencias de Python:

```
pip install -r requirements.txt
```

## Ejecución

```
python bot_completo.py
```

El bot corre en bucle continuo (revisión cada 4 minutos mientras algún
mercado esté operativo), imprime todo el log en pantalla y se detiene con
`Ctrl+C`.

En el mercado US se compran fracciones de acción (hasta 4 decimales) cuando
el presupuesto disponible no alcanza para una acción entera, pensado para
carteras pequeñas. En HK y KR, donde IBKR no admite fracciones, se sigue
comprando en acciones/lotes enteros.

**IMPORTANTE:** este script envía órdenes reales a través de la API de IBKR
(aunque sea sobre una cuenta paper). Revisa bien la configuración
(`IMPORTE_EUROS`, `LIMITE_EXPOSICION_PCT`, listas de activos, tipos de
cambio) antes de dejarlo corriendo desatendido.

## Requisitos y ejecución (bot_alpaca.py — Alpaca)

- Python 3.10+
- Cuenta en [Alpaca](https://alpaca.markets) (empieza con el entorno paper,
  es gratis) y un par de claves API.
- Variables de entorno `ALPACA_API_KEY`, `ALPACA_SECRET_KEY` y (opcional)
  `ALPACA_PAPER=false` para pasar a real (por defecto es `true`, paper).

```
pip install -r requirements.txt
python bot_alpaca.py
```

Ver `ALPACA_NOTES.md` para el detalle de las diferencias frente al bot de
IBKR (comisiones, restricciones de fracciones fuera de horario regular,
etc.) y cómo conseguir las claves API.

**IMPORTANTE:** igual que con IBKR, este script envía órdenes reales en
cuanto `ALPACA_PAPER=false`. Revisa bien la configuración antes de dejarlo
corriendo desatendido.

## Consulta de cartera a demanda

Dos scripts de solo lectura (no tocan el bot en marcha, no colocan ni
modifican ninguna orden) para consultar el estado de la cartera cuando
quieras, en paralelo al bot correspondiente:

```
python cartera_ibkr.py       # posiciones abiertas + cerradas (IBKR)
python cartera_alpaca.py     # posiciones abiertas + cerradas (Alpaca)
```

Muestran las posiciones abiertas (cantidad, precio medio, invertido con
comisión, precio actual, beneficio/pérdida no realizado en moneda local y
en EUR, %) y las operaciones cerradas en un rango de fechas configurable
(hoy por defecto):

```
python cartera_alpaca.py --ayer
python cartera_alpaca.py --semana                # semana laboral actual (lunes a hoy)
python cartera_alpaca.py --desde 2026-08-01 --hasta 2026-08-15
```

Ver `NOTES.md` / `ALPACA_NOTES.md` para el detalle del historial persistente
del que se leen las operaciones cerradas.

## Control desde Telegram (bot_alpaca.py)

`bot_alpaca.py` manda avisos de Telegram automáticamente (compras, ventas,
resumen diario) si se configuran `TELEGRAM_BOT_TOKEN`/`TELEGRAM_CHAT_ID`
(opcional). Además, `telegram_bot.py` es un proceso aparte que permite
arrancar/parar el bot y consultar la cartera con comandos de Telegram desde
el móvil (`/estado`, `/arrancar`, `/parar`, `/cartera`, `/hoy`, `/ayer`,
`/semana`, `/log`). Ver `ALPACA_NOTES.md` para cómo crear el bot de
Telegram y desplegar `telegram_bot.py`.
