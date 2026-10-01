# SimpleQuant update helper. Started by simplequant/update/client.py (Updater.apply) right before the app exits.
# Keep this file ASCII-only: Windows PowerShell 5.1 reads scripts without a BOM as the ANSI code page.
#
# 1. Wait until no process is running from the install folder (the app, its worker processes, a scheduled
#    paper-trading run). Gives up after 90 s without changing anything.
# 2. mode "patch":     check every staged file's SHA256, move the old files to a backup folder, copy the new ones,
#                      move removed files to the backup too. Any error restores the backup, so the old version stays.
#    mode "installer": run the downloaded Inno Setup installer silently into the same folder.
# 3. Write result.json (read by the app on its next start) and start SimpleQuant again.

param([Parameter(Mandatory = $true)][string]$Plan)

$ErrorActionPreference = 'Stop'
$p = Get-Content -LiteralPath $Plan -Raw -Encoding UTF8 | ConvertFrom-Json
$app = [IO.Path]::GetFullPath($p.app_dir).TrimEnd('\')
$exe = Join-Path $app 'SimpleQuant.exe'
$backup = Join-Path $p.work 'backup'

function Write-Log([string]$msg) {
    try { Add-Content -LiteralPath $p.log -Value ('{0:yyyy-MM-dd HH:mm:ss} {1}' -f (Get-Date), $msg) -Encoding UTF8 } catch {}
}

function Write-Result([bool]$ok, [string]$msg) {
    Write-Log "result ok=$ok $msg"
    [ordered]@{ ok = $ok; version = $p.version; message = $msg } | ConvertTo-Json |
        Set-Content -LiteralPath $p.result -Encoding UTF8
}

function Test-Hash([string]$file, [string]$sha) {
    (Test-Path -LiteralPath $file) -and ((Get-FileHash -LiteralPath $file -Algorithm SHA256).Hash -eq $sha.ToUpperInvariant())
}

function Get-Native([string]$rel) { $rel.Replace('/', '\') }

function Wait-AppExit {
    $prefix = $app + '\'
    $deadline = (Get-Date).AddSeconds(90)
    while ($true) {
        $busy = @(Get-Process -ErrorAction SilentlyContinue | Where-Object {
            $path = $null
            try { $path = $_.Path } catch {}
            $path -and $path.StartsWith($prefix, [StringComparison]::OrdinalIgnoreCase)
        })
        if ($busy.Count -eq 0) { return $true }
        if ((Get-Date) -gt $deadline) { return $false }
        Start-Sleep -Milliseconds 500
    }
}

function Invoke-Patch {
    foreach ($f in @($p.files)) {
        if (-not (Test-Hash (Join-Path $p.staging (Get-Native $f.path)) $f.sha256)) { throw "verify: $($f.path)" }
    }
    if (Test-Path -LiteralPath $backup) { Remove-Item -LiteralPath $backup -Recurse -Force }
    # Paths that were moved to the backup or newly created; rollback undoes exactly these
    $touched = New-Object System.Collections.Generic.List[string]
    try {
        foreach ($f in @($p.files)) {
            $rel = Get-Native $f.path
            $dst = Join-Path $app $rel
            if (Test-Path -LiteralPath $dst) {
                $bak = Join-Path $backup $rel
                [void][IO.Directory]::CreateDirectory((Split-Path -Parent $bak))
                Move-Item -LiteralPath $dst -Destination $bak -Force
            }
            $touched.Add($rel)
            [void][IO.Directory]::CreateDirectory((Split-Path -Parent $dst))
            Copy-Item -LiteralPath (Join-Path $p.staging $rel) -Destination $dst -Force
        }
        foreach ($r in @($p.removed)) {
            $rel = Get-Native $r
            $dst = Join-Path $app $rel
            if (Test-Path -LiteralPath $dst) {
                $bak = Join-Path $backup $rel
                [void][IO.Directory]::CreateDirectory((Split-Path -Parent $bak))
                Move-Item -LiteralPath $dst -Destination $bak -Force
                $touched.Add($rel)
            }
        }
    } catch {
        $err = $_
        Write-Log "error: $err; restoring $($touched.Count) files"
        for ($i = $touched.Count - 1; $i -ge 0; $i--) {
            $dst = Join-Path $app $touched[$i]
            $bak = Join-Path $backup $touched[$i]
            try {
                if (Test-Path -LiteralPath $dst) { Remove-Item -LiteralPath $dst -Force }
                if (Test-Path -LiteralPath $bak) { Move-Item -LiteralPath $bak -Destination $dst -Force }
            } catch { Write-Log "restore failed: $($touched[$i]): $_" }
        }
        throw $err
    }
    # Old bytecode next to replaced .py files would be ignored anyway (timestamps differ); remove it to be safe
    foreach ($f in @($p.files)) {
        if ($f.path.EndsWith('.py')) {
            $cache = Join-Path (Split-Path -Parent (Join-Path $app (Get-Native $f.path))) '__pycache__'
            if (Test-Path -LiteralPath $cache) { Remove-Item -LiteralPath $cache -Recurse -Force -ErrorAction SilentlyContinue }
        }
    }
    try {
        if (Test-Path -LiteralPath $p.uninstall_key) {
            Set-ItemProperty -LiteralPath $p.uninstall_key -Name DisplayVersion -Value $p.version
        }
    } catch { Write-Log "registry: $_" }
    Remove-Item -LiteralPath $backup -Recurse -Force -ErrorAction SilentlyContinue
}

function Invoke-Installer {
    if (-not (Test-Hash $p.installer $p.sha256)) { throw 'verify: installer' }
    $log = Join-Path $p.work 'installer.log'
    $argv = @('/VERYSILENT', '/SUPPRESSMSGBOXES', '/NORESTART', '/SP-', ('/DIR="{0}"' -f $app), ('/LOG="{0}"' -f $log))
    $proc = Start-Process -FilePath $p.installer -ArgumentList $argv -Wait -PassThru
    if ($proc.ExitCode -ne 0) { throw "installer exit code $($proc.ExitCode)" }
}

Write-Log "start: $($p.mode) -> $($p.version), app $app"
if (-not (Wait-AppExit)) {
    Write-Result $false 'busy'     # still running: change nothing and do not start a second copy
    exit 1
}
$ok = $true
try {
    if ($p.mode -eq 'installer') { Invoke-Installer } else { Invoke-Patch }
    Write-Result $true ''
} catch {
    $ok = $false
    Write-Result $false ([string]$_.Exception.Message)
}
if (-not $p.no_restart) { Start-Process -FilePath $exe -WorkingDirectory $app }
if (-not $ok) { exit 1 }
