# 말풍선 번역 레이어 설치

이 배포본은 Windows용 로컬 서버와 Chrome/Edge 확장 프로그램입니다. 확장 프로그램만 설치하면 번역되지 않습니다. Python 3.10 또는 3.11(64비트), 인터넷 연결, 본인의 DeepL API Free 또는 Azure Translator 키가 필요합니다. 개발자의 API 키는 제공되지 않습니다.

## 처음 설치

1. `manga-translate-0.7.0-windows.zip`을 다운로드해 원하는 폴더에 압축 해제합니다.
2. 압축 해제한 폴더에서 PowerShell을 열고 실행합니다.

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\local-server\scripts\bootstrap.ps1
```

Python을 자동으로 찾지 못하면 `-PythonPath '설치한 Python의 python.exe 전체 경로'`를 붙이세요. 첫 설치에는 패키지와 언어 모델 다운로드 시간이 필요합니다. 오류가 나면 다음 단계로 진행하지 말고 오류 내용을 확인하세요.

3. 키 입력용 파일을 만들고 엽니다. 기존 설정이 있으면 덮어쓰지 않습니다.

```powershell
if (-not (Test-Path .\local-server\.env)) { Copy-Item .\local-server\.env.example .\local-server\.env }
notepad .\local-server\.env
```

DeepL은 `DEEPL_AUTH_KEY`, Azure는 `AZURE_TRANSLATOR_KEY`와 `AZURE_TRANSLATOR_REGION`을 입력하고 저장합니다. Azure Translator 무료 등급(F0)은 매월 200만 자까지 무료이며, 리소스의 **Keys and Endpoint**에서 키와 위치(리전)를 복사합니다. 하나만 설정해도 됩니다. 유료 항목은 선택 사항이며 기본적으로 사용하지 않습니다.

4. `start-server.cmd`를 더블클릭합니다. 서버 창은 번역 중 계속 열어 두세요.
5. Chrome 주소창에서 `chrome://extensions`를 열고 **개발자 모드 → 압축해제된 확장 프로그램을 로드합니다**를 선택한 뒤 배포 폴더의 `extension` 폴더를 지정합니다. Edge는 `edge://extensions`에서 같은 방법으로 설치합니다.
6. 웹툰 탭을 열거나 새로고침하고 확장 프로그램의 설정에서 **연결 확인**을 누릅니다. **화면 번역 시작**을 누른 뒤 영역을 선택합니다.

## 이후 실행과 업데이트

매번 `start-server.cmd`로 서버를 시작합니다. 키를 바꿨다면 서버를 다시 시작하세요. 업데이트 시 새 폴더에 압축을 풀고 본인의 `.env`를 직접 옮긴 뒤 설치를 다시 실행하고 Chrome에서 새 `extension` 폴더를 로드하세요. 기존 버전을 제거하면 사전과 설정이 지워질 수 있습니다.

현재 배포는 개발자 모드로 수동 설치하는 버전이며 Chrome 웹 스토어 등록본이 아닙니다. ZIP을 브라우저에 끌어놓아 설치하는 방식은 지원하지 않습니다. 확장 프로그램 ZIP은 확장 파일만 담고 있으므로 별도로 로컬 서버가 필요합니다.

## 데이터 처리

화면 캡처는 본인 PC의 OCR 서버로 전달됩니다. OCR 텍스트와 언어 설정은 본인이 선택한 번역 제공자에게 전송됩니다. 키는 본인 PC의 `.env`에 저장합니다. 자세한 내용은 `PRIVACY.md`를 확인하세요. 프로젝트 문의: https://github.com/W1nyu/MangaTranslate/issues
