# bot_alpaca.py — notas de estado (para retomar en otra conversación)

## Qué es esto

Hermano de `bot_completo.py` (el bot de IBKR): misma lógica de señales MACD
multi-temporalidad y las mismas reglas de compra/venta, pero conectado a
**Alpaca** en vez de a IBKR, y limitado al mercado de **EE.UU.** (Alpaca no
cubre HK ni KR). Pensado para correr **en paralelo** al bot de IBKR — no lo
sustituye, el usuario sigue operando con IBKR en todos los mercados y usa
Alpaca solo para US.

## Por qué Alpaca

- **Comisión 0€** en acciones/ETFs de US vía API (solo tasas regulatorias
  mínimas de SEC/FINRA en ventas, ver más abajo) — mucho más barato que el
  1% que tenías en fracciones de IBKR.
- **API pensada desde el diseño para trading algorítmico** (no como IBKR,
  que necesita IB Gateway/TWS corriendo como aplicación de escritorio) —
  esto es HTTP/REST normal, lo que la hace **mucho más fácil de mover a un
  servidor en la nube** más adelante (el objetivo final del usuario es
  gestionar todo desde el móvil, sin gastos iniciales).
- Fracciones de acción soportadas de forma **nativa** vía API.
- Registrado en la CNMV española (Alpaca Europe).

## Cómo conseguir las API keys

1. Crea una cuenta en <https://alpaca.markets> (empieza con el entorno
   **paper/simulado**, es gratis y no requiere verificación de identidad).
2. En el dashboard, genera un par de claves API (**API Key ID** + **Secret
   Key**) para el entorno paper.
3. Cuando quieras pasar a real, tendrás que verificar identidad/KYC y
   generar un par de claves NUEVO para el entorno live (las de paper no
   sirven para real, y viceversa).

## Variables de entorno necesarias

```
ALPACA_API_KEY=tu_api_key
ALPACA_SECRET_KEY=tu_secret_key
ALPACA_PAPER=true    # true = simulado (por defecto). Pon "false" explícitamente para operar en real.
```

**Importante**: por diseño, si no defines `ALPACA_PAPER`, el bot asume
`true` (paper) — así no hay manera de acabar operando con dinero real "por
accidente" con una variable mal puesta. Solo pasa a real si pones
`ALPACA_PAPER=false` explícitamente.

## Cómo instalar y ejecutar

```
pip install -r requirements.txt
python bot_alpaca.py
```

(o instala solo lo nuevo: `pip install alpaca-py`, ya está en
`requirements.txt`).

## Diferencias de comportamiento frente al bot de IBKR

### Comisiones

**Se asume 0€, tanto en compras como en ventas** — decisión explícita del
usuario (agosto 2026) para simplificar el cálculo. `beneficio_pct` en
`revisar_ventas` es directamente el beneficio bruto, sin ningún descuento.

Nota para el futuro: en la realidad, Alpaca sigue repercutiendo tasas
regulatorias mínimas de SEC/FINRA en ventas (no son comisión de Alpaca,
sino de la SEC/FINRA — del orden de 0.0023% + 0.000119 USD/acción), que se
decidió ignorar por ser insignificantes para el tamaño de cartera actual.
Si la cartera crece mucho, podría valer la pena reintroducirlas para mayor
precisión (el código anterior con `estimar_comision_venta` está en el
historial de git si hace falta recuperarlo).

### Fracciones de acción y horario extendido — restricción real de Alpaca

**Corregido (agosto 2026)**: la versión anterior de esta sección (y del
código) asumía una regla de Alpaca que quedó **obsoleta desde marzo de
2024** — Alpaca amplió el soporte de fracciones para admitirlas también en
órdenes LIMITADAS con horario extendido. El usuario detectó el síntoma en
el log real (ventas fraccionarias omitidas sistemáticamente fuera de
sesión regular) y se verificó contra la documentación oficial de Alpaca
(`docs.alpaca.markets/us/docs/fractional-trading` y el changelog
"Support for Fractional ... with Extended Hours Orders").

Regla real actual:
- Las órdenes **a MERCADO** admiten `qty` fraccionario o `notional`,
  siempre con `time_in_force=DAY`. Alpaca rechaza órdenes a mercado fuera
  de sesión regular.
- Las órdenes **fuera de sesión regular** (pre/postmercado,
  `extended_hours=True`) exigen tipo LIMITADO con `time_in_force=DAY` (o
  GTC) — pero **sí admiten `qty` fraccionario**, ya no exigen cantidad
  entera.
- Es decir: **una posición fraccionaria SÍ se puede comprar y vender fuera
  de sesión regular**, con una orden limitada normal y `qty` fraccionario.

Consecuencias implementadas:
- **Compras y ventas en pre/postmercado**: se manda una orden LIMITADA al
  precio exacto (`extended_hours=True`) con la cantidad fraccionaria
  calculada igual que en sesión regular — ya no se redondea a entero ni se
  omite por ser fracción.
- El código anterior (que forzaba cantidad entera en compras y omitía
  ventas fraccionarias fuera de sesión regular) está en el historial de
  git si hace falta consultarlo.

### Horario

Igual que en `bot_completo.py`: premercado 4:00-9:30 ET, regular
9:30-16:00 ET, postmercado 16:00-20:00 ET. En **postmercado solo se compra,
no se vende** (misma decisión del usuario que en el bot de IBKR). Las
ventanas de seguridad (no comprar en los últimos 90 min, venta forzada en
los últimos 15 min) siguen ancladas al cierre regular (16:00 ET).

**No maneja festivos del mercado** (solo fin de semana) — misma limitación
que el bot de IBKR, documentada ahí también.

### Peticiones de datos: en LOTE, no una por ticker

Diferencia importante de diseño frente a IBKR: Alpaca permite pedir varias
acciones en la MISMA llamada a la API de datos. Como el límite de la API
gratuita es de 200 peticiones/minuto, y el análisis completo son 7
temporalidades × 30 tickers = 210 peticiones si se hiciera una por ticker
(se pasaría del límite), `bot_alpaca.py` pide **cada temporalidad una sola
vez para TODOS los tickers a la vez** (`pedir_velas_lote`) — solo 7
peticiones por ciclo de compras, muy por debajo del límite.

### Vigilante de congelación / archivos de estado

Mismo mecanismo que el bot de IBKR (hilo interno + archivo de latido +
PID), pero con nombres de archivo DISTINTOS
(`latido_bot_alpaca.txt`/`bot_alpaca.pid`) para poder correr los dos bots a
la vez en la misma carpeta sin que se pisen entre ellos.

Supervisor externo ya creado: `run.bot.alpaca.bat` (relanza el bot si
termina) + `vigilante_externo_alpaca.ps1` (mata el proceso si se congela
más de 25 min, análogo a `vigilante_externo.ps1` del bot de IBKR). Mismo
uso: dejar `run.bot.alpaca.bat` corriendo en una ventana y
`powershell -ExecutionPolicy Bypass -File vigilante_externo_alpaca.ps1` en
otra (o como Tarea Programada al iniciar sesión).

**Bug real encontrado y corregido (agosto 2026, primera prueba en
paper)**: fuera de horario de mercado, el bot dormía hasta 30 minutos de
una vez (`time.sleep(min(segundos_espera, 1800))`) sin refrescar el latido
durante la espera — como el umbral de congelación son 20 minutos
(`UMBRAL_CONGELACION_SEGUNDOS`), el vigilante interno confundía esa espera
larga y legítima con una congelación real y mataba el proceso él solo, sin
que hubiera ningún fallo de verdad. Se ve en el log real:
`"Esperando 464 minutos..."` seguido, exactamente 20 minutos después, de
`"[VIGILANTE] 20 minutos sin señal de vida"`. → se añadió
`esperar_en_tramos()`, que trocea la espera en bloques de 60s y refresca el
latido en cada uno (mismo patrón que `esperar_pumpeando()` en
`bot_completo.py`, que ya troceaba las esperas largas por el mismo motivo,
aunque ahí el motivo original era poder detectar cortes de conexión con
IBKR, no solo el latido).

**Segundo bug real, más serio (agosto 2026, primera prueba en paper)**: el
bot se quedó congelado **más de 3 horas** justo después de colocar una
orden de compra (XOM), sin que el vigilante interno reaccionara — hizo
falta que el usuario pulsara Ctrl+C a mano, exactamente el mismo síntoma
que ya vimos con IBKR meses atrás. Causa raíz: **`alpaca-py` (v0.44.0) no
pone ningún `timeout` por defecto en sus peticiones HTTP** (usa
`requests.Session` internamente, y sin `timeout` explícito `requests`
espera indefinidamente). Un corte de red momentáneo mientras se colocaba
la orden dejó la llamada HTTP colgada para siempre — como eso bloquea el
hilo principal a nivel de socket, ni el vigilante interno (que vive en
otro hilo) puede reaccionar, por el mismo motivo del GIL que con IBKR.

A diferencia de IBKR (donde la única solución de verdad era un vigilante
externo, porque `ib_async` no expone ningún control de timeout), aquí sí
se pudo arreglar de raíz: `_forzar_timeout_por_defecto()` envuelve
`cliente._session.request()` para que **toda** petición HTTP a Alpaca
(datos y trading) lleve un timeout de `HTTP_TIMEOUT_SEGUNDOS = 30`
segundos si el llamador no especifica uno propio. Así, un corte de red se
convierte en una excepción normal (`requests.exceptions.Timeout`), que el
`try/except` de cada ticker ya captura y registra en el log — el ciclo
sigue con el siguiente valor en vez de congelarse. El vigilante externo
(`vigilante_externo_alpaca.ps1`) sigue siendo una buena red de seguridad
adicional por si se congela en cualquier otro punto no cubierto por este
timeout, pero ya no es la única defensa.

**Tercer bug real (agosto 2026, primera semana con las fracciones en
horario extendido activadas)**: una venta de META en premercado quedó
como orden LIMITADA abierta sin rellenarse (el precio se alejó del
límite). En un ciclo posterior, con nueva señal de venta, el bot intentó
mandar OTRA orden de venta para la misma posición y Alpaca la rechazó:
`"insufficient qty available for order (requested: 8.3558, available:
0)"`, con `held_for_orders` mostrando que la posición entera seguía
retenida por la orden vieja todavía abierta. El bot nunca cancelaba una
orden que se quedaba sin rellenar, así que se quedaba viva bloqueando
cualquier venta futura de ese valor hasta que expirara sola (o para
siempre, según cómo trate Alpaca el `time_in_force=DAY` en operaciones de
horario extendido). → se añadió `cancelar_ordenes_abiertas(ticker)`, que
se llama justo antes de cada `submit_order` (compra y venta): consulta las
órdenes abiertas de ese ticker (`get_orders` con `status=OPEN`) y las
cancela (`cancel_order_by_id`) antes de mandar la nueva, para que la
cantidad retenida vuelva a estar disponible.

**Mismo bug, segunda vuelta (31 agosto 2026)**: el error
`"insufficient qty available"` volvió a aparecer para varios tickers
(AMZN, QCOM, V, WFC) **con el arreglo anterior ya desplegado**. Causa:
`cancel_order_by_id()` solo ENVÍA la cancelación, pero Alpaca la procesa
de forma asíncrona (la orden pasa primero por `pending_cancel` antes de
llegar a `canceled`) — el código cancelaba y, sin esperar a que la
cancelación se completara de verdad, mandaba la orden nueva justo
después, así que la cantidad todavía podía seguir figurando como
retenida por la orden vieja (condición de carrera). → `cancelar_ordenes_abiertas()`
ahora llama a `esperar_estado_final_orden(orden.id)` después de cancelar
cada orden abierta, para no continuar hasta que la cancelación llegue a
un estado final de verdad.

### Resumen de cierre

Versión simplificada respecto al de IBKR: solo posiciones abiertas (precio
actual, P/L no realizado). Para un resumen completo (abiertas + cerradas,
con rango de fechas), ver `cartera_alpaca.py` más abajo.

### Historial de fecha de apertura

**No implementado todavía** (el `historial_compras.json` persistente que sí
tiene el bot de IBKR). Como el resumen de Alpaca por ahora no muestra fecha
de apertura, no hace falta de momento — pendiente si se añade esa tabla.

### Consulta de cartera a demanda: `cartera_alpaca.py`

Script aparte (agosto 2026), **no toca el bot en marcha**: se puede
ejecutar en cualquier momento, en paralelo a `run.bot.alpaca.bat`, y solo
lee datos (no coloca, modifica ni cancela ninguna orden). Usa las mismas
variables de entorno que `bot_alpaca.py` (`ALPACA_API_KEY`,
`ALPACA_SECRET_KEY`, `ALPACA_PAPER`).

Muestra:
- **Posiciones abiertas**: ticker, cantidad, precio medio, invertido (USD,
  sin comisión — ver más arriba), precio actual, beneficio/pérdida no
  realizado en USD y en EUR, y en %.
- **Posiciones cerradas** en un rango de fechas (por defecto, hoy):
  ticker, cantidad, coste medio, precio de venta, ganancia/pérdida
  realizada en USD y en EUR, y en %.

La tabla de cerradas se lee de un historial persistente nuevo,
`historial_operaciones_alpaca.json` (en `.gitignore`, no se sube al
repo), que `bot_alpaca.py` va rellenando el mismo justo cuando una compra
o venta se confirma como `filled` (`registrar_operacion_historial()`) —
la API de Alpaca no expone directamente el beneficio realizado de una
venta pasada, así que se guarda en el momento en que sí se conoce. Solo
empieza a acumular historial desde que se desplegó esta función; los días
anteriores a eso no tendrán operaciones cerradas que mostrar.

Uso:
```
python cartera_alpaca.py                        # hoy
python cartera_alpaca.py --ayer                  # dia anterior
python cartera_alpaca.py --semana                # semana laboral actual (lunes a hoy)
python cartera_alpaca.py --desde 2026-08-01 --hasta 2026-08-15
```

## Activos

Misma lista de 30 tickers de US que en `bot_completo.py` (`ACTIVOS_US`),
copiada literalmente como lista simple de símbolos (Alpaca no necesita
`exchange`/`currency` por ticker, todo es US/USD).

## Pendiente / próximos pasos

- Probar A FONDO en modo paper antes de pasar a real (en curso).
- Cuando llegue el momento de mover esto a la nube (objetivo declarado del
  usuario: gestionar todo desde el móvil, sin gastos iniciales), este bot
  es el candidato natural para ir primero — no depende de una app de
  escritorio como IB Gateway, solo de las variables de entorno con las API
  keys.
