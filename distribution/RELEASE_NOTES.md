다른 PC에 설치할 수 있는 Chrome/Edge 확장 프로그램 + Windows 로컬 OCR 서버 배포본입니다.

## 다운로드 및 설치

1. 아래 Assets에서 **manga-translate-0.6.0-windows.zip**을 다운로드하고 압축을 해제하세요.
2. 압축 안의 **INSTALL.md** 안내에 따라 Python 3.10/3.11 환경과 OCR 모델을 설치하세요.
3. 본인의 DeepL 또는 Azure API 키를 `local-server/.env`에 입력하세요.
4. `start-server.cmd`를 실행하고 Chrome 확장 프로그램 관리에서 개발자 모드로 `extension` 폴더를 로드하세요.

**개발자의 API 키, 개인 설정, 사용량 기록은 포함되어 있지 않습니다.** 사용자마다 본인의 키와 로컬 서버가 필요합니다. 유료 API 전환은 기본적으로 꺼져 있습니다.

- `manga-translate-0.6.0-extension.zip`: 확장 프로그램 파일만 제공. 로컬 서버가 별도로 필요합니다.
- `manga-translate-0.6.0-windows.zip`: 확장 프로그램 및 로컬 서버 소스, 설치·실행 스크립트, 설치 안내와 데이터 처리 안내.
- `SHA256SUMS.txt`: 다운로드 파일 SHA-256 검증값.

Chrome 웹 스토어 심사를 거친 등록본은 아니며 개발자 모드 수동 설치 배포입니다. 현재 작업본을 검증해 만든 ZIP을 사용하세요. GitHub가 자동 제공하는 Source code ZIP/TAR는 기준 커밋의 소스이므로 이 설치 배포본과 다릅니다.

검증: 확장 프로그램 테스트 74개, 서버 테스트 111개 통과. ZIP 허용 파일 목록·압축 무결성·로컬 API 키 혼입 검사 완료. 새 PC에서의 전체 모델 다운로드 및 설치는 별도 확인이 필요합니다.
