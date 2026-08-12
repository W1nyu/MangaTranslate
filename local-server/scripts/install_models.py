"""Download and install a free Argos Translate language pair into the local user cache."""
from __future__ import annotations

import argparse
from collections import deque
import sys
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.runtime import configure_local_runtime

configure_local_runtime()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Install an Argos Translate language pair.")
    parser.add_argument("--from", dest="source", help="Source ISO language code, e.g. en")
    parser.add_argument("--to", dest="target", help="Target ISO language code, e.g. ko")
    parser.add_argument("--list", action="store_true", help="List language pairs available from the Argos package index")
    return parser.parse_args()


def find_install_path(packages: list[Any], source: str, target: str) -> list[Any] | None:
    """Find the shortest installable model path, preferring a direct pair."""
    if source == target:
        return []
    direct = next((item for item in packages if item.from_code == source and item.to_code == target), None)
    if direct is not None:
        return [direct]

    outgoing: dict[str, list[Any]] = {}
    for package in packages:
        outgoing.setdefault(package.from_code, []).append(package)

    queue: deque[tuple[str, list[Any]]] = deque([(source, [])])
    visited = {source}
    while queue:
        current, path = queue.popleft()
        for package in outgoing.get(current, []):
            next_code = package.to_code
            next_path = [*path, package]
            if next_code == target:
                return next_path
            if next_code not in visited:
                visited.add(next_code)
                queue.append((next_code, next_path))
    return None


def main() -> int:
    args = parse_args()
    if not args.list and (not args.source or not args.target):
        print("Example: python scripts/install_models.py --from en --to ko", file=sys.stderr)
        return 2

    try:
        import argostranslate.package
    except ImportError:
        print("Argos Translate가 없습니다. `pip install -r requirements.txt`를 먼저 실행하세요.", file=sys.stderr)
        return 1

    print("Argos 언어 패키지 목록을 확인하는 중입니다...")
    try:
        argostranslate.package.update_package_index()
        packages = argostranslate.package.get_available_packages()
    except Exception as error:
        print(f"언어 패키지 목록을 가져오지 못했습니다: {error}", file=sys.stderr)
        print("인터넷 연결을 확인한 뒤 같은 명령을 다시 실행하세요.", file=sys.stderr)
        return 1
    if args.list:
        for package in sorted(packages, key=lambda item: (item.from_code, item.to_code)):
            print(f"{package.from_code:>3} → {package.to_code:<3}  {package.package_version}")
        return 0

    install_path = find_install_path(packages, args.source, args.target)
    if install_path is None:
        print(f"{args.source} → {args.target}에 사용할 수 있는 번역 모델 경로를 찾지 못했습니다.", file=sys.stderr)
        print("`python scripts/install_models.py --list`로 사용 가능한 조합을 확인하세요.", file=sys.stderr)
        return 1
    if not install_path:
        print("출발 언어와 도착 언어가 같아서 추가 모델 설치가 필요하지 않습니다.")
        return 0

    path_codes = [args.source, *(package.to_code for package in install_path)]
    if len(install_path) > 1:
        print("직접 언어쌍이 없어 중간 언어 경로를 설치합니다: " + " → ".join(path_codes))

    for package in install_path:
        print(f"{package.from_code} → {package.to_code} 모델을 내려받아 설치합니다. (무료 오픈소스 모델)")
        try:
            download_path = package.download()
            argostranslate.package.install_from_path(download_path)
        except Exception as error:
            print(f"{package.from_code} → {package.to_code} 모델 설치에 실패했습니다: {error}", file=sys.stderr)
            print("인터넷 연결을 확인한 뒤 같은 명령을 다시 실행하세요.", file=sys.stderr)
            return 1
    print("설치가 완료됐습니다. 이제 로컬 서버를 시작하거나 다시 번역해 보세요.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
