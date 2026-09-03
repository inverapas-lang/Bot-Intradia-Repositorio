# Notas del proyecto — Bot Intradía IBKR

> Este documento existe para que cualquiera (tú, o una conversación nueva conmigo) pueda
> retomar el proyecto rápidamente sin depender del historial de chat. Si vuelves después de
> mucho tiempo, empieza por aquí. El código y los commits son la fuente de verdad; esto es
> el resumen legible.

**Repo:** `inverapas-lang/Bot-Intradia-Repositorio`
**Rama de trabajo:** `claude/bot-development-steps-tr4a6x`
**PR:** #1 (abierto, sin fusionar a `main` a fecha de este documento)
**Archivo principal:** `bot_completo.py`
**Tests:** `tests/test_bot_completo.py` (sin pytest, se ejecuta con `python3 tests/test_bot_completo.py`; no requiere IBKR, usa dobles de prueba)

## Qué es esto

Bot de trading intradía sobre Interactive Brokers (vía `ib_async`), con señales basadas en
MACD multi-temporalidad. Corre en bucle continuo, revisando ventas y compras cada ciclo.
**Envía órdenes reales** a IBKR (aunque sea sobre cuenta paper) — no es una simulación.

**Desde agosto 2026 hay un bot hermano, `bot_alpaca.py`** (sobre Alpaca, solo mercado US,
comisión 0€, pensado para correr EN PARALELO a este). Ver `ALPACA_NOTES.md` para su estado —
estas notas (`NOTES.md`) siguen siendo solo sobre el bot de IBKR.

Se ejecuta localmente en el ordenador del usuario (Windows, `C:\Users\javie\Broker`), con
IB Gateway/TWS corriendo en la misma máquina. Yo (Claude) no tengo acceso directo a esa
ejecución; el usuario pega los logs del CMD y yo los reviso y edito el código en este repo.

## Capital real objetivo

**~300€**, aunque el bot se ha estado probando contra una cuenta **paper de IBKR con saldo
por defecto (~$1M)**. Esto es importante: con 300€ reales, el 15% de exposición máxima por
valor son solo ~45€, lo que probablemente deja **HK y KR casi inactivos** (los lotes mínimos
de muchas acciones de esos mercados cuestan más que eso). Quedó como pregunta abierta si
desactivar HK/KR para la cuenta real — **no decidido todavía**.

## Mercados y horarios

- **US** (NYSE/Nasdaq, SMART, USD): premercado 4:00–9:30 ET + regular 9:30–16:00 ET.
- **HK** (Hong Kong, SEHK, HKD): 9:30–16:00 hora de Hong Kong (simplificado, ignora la pausa
  de mediodía real).
- **KR** (Corea/KRX, KRW): 9:00–15:30 hora de Corea.
- **EU** (Euronext/Borsa Italiana, EUR): definido en el código (`ACTIVOS_EU`, 20 valores)
  pero **desactivado** — pendiente de contratar la suscripción de datos de mercado de Europa
  en IBKR.
- **CRYPTO** (via Paxos, USD): añadido en septiembre 2026 a petición del usuario (ya tenía el
  permiso de cripto activado en IBKR y había hecho una operación a mano). Horario controlado
  por `CRYPTO_24_7` (actualmente `True` — el usuario confirmó que su cuenta opera 24/7, nivel
  "Crypto Plus"; si en algún momento se confirma que en realidad es "Crypto Basic", poner esto
  en `False` para usar el horario domingo 3:00 AM ET a viernes 4:00 PM ET, ya implementado en
  `es_horario_operativo_cripto()`). Ver sección dedicada más abajo para el detalle completo.

## Listas de valores actuales

- `ACTIVOS_US`: 30 tickers, lista curada a petición del usuario (ago. 2026), por sector:
  - Tecnología (Nasdaq): AAPL, MSFT, NVDA, AMZN, GOOGL, META, TSLA, AVGO, AMD, NFLX, INTC,
    QCOM, CSCO, SMCI.
  - Financiero (NYSE): JPM, BAC, WFC, C, V, MA.
  - Energía (NYSE): XOM, CVX.
  - Consumo/Salud/Industrial (NYSE): WMT, DIS, KO, JNJ, PFE, F, T, GE.
- `ACTIVOS_HK`: 28 tickers (blue chips de Hong Kong). **Excluido de `ACTIVOS`** desde agosto 2026
  (ver sección "Comisiones estimadas" — con el capital actual, ni los lotes fijos de HK ni la
  comisión mínima por orden son viables). La lista sigue definida en el código por si se
  reactiva más adelante con más capital; solo hay que añadir `+ ACTIVOS_HK` de nuevo a `ACTIVOS`.
  Las posiciones de HK que ya se tuvieran abiertas se siguen vendiendo con normalidad
  (`revisar_ventas` no depende de esta lista, solo de las posiciones reales en IBKR).
- `ACTIVOS_KR`: 10 tickers (blue chips de Corea).
- Total activo en `ACTIVOS` (US+KR): **40 valores**. EU y HK excluidos.

## Configuración clave actual

| Parámetro | Valor | Qué hace |
|---|---|---|
| `INTERVALO_SEGUNDOS` | 240 (4 min) | Objetivo de tiempo entre ciclos. Con la espera adaptativa, si el ciclo tarda más, no se espera nada extra. |
| `IMPORTE_EUROS` | 1000 | Presupuesto tope por operación (US/EU/KR), en la práctica casi nunca es el límite real (lo es el 15%). |
| `IMPORTE_EUROS_HK` | 3500 | Igual pero para HK (lotes fijos más grandes). |
| `LIMITE_EXPOSICION_PCT` | 15% | Máximo del valor total de cartera por posición. |
| `UMBRAL_BENEFICIO_PCT` | 0.5% | Beneficio neto mínimo para empezar a vigilar la venta. |
| `MINUTOS_SIN_COMPRAR_ANTES_CIERRE` | 90 | Ventana antes del cierre en la que no se compra (salvo promediar a la baja). |
| `MINUTOS_VENTA_FORZADA_ANTES_CIERRE` | 15 | Ventana antes del cierre en la que se vende si el beneficio está entre 0.5% y `BENEFICIO_MAX_VENTA_FORZADA_PCT`. |
| `BENEFICIO_MAX_VENTA_FORZADA_PCT` | 2.0% | Techo de la venta forzada. |
| `UMBRAL_FALLOS_SEGUIDOS_DATOS` | 8 | Cortacircuitos: tras 8 valores seguidos sin datos, deja de reintentar 3× y avisa una vez. |

**Pendiente de decidir:** subir `INTERVALO_SEGUNDOS` — con datos reales, un ciclo con solo
HK+KR (US cerrado) tarda ~135–165s; con HK ampliado a 28 valores y US abierto (40 más),
es probable que supere los 240s de forma habitual. El aviso automático en el log
(`"AVISO: el ciclo ha tardado Xs..."`) ya avisa cuando pasa, pero no se ha subido el número
todavía a la espera de ver un ciclo real con los tres mercados activos.

## Reglas de compra (`analizar_activo` + `revisar_compras`)

1. **Señal de compra**: MACD en 7 temporalidades (1min, 5min, 15min, 30min, 1h, 1día, 1semana).
   - Cortas (hasta 1h): a favor si MACD > línea de señal.
   - Largas (día, semana): a favor si el histograma de las últimas 3 velas está **creciendo**
     (momentum acelerando — importante: una tendencia de pendiente *constante* no basta,
     matemáticamente el histograma decrece aunque el precio siga subiendo; solo una
     aceleración real lo activa).
   - `COMPRA` si las 7 están a favor, o como mucho 1 de 7 en contra.
   - **Atajo añadido**: si las 4 más cortas (1min/5min/15min/30min) están todas a favor →
     `COMPRA` directa, sin mirar el resto. Efecto colateral: el viejo resultado `BLOQUEADO`
     (cortas ok, largas no) ya no puede darse, porque el atajo dispara antes.
   - Si faltan datos en alguna temporalidad → `SIN_DATOS`, no opera.
2. Solo se analiza si el mercado de ese valor está en horario operativo.
3. **Ventana de no-compra** (últimos 90 min antes del cierre): no compra nada salvo que ya
   tengas el valor y el precio actual sea menor que tu precio medio (promediar a la baja).
4. **Límite de exposición**: no compra si esa posición ya es ≥15% de la cartera total (USD).
5. **Importe**: `min(presupuesto fijo, margen restante hasta el 15%)`.
   - **US**: se permite comprar **fracción de acción** (hasta 4 decimales). La orden se manda
     por **importe en efectivo** (`cashQty`), no por número de acciones — ver bug crítico #9.
   - **HK/KR**: acciones/lotes enteros, redondeo hacia abajo al lote mínimo de IBKR.
6. Órdenes **a mercado** en sesión regular. En **pre/postmercado de US** (ver sección
   dedicada más abajo), órdenes **limitadas al precio exacto** en vez de a mercado.
7. Si el estado de la orden no confirma `Filled`, se vuelve a consultar la posición real en
   IBKR unos segundos después (`verificar_posicion_tras_orden_no_confirmada`) para dar un
   veredicto fiable en el log en vez de fiarse solo del objeto `Trade` (ver bug #10).
8. Si se confirma que la compra abrió una posición **nueva desde cero** (no una ampliación),
   se registra la fecha en `historial_compras.json` (ver sección de historial más abajo).

## Reglas de venta (`revisar_ventas`)

- **Nunca hay stop-loss** de pérdidas — el bot no vende nunca solo porque esté en negativo.
- Solo actúa si el mercado de esa posición está en horario operativo (bug corregido, ver abajo).
- Si beneficio neto < 0.5% → se mantiene.
- Si estás en los últimos 15 min antes del cierre y el beneficio está entre 0.5% y 2% →
  **venta forzada** con orden límite (0.2% por debajo del precio actual).
- Si no, y el MACD de 5 min está bajista → vende **a mercado**.
- Si MACD de 5 min alcista → se deja correr, aunque tenga beneficio.
- Si el estado de la orden no confirma `Filled`, igual que en compras: se reconsulta la
  posición real para dar un veredicto fiable en el log.
- En **postmercado de US** SÍ se vende con normalidad (con orden límite, ver sección dedicada
  más abajo); lo que no se hace en ese tramo es comprar.

## Pre/postmercado de US (agosto 2026)

Antes, el bot "miraba" precios en premercado (`HORA_INICIO_US = 4:00 ET`) pero en realidad
**no ejecutaba nada de verdad** ahí: las órdenes no llevaban el flag `outsideRth`, obligatorio
en IBKR para operar fuera de la sesión regular (9:30-16:00 ET). Sin él, una orden a esas horas
se queda pendiente sin ejecutar hasta la apertura oficial. Y el bot tampoco tenía ningún tramo
de postmercado (16:00-20:00 ET) — se consideraba cerrado justo al cierre regular.

**Decisión del usuario**: activar tanto pre como postmercado, pero con dos matices:
1. En **ambos** tramos, las órdenes deben ser **limitadas al precio exacto** (con
   `outsideRth=True`), nunca a mercado — la liquidez es mucho menor y una orden a mercado
   podría ejecutarse a un precio muy distinto del analizado.
2. En **postmercado, solo se vende, nunca se compra** (decisión explícita, y al revés de lo
   que se implementó en un primer momento: no abrir posiciones nuevas con la liquidez tan baja
   de esa franja, pero sin bloquear la salida de una posición que ya toque cerrar). En
   premercado sí se compra y se vende con normalidad (ambas con orden límite).

**Importante — las ventanas de seguridad NO se movieron**: `en_ventana_sin_compra` (últimos
90 min antes del cierre) y `en_ventana_venta_forzada` (últimos 15 min antes del cierre) siguen
ancladas al **cierre regular** (16:00 ET), sin cambios — decisión explícita del usuario para no
alterar la gestión de riesgo ya probada. El postmercado (16:00-20:00 ET) es una franja
*adicional* donde se puede vender, sin las protecciones pensadas para el final de la sesión
regular.

Piezas clave:
- `HORA_CIERRE_EXTENDIDO_US = 20:00 ET`: nuevo límite de `es_horario_operativo("US")`
  (antes era `HORA_CIERRE_US = 16:00 ET`, que ahora solo se usa para las ventanas de
  seguridad, no para saber si el mercado está "abierto").
- `en_postmercado_us()`: True entre 16:00-20:00 ET. Usado en `revisar_compras` para saltarse
  por completo cualquier intento de compra en US en ese tramo (se sigue analizando/vendiendo
  con normalidad).
- `fuera_de_sesion_regular_us()`: True en pre **o** postmercado (falso en sesión regular).
  Usado tanto en `revisar_compras` como en `revisar_ventas` para decidir orden límite vs
  mercado (en ventas puede darse en cualquiera de los dos tramos, ya no solo en premercado).

**Hallazgo adicional (visto en producción, agosto 2026)**: las fracciones de acción NO
funcionan vía API fuera de la sesión regular, **ni por `cashQty` ni por cantidad directa**
(error 10243 "Please use desktop version to place this order" con los dos métodos, para
varios valores distintos en pre/postmercado). Por eso, en `revisar_compras`, cuando
`fraccionable and fuera_de_sesion_regular_us()` (`fracciones_no_disponibles`), se salta
directamente el Plan A (`cashQty`) y el Plan B (cantidad fraccionaria directa) y se va
derecho al Plan C (acciones enteras) — evita 2 órdenes rechazadas por señal, y con
presupuestos pequeños puede significar que la señal se omita si no llega ni para 1 acción
entera (no hay nada que hacer ahí, es una limitación real de la plataforma fuera de horario
regular).
- `crear_orden_limitada`/`crear_orden_limitada_cash` aceptan ahora `fuera_horario_regular=True`
  para activar `outsideRth` en la orden.

## Historial de fecha de apertura (`historial_compras.json`)

Archivo JSON local (fuera de git, en `.gitignore`), en la misma carpeta que `bot_completo.py`.
Guarda, por cada valor, la fecha en la que se abrió la posición **desde cero**. Existe porque
`reqExecutions()` de IBKR solo devuelve las ejecuciones del **día actual** — sin este archivo,
el resumen de cierre mostraba "?" como fecha de apertura para cualquier posición comprada en
días anteriores.

- Se registra/actualiza solo cuando se confirma una compra que abre una posición nueva (no
  se tenía nada de ese valor antes).
- **No se toca** en compras adicionales sobre una posición ya abierta (promediar a la baja):
  la fecha sigue siendo la de la apertura original.
- **No se borra explícitamente al vender** — simplemente queda ahí como fecha histórica hasta
  que se vuelve a comprar ese valor desde cero (momento en el que se sobreescribe). Esto es
  intencional: así el resumen de cierre de mercado, generado varias horas después del cierre,
  todavía puede mostrar la fecha de apertura correcta de una posición que se cerró ese mismo día.
- Funciones clave: `cargar_historial_compras`, `guardar_historial_compras`,
  `registrar_apertura_de_posicion`, `obtener_apertura_registrada`.
- Si el archivo se borra o se mueve a otro ordenador, simplemente se pierde el historial
  acumulado (vuelve a mostrar "?" hasta la siguiente compra de cada valor) — no rompe nada.
- En la tabla de "Operaciones cerradas hoy" del resumen, este historial es solo el **fallback**:
  se prioriza la fecha calculada de `reqExecutions()` (ver bug #13 en la lista de abajo), para
  no mostrar la apertura de una ronda de compra posterior a la venta que se está resumiendo.

## Historial de operaciones ejecutadas (`historial_operaciones_ibkr.json`) y `cartera_ibkr.py`

Archivo JSON local nuevo (agosto 2026, fuera de git), distinto del anterior (ese solo guarda la
fecha de apertura; este guarda **cada compra y venta ejecutada con éxito**, con cantidad, precio,
comisión estimada, coste medio y beneficio % en el caso de las ventas). Existe porque
`reqExecutions()` de IBKR solo devuelve el día actual, así que no sirve para consultar el
historial de operaciones cerradas de días anteriores — este archivo sí acumula indefinidamente.

- Se rellena solo (`registrar_operacion_historial()`) justo después de confirmar que una orden
  quedó `Filled`, en los 3 puntos de ejecución de `revisar_ventas`/`revisar_compras` (venta
  forzada, venta por MACD bajista, compra). Usa la cantidad/precio REALES de la orden
  (`trade.orderStatus.filled`/`avgFillPrice`) cuando están disponibles, con la cantidad/precio
  previstos como fallback.
- Solo registro: no participa en ninguna decisión de trading, así que un fallo al escribir el
  archivo (p. ej. disco lleno) no aborta el ciclo, solo se registra en el log.
- Solo empieza a acumular desde que se desplegó esta función — no hay datos retroactivos de
  operaciones anteriores a agosto 2026.

**`cartera_ibkr.py`** es un script nuevo, aparte del bot, que consulta el estado de cartera A
DEMANDA sin tocar el bot en marcha (se conecta a IB Gateway con `clientId=9`, distinto del `1`
que usa `bot_completo.py`, para poder correr a la vez). Muestra las posiciones abiertas de todos
los mercados activos (con beneficio/pérdida no realizado en moneda local y en EUR) y las
operaciones cerradas en un rango de fechas configurable (hoy por defecto; `--ayer`, `--semana`,
o `--desde`/`--hasta`), leyendo este historial. Es de solo lectura.

## Totales en el resumen de cierre de mercado

Además de las tablas de detalle por símbolo (que se mantienen igual), el resumen ahora muestra:
- **Posiciones abiertas**: fila `TOTAL` con el invertido y beneficio/pérdida no realizado ya
  existían; se añadió el total invertido también en **USD** (antes solo en EUR).
- **Operaciones cerradas hoy**: fila nueva `TOTAL ganancia hoy (<mercado>)` con la suma de la
  ganancia neta del día en **USD y EUR** (no incluye filas `N/D` sin datos suficientes, como VZ).

## Aviso de modo de cuenta (DEMO vs REAL)

`obtener_modo_cuenta` / `avisar_modo_cuenta`: al conectar (y tras cada reconexión), el bot
consulta `ib.managedAccounts()` y detecta si la cuenta es demo o real por el ID (convención
de IBKR: las cuentas de paper trading siempre empiezan por `DU`). Se imprime un aviso bien
visible con `#` de por medio, y además una marca corta `[DEMO/PAPER TRADING (cuenta ...)]` o
`[REAL - DINERO REAL (cuenta ...)]` en la línea de "Iniciando nuevo ciclo" de cada ciclo, para
que sea imposible perder de vista el modo con solo mirar el log reciente.

## Vigilante de congelación del proceso — IMPORTANTE: requiere supervisor externo

Se vio en producción un episodio real de **~10,7 horas colgado sin ningún log**, tras un
`WinError 10054` (corte de red). El hilo principal se quedó bloqueado dentro de una llamada
bloqueante a IBKR (probablemente `reqContractDetails`) que nunca devolvió ni lanzó excepción
— ningún `try/except` de nuestro código puede detectar eso, porque el hilo está literalmente
parado, no lanzando errores.

Se añadió un **hilo vigilante en segundo plano** (`vigilante_congelacion`, arrancado al inicio
de `main()`) que comprueba cada minuto cuánto tiempo ha pasado desde la última "señal de vida"
(`_ultimo_latido`, que se refresca en cada `log()` y en cada tramo de `esperar_pumpeando`). Si
pasan más de `UMBRAL_CONGELACION_SEGUNDOS` (20 min) en silencio, fuerza el cierre del proceso
con `os._exit(1)`.

**Esto NO reinicia el bot por sí solo** — el bucle de reintento que ya tiene el script
(`if __name__ == "__main__": while True: ...`) vive en el **mismo proceso** que se acaba de
matar, así que no sirve de nada aquí. Para que el bot se recupere solo tras esto, **hace falta
un supervisor externo**. Se añadió `run.bot.bat` (relanza `python bot_completo.py`
automáticamente si el proceso termina por cualquier motivo) — el usuario debería lanzar el bot
con este `.bat` en vez de `python bot_completo.py` directamente, para que el vigilante sea
realmente útil.

### El hilo interno NO es del todo fiable — caso real en producción

Se vio en producción que el hilo `vigilante_congelacion` **no saltó solo**: el bot se congeló
a las 20:01, y el aviso `[VIGILANTE]` no apareció hasta que el usuario pulsó **Ctrl+C** a mano
mucho después. Motivo: si el hilo principal se queda atascado dentro de una llamada de bajo
nivel que nunca cede el turno (el **GIL** de Python — el intérprete solo puede ejecutar un hilo
Python a la vez), **ningún otro hilo del mismo proceso puede ejecutarse tampoco**, ni siquiera
el propio vigilante. Un hilo dentro del mismo `python.exe` no es inmune a que ese mismo
`python.exe` esté paralizado.

### La solución real: vigilante EXTERNO (`vigilante_externo.ps1`)

Para tener un vigilante de verdad inmune a esto, tiene que ser un **proceso de Windows aparte**,
no un hilo dentro de Python. Se añadió:

- `bot_completo.py` ahora escribe, además de refrescar `_ultimo_latido` en memoria:
  - **`latido_bot.txt`**: la hora actual, cada vez que hay actividad (máximo cada 10s, para no
    escribir en disco en cada log).
  - **`bot.pid`**: el PID del proceso, escrito una vez al arrancar (`escribir_pid()` en `main()`).
- **`vigilante_externo.ps1`**: script de PowerShell independiente que corre cada minuto, revisa
  cuándo se modificó `latido_bot.txt` por última vez y, si pasan más de 25 minutos (deliberadamente
  algo más que el umbral del vigilante interno, 20 min, para darle a este la primera oportunidad),
  mata el proceso por su PID (`Stop-Process -Id <pid> -Force`). Al morir el proceso, `run.bot.bat`
  lo relanza solo.

**Cómo usarlo**: dejar corriendo `run.bot.bat` en una ventana y, en otra ventana aparte,
`powershell -ExecutionPolicy Bypass -File vigilante_externo.ps1`. Ambos deben estar corriendo a
la vez para tener protección completa. Ideal: configurar `vigilante_externo.ps1` como Tarea
Programada de Windows que arranque solo al iniciar sesión, para no depender de acordarse de
abrirlo a mano cada vez.

`latido_bot.txt` y `bot.pid` están en `.gitignore` (son estado de ejecución, no código).

**Sin `run.bot.bat` + `vigilante_externo.ps1` corriendo los dos a la vez, sigue existiendo el
riesgo de que una congelación por GIL se quede sin detectar** — el hilo interno solo cubre
congelaciones "normales" (bucles que sí ceden el turno de vez en cuando); el externo cubre el
resto.

## Plan A/B/C ante rechazo de fracciones en la API (error 10244 / 10243)

No todos los valores/cuentas admiten el mecanismo `cashQty` (importe en efectivo) que usa el
bot para fracciones vía API — a veces por instrumento (visto en demo con `ORCL`, `PLTR`, `CAT`,
`NFLX`, `INTC`), a veces por CUENTA COMPLETA (visto en la cuenta REAL: rechazado en TODOS los
valores probados, `GS` y `PFE`). El rechazo es `Error 10244: La cantidad de efectivo no puede
utilizarse en esta orden`.

**Hallazgo importante (comprobado a mano en cuenta real, agosto 2026)**: cuando `cashQty` falla,
NO significa que la cuenta no admita fracciones de verdad. Se comprobó comprando manualmente
0.5 acciones de PFE desde la app de IBKR, tanto con orden límite como a **mercado**, indicando
la cantidad fraccionaria **directamente** (no como importe en efectivo) — y funcionó sin
problema. El fallo es específico del mecanismo `cashQty`, no de las fracciones en sí.

Por eso ahora hay una cadena de reintentos de 3 pasos, tanto en compras como en ventas:

- **Plan A** — `cashQty` (importe en efectivo): el método preferido, funciona en la mayoría de
  casos (p.ej. toda la cuenta demo salvo excepciones puntuales).
- **Plan B** — si Plan A falla con `Error 10244`: se reintenta con la **cantidad fraccionaria
  original puesta directamente** como `totalQuantity` (p.ej. `3.544` acciones), sin redondear.
  Esto es lo que ahora funciona en la cuenta real.
- **Plan C** — si Plan B también falla (`Error 10243`, "no se puede introducir la orden de
  tamaño fraccionario a través de la API"): último recurso, se reintenta con **acciones
  ENTERAS** (redondeando hacia abajo el presupuesto disponible). Si el presupuesto no llega ni
  para 1 acción entera, se omite con un aviso claro en el log.

`orden_rechazada_por_codigo(trade, {codigos...})` detecta estos códigos en el registro de la
orden (`trade.log`) para decidir si pasar al siguiente plan. En ventas solo se implementaron
los Planes A y B (no hay Plan C de "vender solo la parte entera y dejar un resto fraccionario
sin vender" — si algún día hace falta, avisar).

## Comisiones estimadas (tarifas reales de IBKR, plan "tiered", Nivel I)

Ajustado en agosto 2026 con las tarifas reales consultadas en interactivebrokers.ie (el
usuario confirmó que su cuenta usa el plan **"tiered" (por niveles)**, no "fixed"). Antes se
usaba una estimación genérica de 0.07% + mínimo 1€ que **infravaloraba mucho** el coste real,
sobre todo en fracciones de US y en HK/KR.

| Mercado | Tarifa | Mínimo por orden |
|---|---|---|
| US, acciones **enteras** | 0.0035 USD/acción | 0.35 USD (tope máx.: 1% del valor negociado) |
| US, **fraccionarias** | 1% del valor negociado | 0.01 USD |
| HK | 0.05% del valor negociado | ~2.25 USD equivalente |
| KR | 0.06% del valor negociado | 4000 KRW (KR no tiene plan "fixed", solo tiered) |

`estimar_comision(valor_operacion, currency, cantidad=None)` calcula la comisión de **una
sola** operación (compra O venta) — para el coste de ida y vuelta hay que sumar dos llamadas,
una por cada lado (ya no se usa un único mínimo compartido para toda la operación, como se
hacía antes: cada orden real paga su propio mínimo en IBKR). El parámetro `cantidad` es
necesario en US para distinguir acciones enteras de fraccionarias vía
`es_cantidad_fraccionaria()`; si no se indica, se asume fraccionaria (el caso más habitual del
bot en US).

**Importante — sigue siendo una aproximación, no la cifra exacta al céntimo**: estas tarifas
son solo la comisión propia de IBKR. No incluyen las "comisiones de terceros" que IBKR
repercute aparte (tasas de bolsa, de compensación, cargos normativos — p.ej. el impuesto de
timbre de Hong Kong), que no están cuantificadas aquí. El beneficio neto que calcula el bot es
algo optimista frente al real, especialmente en HK y KR.

**Hallazgo importante sobre el capital pequeño (~300€, límite de exposición 15% ≈ 45 $ por
posición)**: con los mínimos por orden de HK (~2.25 $) y KR (~2.85 $, 4000 KRW), una sola
operación de ida y vuelta se puede comer **~10-13% del valor de la posición solo en
comisiones mínimas**, antes de contar ganancias/pérdidas de mercado — operar en HK/KR es
poco viable con un capital tan pequeño, más allá del problema de los lotes fijos de HK ya
documentado. Ver conversación de agosto 2026 para el detalle completo del cálculo.

## Criptomonedas (PAXOS/ZEROHASH, vía IBKR) — añadido septiembre 2026

Petición explícita del usuario: ya tenía el permiso de cripto activado en IBKR y había hecho
una operación a mano antes de pedir que el bot lo soportara.

**IMPORTANTE — dos proveedores distintos con conId diferente para la misma moneda**: IBKR
ofrece cripto a través de dos exchanges/proveedores distintos: `"PAXOS"` (el original, 4
monedas: BTC/ETH/LTC/BCH) y `"ZEROHASH"` (más reciente, más monedas: BTC, ETH, LTC, BCH,
LINK, MATIC, SOL...). Cuál usa una cuenta concreta depende de sus suscripciones de datos de
mercado (Client Portal → Configuración de cuenta → Suscripciones de datos de mercado).

**Historia real de la depuración de este bug en producción (sept. 2026, cuenta U25302975)** —
se deja documentada entera porque cada paso descarto una hipotesis con evidencia real y la
secuencia importa para no repetir los mismos pasos en falso si vuelve a pasar:
1. Con `exchange="PAXOS"` (valor original): `reqHistoricalData` se quedaba colgado con
   `TimeoutError` en los 3 intentos para BTC, sin ningún error de permisos, mientras que
   las acciones US funcionaban con normalidad en el mismo ciclo.
2. Una captura de pantalla de la página de suscripciones de datos de la usuaria mostraba
   "ZEROHASHE Cryptocurrency" → se interpretó (incorrectamente, ver paso 4) que la cuenta
   usaba ZEROHASH, y se cambió `EXCHANGE_CRYPTO` a `"ZEROHASH"`.
3. El `TimeoutError` PERSISTIÓ igual tras el cambio de exchange. Se añadió `on_error_ib()`
   (enganchado a `ib.errorEvent`) para dejar de operar a ciegas: sin este logging, cualquier
   error real de la API de IBKR era invisible, solo se veía el timeout genérico. Con el
   logging se descubrió la verdadera causa del timeout original: `reqHistoricalData` exige
   `whatToShow='AGGTRADES'` para contratos `secType='CRYPTO'` (el resto de mercados usa
   `'TRADES'`, que simplemente no funciona para cripto). `pedir_velas()` ahora elige
   `what_to_show` según `contrato.secType`.
4. Con AGGTRADES + exchange="ZEROHASH": la **venta** de la posición real de la usuaria (el
   contrato de `ib.positions()` llega con `exchange=""` y se completa vía
   `ib.qualifyContracts()` a partir de su conId real — ver más abajo) SÍ obtuvo precio
   correctamente. Pero la **compra** (contrato construido a mano en `ACTIVOS_CRYPTO` con
   `exchange="ZEROHASH"`, conId resuelto 541686651) falló con un error explícito y sin
   ambigüedad: `Error 162: No market data permissions for ZEROHASH CRYPTO`. Se interpretó
   (paso en falso, ver paso 5) que la cuenta debía estar en PAXOS.
5. Se cambió `EXCHANGE_CRYPTO` a `"PAXOS"` (conId resuelto 479624278): la **compra** volvió a
   fallar, esta vez con `Error 162: No market data permissions for PAXOS CRYPTO`. Es decir: NI
   "PAXOS" NI "ZEROHASH", tal cual los resuelve IBKR por defecto para BTC/USD sin más contexto,
   tienen datos en esta cuenta — y el conId de esos dos intentos (541686651 y 479624278) es
   DISTINTO del conId real de la posición de la usuaria (confirmado en logs), que es un
   *tercer* contrato cuyo exchange nunca se llegó a ver por texto. No hay forma fiable de
   adivinar ese exchange a mano.
6. **Solución definitiva**: dejar de adivinar el exchange del todo. `EXCHANGE_CRYPTO` se deja
   en `""` (vacío) y `crear_contrato()` ya no lo usa directamente — en su lugar,
   `descubrir_exchange_cripto(ib)` busca entre las posiciones abiertas alguna de CRYPTO ya
   resuelta por IBKR (completando su `exchange` vía `qualifyContracts()` a partir de su conId
   real si hiciera falta, igual que en el paso 4) y devuelve ese exchange REAL, que se cachea
   en `_exchange_cripto_cache` (variable de proceso) y se reutiliza para construir el contrato
   de CUALQUIER cripto de `ACTIVOS_CRYPTO` — incluidas monedas que la cuenta no tiene abiertas
   todavía (ETH/LTC/BCH), asumiendo que el mismo exchange vale para las 4 (razonable: las
   suscripciones de datos de cripto de IBKR son por proveedor, no por moneda suelta). Si la
   cuenta no tiene AÚN ninguna posición de cripto abierta (primer arranque, cero compras
   nunca), la caché se queda a `None` y se usa el `activo["exchange"]` de siempre (vacío) como
   última alternativa — en ese caso concreto, la primera compra de cripto puede fallar hasta
   que exista una posición (manual o del propio bot) que ancle el exchange correcto.

Moraleja para el futuro: si cripto vuelve a dar timeout o error de datos, mirar PRIMERO el
error real en el log (gracias a `on_error_ib` ya no es un `TimeoutError` ciego) — pero NO
asumir que ese error apunta al otro proveedor sin más: como se vio en los pasos 4-5, "sin
datos para ZEROHASH" no implica "datos para PAXOS", puede ser un tercer contrato totalmente
distinto. La única fuente fiable es el conId de una posición real ya abierta.

**Monedas**: `ACTIVOS_CRYPTO` = BTC, ETH, LTC, BCH, SOL, LINK. Se empezó solo con las 4
primeras por prudencia (son las que soporta el proveedor original, Paxos, y las más probadas
en la API de IBKR); SOL y LINK se añadieron después a petición explícita del usuario (sept.
2026), ya con el mecanismo de descubrimiento de exchange en marcha (ver arriba) — no hizo
falta ningún cambio adicional, solo ampliar la lista de tickers (mismo
`exchange=EXCHANGE_CRYPTO`, `currency="USD"`).

**Contrato de una posición CRYPTO sin `exchange` (error 321/200)**: a diferencia de las
acciones, el contrato que devuelve `ib.positions()` para una posición CRYPTO llega con el
campo `exchange` vacío. Rellenarlo a mano con un exchange adivinado (`EXCHANGE_CRYPTO`) causó
un error DISTINTO (`Error 200: No security definition has been found`, porque el conId real de
la posición no encajaba con el exchange forzado). La solución correcta, aplicada en
`revisar_ventas` y reutilizada por `descubrir_exchange_cripto()`, es dejar que IBKR complete el
contrato él mismo a partir de su conId (`ib.qualifyContracts(contrato)`) cuando `exchange`
viene vacío — así no importa qué proveedor use la cuenta, el conId ya lo identifica sin
ambigüedad.

**Horario**: IBKR tiene dos niveles de cuenta con horarios distintos:
- **Crypto Basic** (por defecto en la mayoría de cuentas nuevas): domingo 3:00 AM ET a
  viernes 4:00 PM ET (cerrado la mayor parte del fin de semana).
- **Crypto Plus**: 24/7, fines de semana incluidos.

El usuario no sabía cuál tenía y pidió asumir la que permite 24/7 → `CRYPTO_24_7 = True`.
`es_horario_operativo_cripto()` implementa también el horario "Basic" completo (funciones
`proxima_apertura_cripto()`, chequeo viernes/sábado/domingo) por si algún día se confirma que
la cuenta es en realidad "Basic" — solo hay que poner `CRYPTO_24_7 = False`.

**Por qué no se pudo tratar como "un mercado más" sin más cambios**: la cripto vía Paxos
cotiza en **USD**, la misma divisa que las acciones de US — pero todo el código existente
(`CURRENCY_A_MERCADO`, agrupación de posiciones en `revisar_ventas`, filtrado en
`generar_resumen_cierre_mercado`, elección de tarifa en `estimar_comision`) distinguía
mercados **por divisa**. Sin corregir esto, las posiciones de cripto se habrían mezclado con
las de US en todos los sitios que agrupan por divisa. Se resolvió con dos helpers nuevos que
miran primero `contract.secType`:
- `mercado_de_posicion(pos)` — para un objeto `Position` de `ib.positions()`.
- `contrato_pertenece_a_mercado(contrato, mercado)` — para un `Contract` suelto (usado al
  filtrar `ib.reqExecutions()` en el resumen de cierre).

**Construcción de órdenes — mucho más simple que en acciones**: a diferencia de las acciones
(donde una cantidad fraccionaria vía API es rechazada con el error 10243, obligando a todo el
mecanismo de `cashQty` + Plan A/B/C, ver sección dedicada más arriba), en cripto las órdenes
**LMT admiten `totalQuantity` fraccionario de forma nativa y directa** (confirmado en la
documentación oficial de IBKR: LMT usa quantity/totalQuantity, solo las órdenes MKT de cripto
usan cashQty — y aquí no se usan órdenes MKT para cripto, todo es LMT). Por eso
`crear_orden_limitada_cripto()` es una función mínima sin ningún fallback, y `revisar_compras`/
`revisar_ventas` tienen una rama dedicada y aislada para `mercado == "CRYPTO"` (con un
`continue` al final) en vez de intentar encajar la lógica de cripto dentro de la cadena
Plan A/B/C de acciones, que no le aplica en absoluto.

**Comisión**: 0.18% del valor operado (la tarifa más alta del rango 0.12%-0.18% que cita IBKR,
por prudencia), mínimo 1.75 USD, con tope del 1% del valor operado (protege a las operaciones
pequeñas: con el capital actual, ~40-45 $ por operación, el tope del 1% —unos 0.40-0.45 $—
gana casi siempre al mínimo de 1.75 $, así que la comisión real suele ser bastante más baja
que el mínimo citado por IBKR). `estimar_comision_cripto(valor_operacion)` implementa esto por
separado de `estimar_comision()` (que sigue siendo solo para acciones).

**Sin ventana de "venta forzada"**: `en_ventana_venta_forzada("CRYPTO")` siempre da `False`
porque `"CRYPTO"` no está en `CIERRE_POR_MERCADO` (no tiene un único cierre diario del que
calcular minutos-hasta-cierre) — las ventas de cripto van siempre por la lógica normal de MACD
5min, nunca por venta forzada de última hora. Es un comportamiento correcto y esperado, no un
hueco a rellenar: no tiene sentido un concepto de "última hora antes del cierre" en un mercado
continuo.

**Hueco conocido, aceptado por ahora**: no hay un resumen de cierre automático para cripto
(`generar_resumen_cierre_mercado` se sigue llamando solo para US/HK/KR en `main()`, disparado
por `justo_cerro_mercado()`, que requiere una entrada en `CIERRE_POR_MERCADO` que cripto no
tiene por no tener un cierre diario). Para consultar posiciones/operaciones de cripto, usar
`cartera_ibkr.py` (ya corregido para distinguir cripto de US igual que el bot principal).

**Bucle principal (`main()`)**: con `CRYPTO_24_7 = True`, `es_horario_operativo("CRYPTO")`
siempre es `True`, así que `hay_mercado_abierto` en `main()` nunca cae a `False` — el bot deja
de dormir horas fuera del horario de US/HK/KR y pasa a ciclar continuamente
(`INTERVALO_SEGUNDOS` = 4 min) las 24 horas, aunque solo actúe sobre cripto en esos huecos
(las acciones se siguen filtrando por su propio `es_horario_operativo` de siempre). Si algún
día se pone `CRYPTO_24_7 = False`, `segundos_hasta_pre_apertura()` ya tiene en cuenta la
próxima apertura semanal de cripto (`proxima_apertura_cripto()`) para no sobre-dormir el fin
de semana completo cuando cripto reabre el domingo antes que ningún mercado de acciones.

## Control por Telegram del bot de IBKR (`telegram_bot_ibkr.py`) — añadido septiembre 2026

Petición explícita del usuario: quería gestionar `bot_completo.py` (real, IBKR) desde el
móvil, igual que ya podía con Alpaca (`telegram_bot.py`). Se le explicó primero la diferencia
clave frente a Alpaca: **IBKR no es una API en la nube** — hace falta IB Gateway/TWS
ejecutándose en algún ordenador (no hace falta con la ventana abierta en pantalla, pero sí
encendido y con la sesión iniciada) para que el bot pueda conectarse. El usuario eligió
empezar por la opción sencilla: seguir con el PC encendido y añadir Telegram como capa de
control/consulta por encima, en vez de migrar IB Gateway a una VM en la nube (más trabajo,
por la reautenticación/2FA periódica de IBKR). Si en el futuro quiere independizarse del todo
del PC, esa migración a la nube queda pendiente como fase 2.

**Diferencias frente a `telegram_bot.py` (Alpaca)**: aquella versión corre en Linux/AWS con
`systemd` (`systemctl start/stop/is-active`) y lee el log con `journalctl`. Windows no tiene
ninguno de los dos, así que `telegram_bot_ibkr.py` usa mecanismos propios:

- **`/estado`**: no hay `systemctl is-active`. Se lee el PID guardado en `bot.pid` (el mismo
  archivo que ya usaba `vigilante_externo.ps1`) y se comprueba con `tasklist` si ese proceso
  sigue vivo en Windows. Además compara la fecha de modificación de `latido_bot.txt` con
  `UMBRAL_CONGELACION_SEGUNDOS` para distinguir "corriendo bien" (🟢) de "el proceso vive pero
  lleva mucho sin dar señales, probablemente congelado" (🟠) de "no hay ningún proceso con ese
  PID" (🔴).
- **`/arrancar`**: lanza `run.bot.bat` en una ventana de CMD nueva y detached
  (`subprocess.Popen('start "" run.bot.bat', shell=True)`), en vez de arrancar
  `bot_completo.py` directamente — así se conserva TODA la infraestructura de recuperación ya
  existente (el propio bucle de reintento de `run.bot.bat`, el vigilante interno
  `vigilante_congelacion`, y si el usuario lo tiene corriendo, `vigilante_externo.ps1`) en vez
  de reinventar la supervisión de procesos en Python. Antes de lanzar, borra cualquier
  `detener_bot.flag` que pudiera haber quedado de una parada anterior (ver más abajo).
- **`/parar`**: NO mata el proceso a la fuerza (`taskkill`) — eso podría interrumpir una orden
  real a medio colocar. En vez de eso, deja un archivo de señal (`detener_bot.flag`) que:
  1. `bot_completo.py` comprueba en cada vuelta de su bucle principal (como máximo cada ~30s,
     gracias al chequeo añadido dentro de `esperar_pumpeando()`) y, si lo ve, hace un `return`
     limpio de `main()` (con su `try/finally` de siempre, así que sigue desconectando de IB
     Gateway con normalidad).
  2. `run.bot.bat` comprueba el mismo archivo justo después de que `python bot_completo.py`
     termine: si existe, lo borra y sale del bucle (`goto :fin`) en vez de reiniciar el bot a
     los 10s como hace siempre ante cualquier otro tipo de cierre (crash, congelación...). Esta
     es la clave que distingue "parada limpia pedida por el usuario" de "el bot se cayó solo,
     hay que reiniciarlo".
  3. Al arrancar de nuevo (`main()`), si por lo que sea sigue existiendo el flag (p.ej. se
     arrancó `python bot_completo.py` a mano sin pasar por `/arrancar`, que ya lo borra), se
     ignora con un aviso en el log — un arranque nuevo nunca debe autopararse de inmediato.
- **`/log`**: no hay `journalctl`. `bot_completo.py` ahora también escribe cada línea de
  `log()` a un archivo (`bot_completo.log`, en la misma carpeta), además de la consola de
  siempre — con una rotación simple (se trunca a la mitad si supera 5 MB, no crece sin límite
  en un bot 24/7). `/log` lee las últimas líneas de ese archivo y aplica el mismo filtrado de
  ruido rutinario y formato español de números que ya usaba `telegram_bot.py` para Alpaca
  (lista `FRAGMENTOS_RUIDO_LOG`, adaptada a los mensajes propios de `bot_completo.py`).
- **`/cartera`, `/hoy`, `/ayer`, `/semana`**: reutilizan `cartera_ibkr.py`, al que se le
  añadieron versiones `formatear_*()` (igual que ya tenía `cartera_alpaca.py`) que devuelven
  texto listo para Telegram (`html=True`, tablas `<pre>` monoespaciadas) en vez de solo
  imprimir por `print()`. `formatear_posiciones_abiertas()` necesita una conexión `ib` propia
  ya abierta (pide precios en vivo) — `telegram_bot_ibkr.py` abre y cierra esa conexión en cada
  comando, con el mismo `clientId=9` de siempre (`cartera.CLIENT_ID_CARTERA`), distinto del
  `clientId=1` del bot en marcha, para poder consultar sin interferir. **Importante**: estos
  cuatro comandos fallan con un error de conexión si IB Gateway no está abierto/con sesión
  iniciada en ese momento (se muestra el error tal cual en la respuesta de Telegram) —
  `/estado`, `/arrancar` y `/parar` en cambio NO necesitan IB Gateway abierto, solo miran
  archivos y procesos locales de Windows.

**Notificaciones automáticas de compra/venta**: se añadió `notificar_telegram()` y
`formato_es()` a `bot_completo.py` (copiados tal cual del patrón ya probado en
`bot_alpaca.py`) y se enganchó una llamada justo después de cada `registrar_operacion_historial()`
que confirma una operación ejecutada — los 2 puntos de compra (acciones y cripto) y los 3 de
venta (normal, forzada, cripto). Si `TELEGRAM_BOT_TOKEN`/`TELEGRAM_CHAT_ID` no están puestas en
el entorno, `notificar_telegram()` simplemente no hace nada (el bot funciona igual sin
Telegram, es un extra opcional) — por eso no rompió ningún test existente al añadirlo.
**Pendiente/no incluido en esta primera fase**: un resumen automático diario por Telegram (como
sí tiene Alpaca) no se implementó para IBKR — el resumen de cierre de mercado
(`generar_resumen_cierre_mercado`) solo corre para US/HK/KR (no cripto, ver más arriba) y solo
imprime por log, no está conectado a `notificar_telegram()`. Se puede añadir si el usuario lo
pide.

**Cómo ejecutarlo**: variables de entorno `TELEGRAM_BOT_TOKEN`/`TELEGRAM_CHAT_ID` (el mismo bot
de Telegram que ya tenía creado para Alpaca puede reutilizarse, o crear uno nuevo con
@BotFather — un chat puede hablar con varios bots distintos sin problema), luego
`python telegram_bot_ibkr.py` en una ventana de CMD aparte en el mismo PC que IB Gateway. Debe
correr en la MISMA máquina (`/arrancar`, `/parar` y `/log` actúan sobre archivos/procesos
locales de esa máquina, no tendría sentido correrlo en otro sitio). Para que arranque solo sin
tener que acordarse, se puede añadir como Tarea Programada de Windows al iniciar sesión (igual
que se sugiere para `vigilante_externo.ps1`).

## Bugs importantes encontrados y corregidos (orden cronológico)

1. **Cuelgues por desconexión en esperas largas**: `time.sleep()` congelaba el bucle de
   eventos de `ib_async` e impedía detectar cortes de conexión. → esperas en tramos de 30s
   con `ib.sleep()` (`esperar_pumpeando`), reconexión con hasta 5 reintentos.
2. **Venta por MACD bajista**: cambiada de orden límite a **orden a mercado** (a petición
   del usuario).
3. **Un fallo en un valor abortaba todo el ciclo**: si una posición o señal de compra
   lanzaba una excepción inesperada, se perdía el resto del escaneo (incluidas COMPRAS
   enteras si el fallo era en VENTAS). → try/except individual por posición/señal.
4. **Regla de compra "4 cortas alcistas"**: atajo añadido a petición del usuario (ver arriba).
5. **`revisar_ventas` no comprobaba el horario de mercado antes de vender**: causaba
   intentos de venta fuera de horario que IBKR cancelaba, con el log mostrando
   engañosamente "PreSubmitted" (no reflejaba la cancelación real). → se añadió el
   chequeo de horario, igual que ya tenía `revisar_compras`.
6. **Estado de orden leído demasiado pronto**: esperaba 3s fijos tras colocar la orden;
   IBKR a veces manda un aviso benigno (`Error 10349`, "TIF ajustado a DAY") con un estado
   transitorio `Cancelled` que NO es el resultado final (la orden puede acabar `Filled`
   segundos después). → `esperar_estado_final_orden`: espera activa hasta un estado
   terminal o 10s máximo.
7. **Cortacircuitos ante caída de datos de mercado**: visto en producción, tras un corte
   de conectividad (`Error 1100`) el bot se quedó **~3 horas** reintentando 3×15s por cada
   valor sin ningún dato real (TWS/Gateway había perdido la conexión con los "market data
   farms" de IBKR aunque el socket API seguía respondiendo). → tras 8 valores seguidos sin
   ningún dato, deja de reintentar y avisa una vez (`UMBRAL_FALLOS_SEGUIDOS_DATOS`).
8. **Posible cuelgue en `reqExecutions`**: un episodio real de ~38 minutos sin ningún log
   (ni siquiera el "Iniciando nuevo ciclo") coincidió con la ventana de generación del
   resumen de cierre de mercado US. No se pudo reproducir ni arreglar de raíz (parece un
   problema de IBKR/TWS), pero se añadió un log justo antes de `ib.reqExecutions()` para
   localizarlo con precisión si se repite.
9. **BUG CRÍTICO — órdenes fraccionarias nunca se ejecutaban**: IBKR rechaza (`Error 10243`)
   cualquier orden con cantidad de acciones fraccionaria en `totalQuantity` enviada por
   API. Desde que se activaron las fracciones en US, **ninguna compra fraccionaria se había
   ejecutado nunca** (todas quedaban `Cancelled`). → se arregló usando el campo `cashQty`
   de IBKR (importe en efectivo, no nº de acciones) para toda orden fraccionaria, tanto en
   compras como en ventas. **Sigue sin verificarse contra una compra fraccionaria real** tras
   el arreglo — pendiente de confirmar en la próxima que se dispare.
10. **El estado de la orden puede mentir**: caso real confirmado con una venta de JPM que
    el log marcó como `Cancelled` (0 acciones vendidas), pero que en realidad **sí se
    ejecutó** — se vio en el resumen de cierre de mercado, que usa `reqExecutions()`
    (fuente de verdad de IBKR), no el objeto `Trade` que sigue nuestro código. Pasa cuando
    IBKR cancela la orden original por dentro (p.ej. tras ajustar el TIF) y la reenvía como
    una orden de reemplazo que nuestro código nunca llega a rastrear. → cuando una orden no
    confirma `Filled`, ahora se vuelve a consultar la posición real en IBKR unos segundos
    después y se compara con la cantidad de antes, dando un veredicto fiable en el log.
11. **Fecha de apertura "?" para posiciones de días anteriores**: `reqExecutions()` de IBKR
    solo cubre el día actual, así que el resumen de cierre no podía saber cuándo se compró
    por primera vez un valor comprado en un día anterior. → historial persistente local
    (`historial_compras.json`), ver sección dedicada más arriba.
12. **Vigilante interno no despertaba solo (GIL)**: ver sección dedicada más arriba
    ("La solución real: vigilante EXTERNO"). → `vigilante_externo.ps1`.
13. **Fila de operación cerrada con "Abierta desde" DESPUÉS de "Cerrada a las" (caso real:
    QCOM)**: en la tabla de "Operaciones cerradas hoy", la fecha de apertura mostrada
    siempre venía del historial persistente (`historial_compras.json`), que guarda la
    apertura de la posición **actual/más reciente** de ese símbolo. Si el bot volvía a
    comprar el mismo valor el mismo día, DESPUÉS de haber cerrado una ronda anterior, el
    historial ya tenía la fecha de esa compra nueva y posterior — así que la fila de la
    venta antigua mostraba una "Abierta desde" con hora **posterior** a "Cerrada a las"
    (visto en producción: QCOM "Abierta desde 17:18" / "Cerrada a las 15:13"). → en esa
    tabla concreta ahora se prioriza la compra calculada a partir de `reqExecutions()`
    (ya filtrada a compras anteriores a esa venta) sobre la fecha del historial; solo se
    usa el historial si no hay ninguna compra de hoy que explique la venta (posición
    arrastrada de un día anterior). La tabla de posiciones **abiertas** no tenía este
    problema (solo hay una ronda en curso por definición) y sigue igual.
14. **`VZ` sin fecha de apertura (`N/D`)**: cuando ni `reqExecutions()` (solo cubre hoy) ni
    el historial persistente tienen ningún registro de compra para ese símbolo, no hay
    forma de saber cuándo se abrió — típicamente porque la posición ya existía en la
    cuenta antes de que existiera el historial, o se abrió fuera del bot. **No es un bug
    recuperable**: no hay dato que mostrar. Se mostrará bien la próxima vez que ese valor
    se compre de cero (quedará registrado en el historial en ese momento).

## Cosas que NO son bugs (para no perder tiempo re-investigándolas)

- **`Error 10349` ("Order TIF was set to DAY based on order preset")**: aviso rutinario y
  benigno de IBKR. El texto de log que lo acompaña (`Canceled order: Trade(...)`) muestra
  un estado `Cancelled` **transitorio**, no el resultado final. Confirmado con múltiples
  muestras en producción: casi siempre la orden acaba `Filled` segundos después.
- **`Error 1100` / `Error 1102`**: pérdida/recuperación de conectividad entre IBKR y
  TWS/Gateway. El bot se recupera solo la mayoría de las veces (reconexión automática).
  Si persiste (timeouts de datos durante horas), puede hacer falta reiniciar TWS/Gateway
  manualmente, no solo el script de Python (se comprobó que reiniciar solo el script no
  arregla una caída de los market data farms).
- **Ráfagas de `Error handling fields... KeyError` con traceback de `ib_async`**: fallo
  interno de la librería al recibir respuestas tardías de peticiones antiguas tras una
  reconexión. No rompe el bot, es ruido.
- **`Warning 2161`**: control regulatorio de IBKR que limita el precio inicial de órdenes
  a mercado agresivas en Corea. Informativo, no accionable.

## Decisiones explícitas del usuario (no cambiar sin preguntar)

- Ciclo cada 4 minutos (pidió esto explícitamente; 3 minutos se desaconsejó por el tamaño
  de las listas de valores).
- Venta por MACD bajista: a mercado, no límite.
- Regla de compra: atajo de "4 cortas alcistas" compra directo.
- Si una orden se cancela de verdad (no el aviso benigno), **no se reintenta automáticamente**
  — se deja que el siguiente ciclo (4 min después) reevalúe la señal desde cero.

## Preguntas abiertas / sin decidir

- ¿Desactivar HK y/o KR para la cuenta real de 300€, dado que probablemente no puedan
  operar por los lotes mínimos?
- ¿Subir `INTERVALO_SEGUNDOS` por encima de 4 min ahora que HK tiene 28 valores?
- El PR #1 sigue abierto sin fusionar a `main`.

## Cómo retomar el trabajo en una conversación nueva

1. Pide que se añada el repo (`inverapas-lang/Bot-Intradia-Repositorio`) si no está ya.
2. Trabajar sobre la rama `claude/bot-development-steps-tr4a6x` (no crear una rama nueva).
3. Leer este archivo primero, luego `bot_completo.py` y el historial de commits (`git log`)
   si hace falta más detalle de una decisión concreta.
4. Antes de tocar código, correr `python3 tests/test_bot_completo.py` para confirmar que
   se parte de una base que pasa.
5. Tras cualquier cambio: compilar (`python3 -m py_compile bot_completo.py`), correr los
   tests, hacer commit y push a la misma rama (se refleja solo en el PR #1).
6. El usuario descarga manualmente `bot_completo.py` actualizado a su máquina Windows tras
   cada cambio — no hay despliegue automático.
