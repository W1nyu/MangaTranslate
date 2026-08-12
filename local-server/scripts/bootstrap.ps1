param(
    [string]$PythonPath = 'C:\Users\Winyu\AppData\Local\Programs\Python\Python310\python.exe',
    [string]$SourceLanguage = 'en',
    [string]$TargetLanguage = 'ko'
)

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$venvPython = Join-Path $root '.venv\Scripts\python.exe'

if (-not (Test-Path -LiteralPath $PythonPath)) {
    throw "Python을 찾지 못했습니다: $PythonPath. Python 3.10–3.13의 python.exe 경로를 -PythonPath로 지정하세요."
}

if (-not (Test-Path -LiteralPath $venvPython)) {
    & $PythonPath -m venv (Join-Path $root '.venv')
}

Push-Location $root
try {
    & $venvPython -m pip install --upgrade pip
    & $venvPython -m pip install --no-cache-dir -r requirements.txt
    & $venvPython scripts\doctor.py
    & $venvPython scripts\install_models.py --from $SourceLanguage --to $TargetLanguage
    & $venvPython scripts\smoke_test.py --from $SourceLanguage --to $TargetLanguage --text 'Hello, SEOUL!'
    Write-Output "설치와 모델 검증이 완료됐습니다. 다음으로 서버를 시작하세요: $venvPython scripts\run_server.py"
} finally {
    Pop-Location
}
