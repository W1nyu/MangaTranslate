param(
    [string]$PythonPath = '',
    [string]$SourceLanguage = 'en',
    [string]$TargetLanguage = 'ko'
)

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$venvPython = Join-Path $root '.venv\Scripts\python.exe'

if (-not $PythonPath) {
    foreach ($version in @('3.11', '3.10')) {
        if (Get-Command py -ErrorAction SilentlyContinue) {
            $candidate = & py "-$version" -c 'import sys; print(sys.executable)' 2>$null
            if ($LASTEXITCODE -eq 0 -and $candidate) { $PythonPath = $candidate; break }
        }
    }
    if (-not $PythonPath -and (Get-Command python -ErrorAction SilentlyContinue)) {
        $PythonPath = (Get-Command python).Source
    }
}
if (-not $PythonPath -or -not (Test-Path -LiteralPath $PythonPath)) {
    throw "Python을 찾지 못했습니다: $PythonPath. Python 3.10–3.13의 python.exe 경로를 -PythonPath로 지정하세요."
}
& $PythonPath -c 'import sys; assert (3, 10) <= sys.version_info[:2] <= (3, 11), "Install Python 3.10 or 3.11 for this dependency set."'
if ($LASTEXITCODE -ne 0) { throw 'Python 3.10 or 3.11 is required.' }

if (-not (Test-Path -LiteralPath $venvPython)) {
    & $PythonPath -m venv (Join-Path $root '.venv')
    if ($LASTEXITCODE -ne 0) { throw 'Virtual environment creation failed.' }
}

Push-Location $root
try {
    & $venvPython -m pip install --upgrade pip
    if ($LASTEXITCODE -ne 0) { throw 'pip upgrade failed.' }
    & $venvPython -m pip install --no-cache-dir -r requirements.txt
    if ($LASTEXITCODE -ne 0) { throw 'Dependency installation failed.' }
    & $venvPython scripts\doctor.py
    if ($LASTEXITCODE -ne 0) { throw 'Environment check failed.' }
    & $venvPython scripts\install_models.py --from $SourceLanguage --to $TargetLanguage
    if ($LASTEXITCODE -ne 0) { throw 'Model installation failed.' }
    & $venvPython scripts\smoke_test.py --from $SourceLanguage --to $TargetLanguage --text 'Hello, SEOUL!'
    if ($LASTEXITCODE -ne 0) { throw 'Model verification failed.' }
    Write-Output "설치와 모델 검증이 완료됐습니다. 다음으로 서버를 시작하세요: $venvPython scripts\run_server.py"
} finally {
    Pop-Location
}
