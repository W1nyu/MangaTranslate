"""Build allowlisted public archives; never copy a checkout or a local environment."""
from pathlib import Path
import hashlib
import json
import re
import zipfile

ROOT = Path(__file__).resolve().parents[1]


def release_files():
    extension = ["manifest.json", "background.js", "content.js", "content.css",
                 "popup.html", "popup.js", "popup.css", "options.html", "options.js",
                 "options.css", "theme.css", "shared/defaults.js", "shared/glossary.js",
                 "shared/regions.js"]
    server = ["requirements.txt", ".env.example", "app/__init__.py", "app/main.py",
              "app/bubble.py", "app/glossary.py", "app/models.py", "app/ocr.py",
              "app/paid_usage.py", "app/runtime.py", "app/translation.py",
              "scripts/bootstrap.ps1", "scripts/doctor.py", "scripts/install_models.py",
              "scripts/run_server.py", "scripts/smoke_test.py"]
    return extension, server


def validate(files):
    # Compare actual local credential values without printing or packaging them.
    secrets = []
    env = ROOT / "local-server/.env"
    if env.exists():
        for line in env.read_text(encoding="utf-8-sig").splitlines():
            key, sep, value = line.partition("=")
            if sep and re.search(r"KEY|TOKEN|SECRET|PASSWORD", key, re.I):
                value = value.strip().strip('\"\'')
                if len(value) >= 8:
                    secrets.append(value.encode())
    for name, data in files.items():
        if any(secret in data for secret in secrets):
            raise ValueError(f"Credential detected in {name}; release blocked")
        if re.search(rb"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----", data):
            raise ValueError(f"Private key detected in {name}; release blocked")
        if name.endswith(".env.example"):
            for line in data.decode("utf-8-sig").splitlines():
                key, sep, value = line.partition("=")
                if sep and not key.startswith("#") and re.search(r"KEY|TOKEN|SECRET|PASSWORD", key, re.I) and value.strip():
                    raise ValueError("Example environment must contain empty credentials")


def build():
    version = json.loads((ROOT / "extension/manifest.json").read_text(encoding="utf-8"))["version"]
    extension, server = release_files()
    ext = {p: (ROOT / "extension" / p).read_bytes() for p in extension}
    bundle = {f"extension/{p}": data for p, data in ext.items()}
    bundle.update({f"local-server/{p}": (ROOT / "local-server" / p).read_bytes() for p in server})
    bundle.update({p: (ROOT / "distribution" / p).read_bytes() for p in ["INSTALL.md", "PRIVACY.md", "start-server.cmd"]})
    validate(bundle)
    output = ROOT / "dist"
    output.mkdir(exist_ok=True)
    checksums = []
    for suffix, files in [("extension", ext), ("windows", bundle)]:
        path = output / f"manga-translate-{version}-{suffix}.zip"
        with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
            for name, data in sorted(files.items()):
                archive.writestr(name, data)
        with zipfile.ZipFile(path) as archive:
            assert archive.testzip() is None
            assert set(archive.namelist()) == set(files)
            validate({name: archive.read(name) for name in archive.namelist()})
        checksums.append(f"{hashlib.sha256(path.read_bytes()).hexdigest()}  {path.name}")
        print(f"Verified {path.name}: {len(files)} allowlisted files")
    # LF only: with CRLF, `sha256sum -c` reads the "\r" as part of each file name.
    (output / "SHA256SUMS.txt").write_text("\n".join(checksums) + "\n", encoding="utf-8", newline="\n")


if __name__ == "__main__":
    build()
