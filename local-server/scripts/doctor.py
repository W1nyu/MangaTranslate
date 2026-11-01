"""Report whether the local server environment is ready without changing it."""
from __future__ import annotations

import importlib.util
import platform
import sys


REQUIRED_MODULES = {
    "fastapi": "fastapi",
    "uvicorn": "uvicorn",
    "pydantic": "pydantic",
    "PIL": "pillow",
    "numpy": "numpy",
    "paddle": "paddlepaddle",
    "paddleocr": "paddleocr",
    "onnxruntime": "onnxruntime",
    "argostranslate": "argostranslate",
}


def main() -> int:
    print(f"Python: {sys.version.split()[0]} ({platform.architecture()[0]}, {platform.machine()})")
    missing = [distribution for module, distribution in REQUIRED_MODULES.items() if importlib.util.find_spec(module) is None]
    if missing:
        print("누락된 패키지: " + ", ".join(missing))
        print("다음 명령을 실행하세요: .\\.venv\\Scripts\\python.exe -m pip install -r requirements.txt")
        return 1
    print("필수 Python 패키지가 준비됐습니다.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
