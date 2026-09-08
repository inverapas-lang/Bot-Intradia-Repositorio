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

**Opcionales, solo para `/carterapaper` de Telegram** (añadido sept. 2026, petición del
usuario: tras pasar a real, poder seguir consultando el estado de la cuenta paper sin cambiar
el bot de modo):
```
ALPACA_PAPER_API_KEY=tu_api_key_de_la_cuenta_paper
ALPACA_PAPER_SECRET_KEY=tu_secret_key_de_la_cuenta_paper
```
Son las claves de tu cuenta **paper** (distintas de `ALPACA_API_KEY`/`ALPACA_SECRET_KEY`, que
son las de la cuenta con la que opera el bot — ahora la real). Si no las defines,
`/carterapaper` responde con un aviso claro en vez de fallar; el resto de comandos funcionan
igual sin ellas.

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

### Criterio de venta: trailing stop + refuerzo de 2 velas + salida parcial (sept. 2026)

Cambio a petición del usuario, idéntico en concepto al de `bot_completo.py` (misma lógica de
MACD, no depende del bróker) — sustituye al antiguo "beneficio ≥umbral + 1 vela de 5min
bajista". Se aplicó primero solo a `revisar_ventas()` (acciones), y después se **extendió
también a `revisar_ventas_cripto()`** (petición explícita del usuario) — ambas comparten la
misma función `decidir_accion_venta()`, cada una con su propio umbral (`UMBRAL_BENEFICIO_PCT`
0.5% en acciones, `UMBRAL_BENEFICIO_CRYPTO_PCT` 0.3% en cripto) y su propia comisión ya
restada del beneficio neto (0 en acciones, real de Alpaca en cripto).

- `_maximo_beneficio_neto_por_posicion` / `_scale_out_realizado` (dict/set en memoria,
  clave = ticker) trackean el beneficio máximo alcanzado y si ya se hizo la salida parcial
  de cada posición desde que se abrió. Acciones y cripto **comparten** este dict/set (sin
  colisión de claves: los símbolos de cripto llevan "/"), pero cada función (`revisar_ventas()`
  a 130s, `revisar_ventas_cripto()` a 60s) poda solo su propio subconjunto al principio de
  cada ciclo (filtrando por `es_cripto()`), para no borrarle el seguimiento a la otra.
- **Salida parcial** (nueva, petición del usuario: *"vender la mitad al primer objetivo, dejar
  correr el resto con trailing stop"*): la PRIMERA vez que el máximo neto alcanza el umbral,
  se vende `PORCENTAJE_SCALE_OUT` (50%) de la posición para asegurar beneficio ya, dejando el
  resto corriendo. Si la cantidad/importe restante sale demasiado pequeño para dividir con
  sentido (por debajo de `VALOR_MINIMO_OPERACION_CRIPTO_USD` en cripto, o ≥ la cantidad total
  en acciones), se vende todo de una vez en su lugar.
- **Trailing stop (principal, vende el 100% de lo que quede)**: se arma solo cuando el máximo
  alcanza el umbral. Desde ahí, si el beneficio actual retrocede `TRAILING_STOP_VENTA_PCT`
  (0.3 puntos) desde ese máximo, vende TODO lo que quede — incluso si ya cayó a pérdida.
- **Refuerzo (secundario, también vende el 100% de lo que quede)**: si el beneficio actual ya
  está en el umbral o por encima, y las 2 últimas velas de 5 min seguidas son bajistas
  (`macd_5min_bajista_2_velas` en acciones, `macd_5min_bajista_cripto_2_velas` en cripto, no
  solo la última como antes), también vende TODO lo que quede.
- Ninguno de los tres (parcial, trailing, refuerzo) vende por debajo del umbral.
- El máximo y la marca de parcial se olvidan (`cerrar_seguimiento_venta()`) SOLO tras una
  venta TOTAL, nunca tras una parcial, y se podan al principio de cada ciclo para cualquier
  ticker ya no tenido — si se recompra más tarde, el trailing empieza de cero.

### Venta forzada: a mercado, no limitada (sept. 2026)

Cambio a petición del usuario: antes la venta forzada (últimos 15 min antes del cierre,
beneficio entre 0.5% y 2%) usaba una orden LIMITADA 0.2% por debajo del precio actual, para
intentar mejorar el precio de salida. Ahora usa una orden A MERCADO — el objetivo de la venta
forzada es garantizar la salida antes del cierre, y la orden límite corría el riesgo de no
ejecutarse a tiempo si el precio se alejaba del límite. Se prioriza la ejecución garantizada
sobre el pequeño margen de precio que daba el límite anterior. Mismo cambio en
`bot_completo.py` (acciones US/HK/KR).

### Horario

Igual que en `bot_completo.py`: premercado 4:00-9:30 ET, regular
9:30-16:00 ET, postmercado 16:00-20:00 ET. En **postmercado solo se compra,
no se vende** (misma decisión del usuario que en el bot de IBKR). Las
ventanas de seguridad (no comprar en los últimos 90 min, venta forzada en
los últimos 15 min) siguen ancladas al cierre regular (16:00 ET).

**Festivos de NYSE/Nasdaq: SÍ los detecta** (añadido sept. 2026, petición del
usuario: *"puede el bot identificar los días que el mercado no va a estar
abierto... hoy es festivo en US"*). `festivos_nyse(year)`/`es_festivo_us(fecha)`
calculan por regla (no una lista fija que haya que mantener a mano cada
año) los 10 festivos anuales de NYSE/Nasdaq: Año Nuevo, MLK Day, Washington's
Birthday, Good Friday (requiere calcular Pascua), Memorial Day, Juneteenth
(desde 2022), Independence Day, Labor Day, Thanksgiving y Navidad — con la
regla de observancia estándar si caen en fin de semana (sábado → viernes
anterior, domingo → lunes siguiente). `es_horario_operativo()`,
`en_postmercado_us()` y `fuera_de_sesion_regular_us()` ya lo tienen en
cuenta, así que ningún sitio del bot necesita cambios adicionales.
Verificado contra el calendario oficial 2025/2026 (incluido el caso real
que reportó el usuario: Labor Day 2026 cae en 7 de septiembre).

### Peticiones de datos: en LOTE, no una por ticker

Diferencia importante de diseño frente a IBKR: Alpaca permite pedir varias
acciones en la MISMA llamada a la API de datos. Como el límite de la API
gratuita es de 200 peticiones/minuto, y el análisis completo son 7
temporalidades × 30 tickers = 210 peticiones si se hiciera una por ticker
(se pasaría del límite), `bot_alpaca.py` pide **cada temporalidad una sola
vez para TODOS los tickers a la vez** (`pedir_velas_lote`) — solo 7
peticiones por ciclo de compras, muy por debajo del límite. Alpaca no tiene
el problema de pacing de IBKR (no hace falta cachear por eso), pero la
caché de temporalidades largas de abajo se comparte con IBKR por
consistencia de señal entre ambos bots.

### Caché de temporalidades LARGAS (día/semana), con vela en curso condicional

Añadido sept. 2026, petición del usuario: con un horizonte de trading de
HORAS, día y semana se usan como filtro de fondo (evitar comprar contra la
tendencia dominante), no como señal de entrada — para eso ya están las
cortas (1min-1h), que se piden en lote en cada ciclo sin caché.
`_resultados_largas_cacheados()` (compartida por `analizar_todos_los_activos()`
y `analizar_todos_los_activos_cripto()`) implementa dos modos:

- **Modo "cerrada"** (por debajo de `UMBRAL_FRACCION_VELA_EN_CURSO` = 40%
  del día/semana transcurrido): usa solo barras YA CERRADAS
  (`histograma.iloc[-2]` vs `iloc[-4]`, nunca la última vela — la de
  hoy/esta semana, que sigue formándose y es ruido puro al principio del
  periodo, p.ej. un lunes a primera hora). Se cachea una única vez al día
  (no puede cambiar, son datos cerrados).
- **Modo "en_curso"** (a partir del 40%): SÍ se incluye la vela en
  formación (`histograma.iloc[-1]` vs `iloc[-3]`, ya tiene información
  real de sobra), pero se refresca cada `INTERVALO_REFRESCO_VELA_EN_CURSO_SEGUNDOS`
  (1h) en vez de en cada ciclo de 1-2 minutos — sigue ahorrando peticiones
  sin quedarse con un dato de horas atrás.

La fracción transcurrida (`_fraccion_transcurrida_del_dia`/`_de_la_semana`)
usa la sesión 4:00-20:00 ET para acciones (`VENTANA_DIA_POR_MERCADO`) y el
día/semana de calendario UTC completo para cripto (que no tiene "cierre"
de mercado). Acciones y cripto comparten el mismo caché sin colisión: se
indexa por nombre de temporalidad ("1 dia"/"1 semana"), no por ticker.

### Avisos de Telegram ante fallos (sept. 2026, petición del usuario: "vigila el bot y avísame si algo falla")

Hasta ahora `notificar_telegram()` solo se usaba para compras/ventas/resúmenes — un fallo
real (congelación, error fatal del bucle principal) solo quedaba en el log. Se añadieron
avisos por Telegram en:
- **Congelación del proceso** (`vigilante_congelacion`): aviso justo antes de forzar el
  cierre (`os._exit(1)`) — corre en su propio hilo, así que este aviso sí puede salir aunque
  el hilo principal esté congelado.
- **Error fatal fuera del ciclo principal**: aviso con el tipo de excepción antes de
  reiniciar el ciclo.

A diferencia de `bot_completo.py`/IBKR, aquí no hace falta un cortacircuitos de datos caídos
(Alpaca pide en lote, muy por debajo del límite de la API) ni una reconexión explícita
(HTTP normal, sin sesión persistente que reconectar), así que no hay avisos equivalentes a
esos dos.

### Pantalla del PC: el bot NO debe forzarla a quedarse encendida (sept. 2026)

Mismo cambio que en `bot_completo.py` (ver `NOTES.md` para la explicación completa):
`evitar_suspension_windows()` ya no incluye `ES_DISPLAY_REQUIRED`, solo evita que el SISTEMA
se suspenda/hiberne, sin forzar la pantalla a quedarse encendida. En la práctica esto no
aplica ahora mismo (el bot corre en el servidor AWS/Linux, donde la función no hace nada),
pero se mantiene por si se ejecuta alguna vez en Windows.

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

**Consecuencia observada (31 agosto 2026): operaciones "huérfanas" no
registradas en el historial.** Tras migrar a AWS, `/hoy` en Telegram decía
"ninguna operación cerrada hoy" pese a que en el panel de Alpaca sí
aparecían ventas reales de QCOM (y compras de DIS/WFC/INTC/F/V) ese mismo
día. Diagnóstico: `sudo journalctl -u bot-alpaca` no tenía **ninguna**
mención a QCOM en todo el día — el bot en marcha nunca colocó esa orden.
Explicación: la orden de venta de QCOM (`held_for_orders`) que se vio
atascada en el log de esa misma mañana (antes del segundo arreglo de
cancelación) quedó abierta en Alpaca durante horas; más tarde, **por su
cuenta**, el mercado alcanzó su precio límite y se ejecutó sola (en varias
ejecuciones parciales), sin que ningún proceso en marcha (ni el bot de
Windows, ya parado, ni el nuevo de AWS) la hubiera colocado en ese momento.

`registrar_operacion_historial()` solo anota una operación en el instante
en que el propio bot la coloca y confirma su ejecución — una orden
huérfana de un proceso que ya no corre, que se ejecuta por su cuenta más
tarde, no la ve ningún bot en marcha y por tanto no queda registrada.
**No es un bug del registro en sí**, sino una consecuencia colateral de
los dos bugs de cancelación ya corregidos: con esos arreglos desplegados,
no deberían quedar más órdenes huérfanas a partir de ahora, así que las
operaciones nuevas sí deberían registrarse con normalidad. Si en algún
momento se quiere que `/hoy`/`/ayer`/etc. sean 100% fieles incluso a estos
restos antiguos, habría que reconciliar el historial local contra
`get_orders()` de Alpaca (que sí tiene el historial real completo,
independientemente de quién colocó cada orden) — pendiente, no
implementado por decisión expresa del usuario (con que las operaciones
futuras queden bien registradas es suficiente por ahora).

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

**Distinción REAL/PAPER (añadido sept. 2026, petición del usuario)**: el historial se acumula
entre cambios de modo del bot (p.ej. cuando se pasó de paper a real, sin borrar el archivo), así
que sin más contexto no se podría saber a posteriori qué operaciones fueron con dinero real. Se
añadió el campo `"modo"` (`"REAL"` o `"PAPER"`) a cada registro de
`registrar_operacion_historial()`, tomado de `ALPACA_PAPER` en el momento de la operación. Las
operaciones anteriores a este cambio (sin el campo) se tratan como `"PAPER"` — el único modo que
existía entonces. Esto se refleja en `cartera_alpaca.py` (y por tanto en `/hoy`, `/ayer`,
`/semana` de Telegram, ver más abajo):
- `formatear_actividad()`: desglosa compras/ventas por modo, p.ej. "3 operaciones (1 REAL, 2 PAPER)".
- `formatear_operaciones_cerradas()`: cada fila lleva `[REAL]`/`[PAPER]` (texto plano) o el emoji
  💰 (REAL) / 🧪 (PAPER) (tablas HTML de Telegram).
- `formatear_posiciones_abiertas()`: el título indica el modo ACTUAL de conexión del bot
  (`[REAL]`/`[PAPER]`) — a diferencia de las cerradas, las posiciones abiertas vienen en vivo de
  la API de Alpaca, así que siempre están en el modo con el que está conectado el bot en ese
  momento, no mezcladas.

## Control y consulta desde el móvil (Telegram)

Desde agosto 2026, además de correr en un servidor en la nube (ver más
abajo), el bot se puede **gestionar y consultar desde Telegram**: avisos
automáticos de cada compra/venta y del resumen diario, y comandos para
arrancar/parar el bot y consultar la cartera sin tener que entrar por SSH.

### Piezas involucradas

- **`bot_alpaca.py`** (el bot de trading): manda un mensaje de Telegram
  automáticamente cada vez que se ejecuta una compra o una venta, y en el
  resumen diario de cierre (`generar_resumen()`). Función clave:
  `notificar_telegram()`. Si `TELEGRAM_BOT_TOKEN`/`TELEGRAM_CHAT_ID` no
  están configuradas, esta función simplemente no hace nada — el bot
  funciona exactamente igual sin Telegram, es un extra opcional.
- **`telegram_bot.py`** (proceso NUEVO y APARTE, con su propio servicio
  systemd): escucha los mensajes que le mandas por Telegram (comandos) y
  responde. Vive en un proceso separado del bot de trading a propósito —
  un fallo aquí no puede afectar al trading, y viceversa.
- **`cartera_alpaca.py`**: sus funciones `formatear_posiciones_abiertas()`
  y `formatear_operaciones_cerradas()` (antes solo imprimían por
  terminal) ahora devuelven texto, para que `telegram_bot.py` las
  reutilice directamente en los comandos `/cartera`, `/hoy`, `/ayer`,
  `/semana` — la misma lógica y datos, sin duplicar código.

### Comandos disponibles en Telegram

```
/estado    - si el bot esta corriendo o parado
/arrancar  - arranca el bot (systemctl start bot-alpaca)
/parar     - para el bot (systemctl stop bot-alpaca)
/cartera   - posiciones abiertas (igual que cartera_alpaca.py)
/carterapaper - posiciones abiertas de la cuenta PAPER, aparte de la activa
             (requiere ALPACA_PAPER_API_KEY/ALPACA_PAPER_SECRET_KEY, ver arriba)
/hoy       - actividad de hoy (num. compras/ventas y acciones de cada
             lado) + detalle de las ventas cerradas
/ayer      - lo mismo, del dia anterior
/semana    - lo mismo, de la semana laboral actual (lunes a hoy)
/log       - actividad reciente (compras, ventas, avisos, errores), en
             lista y sin el ruido rutinario de cada ciclo (journalctl)
/ayuda     - lista de comandos
```

`/hoy`, `/ayer` y `/semana` combinan dos piezas: primero
`cartera_alpaca.formatear_actividad()` (cuenta las operaciones —tanto
COMPRA como VENTA— del historial en ese rango: numero de operaciones y
acciones totales de cada lado), y despues
`cartera_alpaca.formatear_operaciones_cerradas()` (el detalle linea a
linea de las ventas, con su beneficio/perdida realizado, que ya existía).

Solo responde al chat configurado en `TELEGRAM_CHAT_ID` — cualquier otro
mensaje de cualquier otro chat se ignora y se registra en el log de
`telegram_bot.py`.

### Cómo crear el bot de Telegram

1. En Telegram, busca **@BotFather** y escríbele `/newbot`.
2. Ponle un nombre y un usuario (debe terminar en `bot`, p. ej.
   `mi_bot_alpaca_bot`).
3. BotFather te da un **token** (algo como
   `123456789:AAExxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx`) — es tu
   `TELEGRAM_BOT_TOKEN`.
4. Para conseguir tu `TELEGRAM_CHAT_ID`: escríbele cualquier mensaje a tu
   bot nuevo desde tu cuenta de Telegram, y luego visita en el navegador
   (sustituyendo el token):
   `https://api.telegram.org/bot<TU_TOKEN>/getUpdates`
   Busca en la respuesta JSON el campo `"chat":{"id": ...}` — ese número es
   tu `TELEGRAM_CHAT_ID`.

### Variables de entorno adicionales

```
TELEGRAM_BOT_TOKEN=el_token_de_botfather
TELEGRAM_CHAT_ID=tu_chat_id_numerico
```

Van en el mismo sitio que las de Alpaca (el archivo `.env` que lee el
`EnvironmentFile` de los servicios systemd, ver más abajo).

### Permiso de sudo para arrancar/parar el bot (`/arrancar`, `/parar`)

`telegram_bot.py` corre como el usuario normal (`ubuntu`), pero necesita
poder ejecutar `systemctl start/stop bot-alpaca` sin que le pida
contraseña (si no, esos dos comandos fallan con un aviso claro, aunque el
resto —`/estado`, `/cartera`, `/log`, etc., que son de solo lectura—
funcionan igual). Para darle permiso, en el servidor:

```bash
sudo visudo -f /etc/sudoers.d/telegram-bot-alpaca
```

Y añade esta línea exacta (sustituye `ubuntu` si tu usuario se llama
distinto):

```
ubuntu ALL=(ALL) NOPASSWD: /usr/bin/systemctl start bot-alpaca, /usr/bin/systemctl stop bot-alpaca
```

Guarda y sal. Esto da permiso **únicamente** para esos dos comandos
exactos, no para systemctl en general — no relajes esto a `ALL` sin
necesidad.

### Servicio systemd de `telegram_bot.py`

Igual que `bot-alpaca.service`, pero apuntando a `telegram_bot.py`:

```ini
[Unit]
Description=Telegram bot control (Alpaca)
After=network.target

[Service]
Type=simple
User=ubuntu
WorkingDirectory=/home/ubuntu/Alpaca
EnvironmentFile=/home/ubuntu/Alpaca/.env
ExecStart=/home/ubuntu/Alpaca/venv/bin/python /home/ubuntu/Alpaca/telegram_bot.py
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
```

Guardar en `/etc/systemd/system/telegram-bot.service`, y luego:

```bash
sudo systemctl daemon-reload
sudo systemctl enable telegram-bot
sudo systemctl start telegram-bot
sudo systemctl status telegram-bot
```

## Activos

Misma lista de 30 tickers de US que en `bot_completo.py` (`ACTIVOS_US`),
copiada literalmente como lista simple de símbolos (Alpaca no necesita
`exchange`/`currency` por ticker, todo es US/USD).

## Cripto en Alpaca (añadido sept. 2026, petición del usuario)

Igual que se hizo antes con `bot_completo.py`/IBKR, se añadió trading de
criptomonedas a `bot_alpaca.py`, en paralelo a las acciones (no las
sustituye). A diferencia de IBKR, aquí no hizo falta ninguna cadena de
"prueba y error" en producción: se verificó el comportamiento real de
`alpaca-py` (formato de símbolo, tipos de orden admitidos, fees) contra la
documentación oficial y contra el propio paquete instalado ANTES de
escribir el código, precisamente para no repetir el via crucis de IBKR.

### Activos: `ACTIVOS_CRYPTO`

Mismas 6 monedas que en IBKR (petición explícita del usuario, "las mismas
que en IBKR"): `BTC/USD`, `ETH/USD`, `LTC/USD`, `BCH/USD`, `SOL/USD`,
`LINK/USD`. Formato de símbolo con barra (`"BASE/USD"`), que es como
Alpaca identifica un par cripto — se usa precisamente esa barra como señal
para distinguir cripto de acciones: `es_cripto(ticker) = "/" in ticker`.

**Aviso pendiente de confirmar**: SOL no aparece en la lista de pares
cripto soportados por Alpaca que se pudo verificar por búsqueda externa
(sí están confirmados BTC, ETH, LTC, BCH, LINK). Se incluyó de todos modos
por ser petición explícita del usuario ("las mismas que en IBKR") — si
Alpaca lo rechaza, el propio manejo de errores por ticker del bot lo
registrará en el log sin afectar a las demás monedas ni al resto del bot.
Si aparece un error de símbolo no soportado para SOL, quitarlo de
`ACTIVOS_CRYPTO` sin más.

### Comisión: SÍ existe, a diferencia de acciones

Alpaca no cobra comisión en acciones, pero **sí en cripto** — algo
verificado por búsqueda externa antes de asumir "gratis" otra vez. Tabla
de fees por volumen de 30 días (a partir de la documentación de Alpaca);
solo el primer nivel es realista para el tamaño de cuenta de este bot:

| Volumen 30 días | Maker | Taker |
|---|---|---|
| 0 - 100k USD | 0.15% | 0.25% |
| ... (niveles superiores, no aplicables aquí) | | |

Este bot manda órdenes IOC (ver más abajo), que actúan como "taker" (se
ejecutan contra órdenes ya existentes en el libro, no aportan liquidez
nueva) → se asume siempre el fee **taker del nivel 1: 0.25%**
(`COMISION_CRIPTO_ALPACA_PCT`), tanto en la compra como en la venta (round
trip ≈ 0.5%). `estimar_comision_cripto_alpaca(valor_operado)` centraliza
este cálculo.

### Umbral de beneficio para vender: 0.3% (neto, tras comisión)

Igual que en IBKR (misma decisión del usuario), el umbral para cripto es
más bajo que el de acciones: `UMBRAL_BENEFICIO_CRYPTO_PCT = 0.3`. A
diferencia de acciones (donde se ignora la comisión, ver más arriba), en
`revisar_ventas_cripto()` el beneficio SÍ se calcula neto de la comisión
estimada (compra + venta) antes de compararlo con el umbral — porque aquí
la comisión no es insignificante y sí puede convertir una venta
aparentemente rentable en pérdida.

### Tipo de orden y `time_in_force`: solo IOC (no DAY)

Verificado contra la documentación de Alpaca: las órdenes cripto en Alpaca
**no admiten `TimeInForce.DAY`**, solo `GTC` o `IOC`. Se eligió `IOC`
(Immediate-Or-Cancel) para ambos lados:
- **Compras**: `MarketOrderRequest(symbol=ticker, notional=importe_usd,
  time_in_force=TimeInForce.IOC)` — órdenes a mercado por importe en
  dólares (no por cantidad), igual que las compras de acciones.
- **Ventas**: `LimitOrderRequest(symbol=ticker, qty=cantidad,
  limit_price=precio_actual, time_in_force=TimeInForce.IOC)` — orden
  limitada al precio exacto del momento, cantidad fraccionaria nativa (sin
  redondeo a incrementos, a diferencia de IBKR — Alpaca admite decimales
  libremente en cripto).

### Límite de exposición TOTAL en cripto: 20% de la cartera completa

Petición explícita del usuario: *"que el límite de la cantidad con la que
se puede operar en cripto sea del 20% del total de la cuenta
(acciones+cash+cripto)"*. Implementado en `revisar_compras_cripto()` como
una segunda comprobación, ADEMÁS del límite normal por posición que ya
existía (`LIMITE_EXPOSICION_PCT`, ~15% por activo):

```python
LIMITE_EXPOSICION_CRYPTO_TOTAL_PCT = 20
exposicion_total_cripto = calcular_exposicion_total_cripto_usd(posiciones)
limite_total = cuenta.portfolio_value * LIMITE_EXPOSICION_CRYPTO_TOTAL_PCT / 100
if exposicion_total_cripto + importe_nueva_compra > limite_total:
    # se omite la compra
```

Detalle clave: `cuenta.portfolio_value` (de `get_account()` de Alpaca) ya
representa el valor TOTAL de la cuenta — acciones + cash + cripto, todo
junto — así que el "20% del total de la cuenta (acciones+cash+cripto)"
pedido por el usuario se cumple literalmente sin ningún cálculo adicional,
a diferencia de IBKR donde hubo que construir esa suma a mano.
`calcular_exposicion_total_cripto_usd(posiciones)` es una función genérica
que suma el valor a coste de las posiciones cuyo ticker es cripto
(`es_cripto`), ignorando las de acciones — la misma función sirve también
para excluir cripto de `revisar_ventas()` (ver más abajo).

### Datos de mercado: cliente y peticiones separadas

Alpaca separa los datos de acciones (`StockHistoricalDataClient`) de los
de cripto (`CryptoHistoricalDataClient`) — son clases y endpoints
distintos, aunque el resto de la forma de la petición
(`CryptoBarsRequest`) es igual que `StockBarsRequest`. Se añadieron
funciones paralelas dedicadas a cripto en vez de intentar generalizar las
de acciones dentro de la misma función: `pedir_velas_lote_cripto()`,
`analizar_todos_los_activos_cripto()`, `precio_actual_ticker_cripto()`,
`macd_5min_bajista_cripto()`. **No requiere ninguna suscripción de datos
de pago ni permiso adicional** en Alpaca (a diferencia de IBKR, donde el
mercado de cripto exigió toda una cadena de permisos de datos aparte) —
sí conviene verificar que la cuenta tenga el trading de cripto habilitado
en el dashboard de Alpaca si nunca se ha operado cripto ahí antes.

### Ciclo del bot: sin horario, 24/7, y en su PROPIA cadencia (cada 1 min)

A diferencia de acciones (premercado/regular/postmercado/cerrado), cripto
cotiza 24/7 sin horario de mercado. Se quitó por completo la puerta de
`if not es_horario_operativo(): sleep varias horas; continue` que tenía
antes el bucle principal (`main()`) — código ahora muerto y eliminado:
`segundos_hasta_apertura()`, `esperar_en_tramos()`,
`TRAMO_ESPERA_LARGA_SEGUNDOS`.

**Cadencia doble e independiente (añadido sept. 2026, petición del
usuario: "¿el bot cripto puede correr cada minuto?")**: igual que en
`bot_completo.py`/IBKR, cripto y acciones ya no comparten el mismo ciclo
ni el mismo `sleep` — cada grupo tiene su propio intervalo y se ejecuta
solo cuando le toca a él, sin frenar ni acelerar al otro:
- `CRYPTO_INTERVALO_SEGUNDOS = 60` — cripto se revisa cada minuto
  (`revisar_ventas_cripto()`, `revisar_compras_cripto()`).
- `INTERVALO_SEGUNDOS = 130` — acciones siguen a su ritmo de siempre
  (`revisar_ventas()`, `revisar_compras()`, que además se autolimitan por
  horario de mercado).

`main()` guarda `proxima_revision_cripto`/`proxima_revision_acciones`
(marcas de tiempo con `time.monotonic()`) y en cada vuelta del bucle
ejecuta cada grupo solo si ya le toca; al final espera únicamente hasta la
MÁS PRÓXIMA de las dos revisiones (`min(...)`), nunca más de lo necesario.
Cada bloque tiene su propio `try/except` para que un fallo en cripto no
pare las acciones ni viceversa, y el latido se refresca tras ejecutar
cualquiera de los dos grupos (no solo al final del bucle) para que el
vigilante de congelación no confunda una espera corta y legítima con un
cuelgue real.

### Separación de `revisar_ventas()` (acciones) y cripto

`revisar_ventas()` (acciones) ahora filtra explícitamente las posiciones
de cripto (`if not es_cripto(p.symbol)`) para no aplicarles por error la
lógica de acciones (comisión cero, umbral 0.5%, horario, forzado de venta
antes del cierre...) — cripto tiene su propia función dedicada,
`revisar_ventas_cripto()`, con su propia comisión, umbral y sin ninguna
lógica de horario ni de venta forzada (no tiene sentido "forzar venta
antes del cierre" en un mercado que nunca cierra).

## Límite agregado de posiciones y caja disponible (sept. 2026, petición del usuario)

Además del límite del 15% por posición y del 20% agregado de cripto, tanto
`revisar_compras()` (acciones) como `revisar_compras_cripto()` comprueban
dos cosas más antes de intentar comprar:

- **`MAX_POSICIONES_ABIERTAS = 12`** (solo acciones — cripto no lo
  necesita, su universo son 6 monedas como mucho y ya tiene su propio
  límite agregado del 20%): no se abre un ticker **nuevo** si ya hay 12
  tickers distintos con posición abierta a la vez. Promediar una posición
  ya existente no cuenta para este límite.
- **Efectivo disponible real** (`obtener_efectivo_disponible_usd()`, lee
  `cuenta.cash` de Alpaca): no se compra si el importe de la operación
  supera el efectivo realmente disponible, en ambas funciones (acciones y
  cripto). Antes no había ningún control de caja — el bot podía intentar
  comprar contra fondos que ya estaban comprometidos en otras posiciones.

Ambos se reservan de forma OPTIMISTA en el momento de decidir la compra
(no al confirmarse como `filled`), para que dos señales del mismo ciclo
no se salten el límite entre ellas — igual que ya hacía
`revisar_compras_cripto()` con `exposicion_cripto_actual_usd`. Es
deliberadamente conservador: si una orden acaba rechazada, se pierde
margen para el resto del ciclo, pero nunca se compra de más. Si `cash` no
se puede leer (fallo de red), se omite solo esa comprobación concreta sin
bloquear el resto del ciclo.

## Bug corregido: "1 de 7 en contra" compraba contra la tendencia larga (sept. 2026)

`decidir_senal()` (idéntica en concepto a `analizar_activo()` de `bot_completo.py`, ver ahí
la explicación completa): antes, si la única temporalidad en contra era la diaria o semanal,
se compraba igual — se trataba exactamente igual que "1 minuto en contra". Peor aún, el
atajo de "4 cortas alcistas" ignoraba por completo día/semana, así que este bug pasaba
desapercibido en el caso más habitual (4 cortas alineadas, que es cuando el atajo se
adelantaba a la regla de "1 de 7" y esta última nunca llegaba a aplicarse de verdad). Ahora:
- La excepción de "1 de 7 en contra" solo da `COMPRA` si la temporalidad discordante es
  CORTA (1min-1h); si es larga (día/semana), da `BLOQUEADO_TF_LARGA`.
- El atajo de 4 cortas alcistas TAMBIÉN exige que ninguna larga esté en contra.

## BUG CRÍTICO corregido: el trailing stop se olvidaba en cada reinicio (sept. 2026)

Caso real reportado por el usuario: la cartera `[REAL]` mostraba BCH/USD con +3.45% neto y
LINK/USD con +6.99% neto en una foto, y horas después +1.05% y +5.32% respectivamente en la
siguiente — un retroceso muy superior a los 0.3 puntos de `TRAILING_STOP_VENTA_PCT` — sin que
el bot hubiera vendido nada, ni total ni parcialmente.

Causa: `_maximo_beneficio_neto_por_posicion` y `_scale_out_realizado` (ver "Reglas de venta"
más arriba) vivían solo en memoria (`{}` y `set()` a nivel de módulo, sin persistencia). Entre
las dos fotos de cartera hubo varios reinicios de `bot_alpaca.py` (despliegues de esta misma
sesión de trabajo, vía `systemctl restart bot-alpaca`), y cada reinicio ponía ese diccionario
a `{}` de nuevo. El bot "olvidaba" que ya había visto un máximo de +3.45%/+6.99% en esas
posiciones, y volvía a empezar a trackear el máximo desde el valor vigente en ese momento —
así que el retroceso real (medido desde el máximo histórico ya olvidado) nunca llegó a
compararse contra el umbral de 0.3 puntos.

Arreglo: se persiste este estado a disco (`estado_venta_alpaca.json`, fuera del repositorio
vía `.gitignore` — es estado de ejecución, no código) mediante dos funciones nuevas:
- `cargar_estado_venta()`: se llama una única vez, al arrancar, dentro de `main()` (justo
  después de `actualizar_latido()`), y repuebla `_maximo_beneficio_neto_por_posicion`/
  `_scale_out_realizado` desde el JSON si existe (si no existe o está corrupto, sigue con los
  diccionarios vacíos igual que antes, sin lanzar excepción).
- `_guardar_estado_venta()`: se llama tras cada actualización real de estos dos estructuras,
  dentro de `decidir_accion_venta()` (en sus 3 posibles desenlaces) y de
  `cerrar_seguimiento_venta()` — así el fichero en disco nunca queda desfasado respecto a lo
  que el bot tiene en memoria.

Con esto, un reinicio del bot (despliegue, caída, reconexión) ya NO borra el progreso del
trailing stop de las posiciones abiertas.

**Importante — no es retroactivo**: este arreglo no recupera el máximo de +3.45%/+6.99% que
ya se perdió para las posiciones BCH/LINK que estaban abiertas antes de desplegarlo. En el
primer reinicio tras el despliegue, el trailing de esas posiciones concretas volverá a
empezar a trackear desde el beneficio vigente en ese momento (no desde el pico histórico ya
olvidado) — el beneficio se protege hacia adelante, pero el pico ya perdido no se reconstruye.
De ahí en adelante (mientras el bot no se reinicie, o tras el próximo reinicio con el arreglo
ya desplegado) el máximo sí sobrevive a cualquier reinicio posterior.

## Pendiente / próximos pasos

- Probar A FONDO en modo paper antes de pasar a real (en curso).
- **Ya en marcha (agosto 2026)**: el bot corre 24/7 en un servidor AWS
  (EC2, capa gratuita) en vez de en el PC de Windows, con `systemd` para
  arranque/reinicio automático, y control + avisos desde Telegram (ver
  sección dedicada más arriba) — cumple el objetivo original de "gestionar
  todo desde el móvil, sin gastos iniciales".
