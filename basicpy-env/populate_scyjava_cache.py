#!/usr/bin/env python3
"""Pre-warm the scyjava/Maven cache for BioFormats support.

Run once after setting up the basicpy pixi environment:
    pixi run setup-basicpy

The Java artifacts are downloaded to ~/.scyjava and reused on every subsequent
run, so this only incurs a network cost the first time.
"""

import os
import pathlib
import platform

import bioio_bioformats
import scyjava

base = pathlib.Path.home() / ".scyjava"
base.mkdir(exist_ok=True)
scyjava.config.set_cache_dir(base / ".jgo")
scyjava.config.set_m2_repo(base / ".m2" / "repository")

# Initialize the Reader to trigger download of BioFormats artifacts. We pass a
# path that exists to bypass an early existence check; the Reader will error
# because "/" isn't an image, but the artifacts will have been cached by then.
try:
    bioio_bioformats.Reader("/")
except Exception:
    pass

# Set open permissions so the cache is usable regardless of the uid that runs
# the tool. Skipped on Windows where Unix permission bits have no effect.
if platform.system() != "Windows":
    for root, dirs, files in os.walk(base):
        root = pathlib.Path(root)
        root.chmod(0o777)
        for dname in dirs:
            (root / dname).chmod(0o777)
        for fname in files:
            (root / fname).chmod(0o666)

print(f"BioFormats cache ready: {base}")
