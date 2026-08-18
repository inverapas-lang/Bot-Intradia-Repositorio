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

Alpaca sí admite fracciones de verdad vía API (a diferencia de los
problemas que dimos con `cashQty` en IBKR), pero con una regla fija de la
propia plataforma:

- Las órdenes **fraccionarias** (cantidad no entera, o por importe/`notional`)
  **solo se admiten con tipo MARKET y `time_in_force=DAY`**.
- Las órdenes **fuera de sesión regular** (pre/postmercado, `extended_hours=True`)
  **solo se admiten como LIMITADAS con `time_in_force=DAY`**.
- Combinando ambas reglas: **una orden fraccionaria fuera de sesión regular
  es imposible** — no hay combinación válida.

Consecuencias implementadas:
- **Compras en pre/postmercado**: se calcula la cantidad ENTERA máxima que
  cabe en el presupuesto y se manda una orden LIMITADA al precio exacto
  (`extended_hours=True`). Si no llega ni para 1 acción entera, se omite
  (igual que ya hacíamos en el bot de IBKR).
- **Ventas en pre/postmercado**: si la posición es fraccionaria, **se omite
  la venta hasta la próxima sesión regular** — no hay forma de venderla
  fuera de sesión regular en Alpaca. Si la posición es de acciones enteras,
  se vende con normalidad (orden limitada al precio exacto).

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

### Resumen de cierre

Versión simplificada respecto al de IBKR: solo posiciones abiertas (precio
actual, P/L no realizado). **No** tiene todavía la tabla de "operaciones
cerradas hoy" — para eso haría falta consultar el historial de órdenes de
Alpaca (`get_orders`), pendiente si se necesita más adelante.

### Historial de fecha de apertura

**No implementado todavía** (el `historial_compras.json` persistente que sí
tiene el bot de IBKR). Como el resumen de Alpaca por ahora no muestra fecha
de apertura, no hace falta de momento — pendiente si se añade esa tabla.

## Activos

Misma lista de 30 tickers de US que en `bot_completo.py` (`ACTIVOS_US`),
copiada literalmente como lista simple de símbolos (Alpaca no necesita
`exchange`/`currency` por ticker, todo es US/USD).

## Pendiente / próximos pasos

- Probar A FONDO en modo paper antes de pasar a real (en curso).
- Si se quiere la tabla de "operaciones cerradas hoy" en el resumen, usar
  `TradingClient.get_orders()` con filtro de fecha.
- Cuando llegue el momento de mover esto a la nube (objetivo declarado del
  usuario: gestionar todo desde el móvil, sin gastos iniciales), este bot
  es el candidato natural para ir primero — no depende de una app de
  escritorio como IB Gateway, solo de las variables de entorno con las API
  keys.
