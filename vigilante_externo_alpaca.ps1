# vigilante_externo_alpaca.ps1
#
# Vigilante EXTERNO del bot de Alpaca (analogo a vigilante_externo.ps1, para
# el bot de IBKR). Proceso de Windows totalmente aparte del interprete de
# Python, para poder actuar aunque el bot se quede congelado dentro de una
# llamada bloqueante que no cede el turno (el "GIL" de Python impide que
# cualquier hilo INTERNO, incluido el propio vigilante del bot, se ejecute
# en ese caso).
#
# Como funciona:
#   1. bot_alpaca.py escribe su PID en "bot_alpaca.pid" y refresca
#      "latido_bot_alpaca.txt" (con la hora actual) cada vez que hace algo
#      (cada log(), como maximo cada 10 segundos).
#   2. Este script comprueba cada minuto cuando se modifico
#      "latido_bot_alpaca.txt" por ultima vez.
#   3. Si ha pasado mas tiempo del umbral, asume que el bot esta congelado,
#      lee el PID de "bot_alpaca.pid" y lo mata con Stop-Process -Force.
#   4. run.bot.alpaca.bat (que debe estar corriendo el bot en un bucle)
#      detecta que el proceso murio y lo vuelve a lanzar automaticamente.
#
# Como usarlo:
#   - Dejar este script corriendo en una ventana de PowerShell aparte, a la
#     vez que run.bot.alpaca.bat corre el bot en su propia ventana:
#         powershell -ExecutionPolicy Bypass -File vigilante_externo_alpaca.ps1
#   - O, mejor, crear una Tarea Programada en Windows que lo lance solo al
#     iniciar sesion (igual que se hizo con vigilante_externo.ps1, ver
#     NOTES.md), para no tener que acordarte de abrirlo a mano.
#
# Usa nombres de archivo DISTINTOS a vigilante_externo.ps1 (que vigila
# latido_bot.txt/bot.pid, del bot de IBKR) para poder tener los dos
# vigilantes corriendo a la vez sin que se pisen.

$carpeta = $PSScriptRoot
$archivoLatido = Join-Path $carpeta "latido_bot_alpaca.txt"
$archivoPid = Join-Path $carpeta "bot_alpaca.pid"

# Debe ser mayor que UMBRAL_CONGELACION_SEGUNDOS (20 min) en bot_alpaca.py,
# para dejarle al vigilante interno la primera oportunidad de reaccionar.
# Este es el respaldo de verdad si aquel no puede.
$umbralMinutos = 25

Write-Host "[VIGILANTE EXTERNO ALPACA] Arrancado. Vigilando '$archivoLatido' cada 60s (umbral: $umbralMinutos min)."

while ($true) {
    Start-Sleep -Seconds 60

    if (-not (Test-Path $archivoLatido)) {
        Write-Host "[VIGILANTE EXTERNO ALPACA] Aun no existe el archivo de latido, esperando a que el bot arranque..."
        continue
    }

    $ultimaEscritura = (Get-Item $archivoLatido).LastWriteTime
    $minutos = (New-TimeSpan -Start $ultimaEscritura -End (Get-Date)).TotalMinutes

    if ($minutos -le $umbralMinutos) {
        continue
    }

    Write-Host "[VIGILANTE EXTERNO ALPACA] El bot lleva $([math]::Round($minutos, 1)) minutos sin dar señales de vida. Forzando su cierre..."

    if (-not (Test-Path $archivoPid)) {
        Write-Host "[VIGILANTE EXTERNO ALPACA] No existe '$archivoPid': no se puede saber que proceso matar. Reinicia el bot a mano."
        continue
    }

    $procId = Get-Content $archivoPid -ErrorAction SilentlyContinue
    if (-not $procId) {
        Write-Host "[VIGILANTE EXTERNO ALPACA] El archivo de PID esta vacio. Reinicia el bot a mano."
        continue
    }

    try {
        Stop-Process -Id $procId -Force -ErrorAction Stop
        Write-Host "[VIGILANTE EXTERNO ALPACA] Proceso $procId cerrado. run.bot.alpaca.bat deberia reiniciarlo solo en unos segundos."
    } catch {
        Write-Host "[VIGILANTE EXTERNO ALPACA] No se pudo cerrar el proceso $procId (puede que ya no exista o ya se haya reiniciado): $_"
    }
}
