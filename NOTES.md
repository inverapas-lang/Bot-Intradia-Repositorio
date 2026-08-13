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

## Listas de valores actuales

- `ACTIVOS_US`: 40 tickers (large caps NYSE + Nasdaq: LLY, UBER, ORCL, PLTR, SNOW, CRM, JPM,
  BAC, C, GS, MS, XOM, CVX, GE, CAT, WMT, V, MA, UNH, PFE, AAPL, MSFT, NVDA, GOOGL, AMZN,
  META, AVGO, TSLA, COST, AMD, NFLX, ASML, QCOM, INTC, HON, CSCO, CMCSA, AMGN, ISRG, AMAT).
- `ACTIVOS_HK`: 28 tickers (blue chips de Hong Kong).
- `ACTIVOS_KR`: 10 tickers (blue chips de Corea).
- Total activo en `ACTIVOS` (US+HK+KR): **78 valores**. EU excluido.

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
6. Órdenes siempre **a mercado** (no límite).
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

## Plan B ante valores sin fracciones habilitadas en la API (error 10244)

No todos los valores de US tienen habilitadas las fracciones vía API en IBKR, aunque el
mercado en general sí las soporte — es una restricción por instrumento del lado de IBKR, no
un fallo nuestro. Visto en producción con `ORCL`, `PLTR`, `CAT`, `NFLX`, `INTC`: la orden por
`cashQty` se cancela con `Error 10244: La cantidad de efectivo no puede utilizarse en esta
orden`. Antes, esa señal de compra simplemente se perdía.

Ahora `orden_rechazada_por_codigo(trade, {10244})` detecta este código concreto en el registro
de la orden (`trade.log`), y si pasa, se reintenta automáticamente con **acciones enteras**
(redondeando hacia abajo el presupuesto disponible), en vez de rendirse. Si el presupuesto no
llega ni para 1 acción entera, ahí sí se omite con un aviso claro en el log.

## Comisiones estimadas

`0.07%` del valor de la operación, con mínimo de 1€ (convertido a la divisa local). Se
usa para descontar del beneficio bruto y decidir si vender, y para las tablas del resumen
de cierre.

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
