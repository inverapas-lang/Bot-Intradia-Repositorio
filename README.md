# Bot-Intradia-Repositorio

Bot de trading intradía sobre Interactive Brokers (IBKR), usando `ib_async` y
señales MACD multi-temporalidad. Soporta los mercados US (NYSE/Nasdaq), HK
(Hong Kong) y KR (Corea). El mercado EU está definido en el código pero
desactivado hasta contratar la suscripción de datos correspondiente en IBKR.

## Requisitos

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
