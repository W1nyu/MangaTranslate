"""Verify that an installed Argos pair can translate without a network request."""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.translation import MissingLanguagePair, TranslationRuntimeError, TranslationService


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Smoke-test an installed local Argos language pair.")
    parser.add_argument("--from", dest="source", required=True, help="Source ISO language code")
    parser.add_argument("--to", dest="target", required=True, help="Target ISO language code")
    parser.add_argument("--text", default="Hello, world!", help="Short text to translate")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    service = TranslationService()
    try:
        service.ensure_language_pair(args.source, args.target)
        translated = service.translate(args.text, args.source, args.target, [])
    except (MissingLanguagePair, TranslationRuntimeError) as error:
        print(f"실패: {error}", file=sys.stderr)
        return 1
    print(f"{args.source} → {args.target}: {translated}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
