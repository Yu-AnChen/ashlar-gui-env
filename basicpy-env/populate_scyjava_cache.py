#!/usr/bin/env python3
"""Fetch the self-contained Bio-Formats uber-jar used for reading images.

Run once after creating the pixi environment:
    pixi run setup-basicpy

We deliberately do NOT rely on bioio_bioformats' default scyjava/jgo/maven
resolution: it pins the thin `ome:formats-gpl` jar and resolves ~30 scattered
transitive deps (kryo, turbojpeg, woolz, ...), several of which live in
non-default maven repos. That resolution is fragile and leaves partially-built
caches that fail at runtime with NoClassDefFoundError.

Instead we download the official `bioformats_package.jar` — a fat jar with every
dependency bundled — into ./jars/ (git-ignored) and put it on the JVM classpath
at runtime (see ensure_bioformats() in main.py). Network is only needed here,
at setup time. The jar is pinned by version + SHA-256 for reproducibility.
"""

import hashlib
import sys
import urllib.request
from pathlib import Path

# Bio-Formats 8.5.0 (2026-03). zstd compression supported since 6.8.0; runs on
# Java 8-21 (scyjava/cjdk provides Zulu 11 at runtime). Bump VERSION + SHA256
# together to upgrade.
VERSION = "8.5.0"
SHA256 = "c6e60665d53a334b66e4d635340151f403dfe57a64704c573dd4c03b873befb9"
URL = (
    f"https://downloads.openmicroscopy.org/bio-formats/{VERSION}/artifacts/"
    "bioformats_package.jar"
)

JAR_PATH = Path(__file__).resolve().parent / "jars" / "bioformats_package.jar"


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def main() -> None:
    if JAR_PATH.exists() and _sha256(JAR_PATH) == SHA256:
        print(f"Bio-Formats {VERSION} already present: {JAR_PATH}")
        return

    JAR_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = JAR_PATH.with_suffix(".jar.part")
    print(f"Downloading Bio-Formats {VERSION} from {URL}")
    urllib.request.urlretrieve(URL, tmp)

    got = _sha256(tmp)
    if got != SHA256:
        tmp.unlink(missing_ok=True)
        sys.exit(
            f"SHA-256 mismatch for downloaded jar:\n  expected {SHA256}\n  got      {got}"
        )
    tmp.replace(JAR_PATH)
    print(f"Bio-Formats {VERSION} ready: {JAR_PATH} ({JAR_PATH.stat().st_size} bytes)")


if __name__ == "__main__":
    main()
