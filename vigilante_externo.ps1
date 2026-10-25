# vigilante_externo.ps1
#
# Vigilante EXTERNO del bot de trading. A diferencia del vigilante que corre
# como hilo DENTRO de bot_completo.py (vigilante_congelacion), este script es
# un proceso de Windows totalmente aparte. Eso importa porque si el bot se
# queda congelado dentro de una llamada bloqueante a IBKR que nunca cede el
# turno (el "GIL" de Python), NINGUN hilo dentro de ese mismo proceso Python
# puede ejecutarse -ni siquiera su propio vigilante interno-. Se vio esto en
# produccion: el aviso del vigilante interno no salio solo, hizo falta pulsar
# Ctrl+C para que "despertara". Este script, al ser un proceso de Windows
# distinto, no tiene ese problema: sigue corriendo pase lo que pase dentro
# del interprete de Python.
#
# Como funciona:
#   1. bot_completo.py escribe su PID en "bot.pid" y refresca "latido_bot.txt"
#      (con la hora actual) cada vez que hace algo (cada log(), como maximo
#      cada 10 segundos).
#   2. Este script comprueba cada minuto cuando se modifico "latido_bot.txt"
#      por ultima vez.
#   3. Si ha pasado mas tiempo del umbral, asume que el bot esta congelado,
#      lee el PID de "bot.pid" y lo mata con Stop-Process -Force.
#   4. run.bot.bat (que debe estar corriendo el bot en un bucle) detecta que
#      el proceso murio y lo vuelve a lanzar automaticamente.
#
# Como usarlo:
#   - Dejar este script corriendo en una ventana de PowerShell aparte, a la
#     vez que run.bot.bat corre el bot en su propia ventana:
#         powershell -ExecutionPolicy Bypass -File vigilante_externo.ps1
#   - O, mejor, crear una Tarea Programada en Windows que lo lance solo al
#     iniciar sesion, para no tener que acordarte de abrirlo a mano.

$carpeta = $PSScriptRoot
$archivoLatido = Join-Path $carpeta "latido_bot.txt"
$archivoPid = Join-Path $carpeta "bot.pid"

# Debe ser mayor que UMBRAL_CONGELACION_SEGUNDOS (20 min) en bot_completo.py,
# para dejarle al vigilante interno la primera oportunidad de reaccionar.
# Este es el respaldo de verdad si aquel no puede.
$umbralMinutos = 25

Write-Host "[VIGILANTE EXTERNO] Arrancado. Vigilando '$archivoLatido' cada 60s (umbral: $umbralMinutos min)."

while ($true) {
    Start-Sleep -Seconds 60

    if (-not (Test-Path $archivoLatido)) {
        Write-Host "[VIGILANTE EXTERNO] Aun no existe el archivo de latido, esperando a que el bot arranque..."
        continue
    }

    $ultimaEscritura = (Get-Item $archivoLatido).LastWriteTime
    $minutos = (New-TimeSpan -Start $ultimaEscritura -End (Get-Date)).TotalMinutes

    if ($minutos -le $umbralMinutos) {
        continue
    }

    Write-Host "[VIGILANTE EXTERNO] El bot lleva $([math]::Round($minutos, 1)) minutos sin dar señales de vida. Forzando su cierre..."

    if (-not (Test-Path $archivoPid)) {
        Write-Host "[VIGILANTE EXTERNO] No existe '$archivoPid': no se puede saber que proceso matar. Reinicia el bot a mano."
        continue
    }

    $procId = Get-Content $archivoPid -ErrorAction SilentlyContinue
    if (-not $procId) {
        Write-Host "[VIGILANTE EXTERNO] El archivo de PID esta vacio. Reinicia el bot a mano."
        continue
    }

    try {
        Stop-Process -Id $procId -Force -ErrorAction Stop
        Write-Host "[VIGILANTE EXTERNO] Proceso $procId cerrado. run.bot.bat deberia reiniciarlo solo en unos segundos."
    } catch {
        Write-Host "[VIGILANTE EXTERNO] No se pudo cerrar el proceso $procId (puede que ya no exista o ya se haya reiniciado): $_"
    }
}
