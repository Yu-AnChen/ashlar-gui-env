#!/usr/bin/env python3
"""Benchmark direct Bio-Formats read+downsize strategies for the BaSiC pipeline.

Both strategies produce, per channel, an ``(I, 128, 128)`` stack identical to what
``main.py``'s file branch feeds to BaSiC. We only time read+downsize here
(``basic.fit``/``autotune`` are identical across approaches), and assert the two
strategies are bit-identical so the comparison is apples-to-apples.

The slow ``bioio_bioformats`` baseline has been removed on purpose — it dominated
runtime and isn't needed to compare the candidate read orders.

    pixi run python basicpy-env/bench_reader.py <path-to-image>

Structural metadata (series/channel/Z counts, shape, dtype) always comes from
Bio-Formats. The pixel-read backend depends on the input:

  * TIFF input (.tif/.ome.tif): read pixels with tifffile.
        {tifffile-onepass, tifffile-chanwise}
  * non-TIFF input (e.g. .rcpnl): read pixels with Bio-Formats.
        {minimal-onepass, minimal-chanwise}

  onepass  - single FOV-major pass over all planes (storage order).
  chanwise - channel-major loop (each channel across all FOVs, then next).

Both read-order variants for a given input produce identical (I, 128, 128) stacks
(asserted), so onepass-vs-chanwise is an apples-to-apples access-order comparison.

There is no warm-up read: the first strategy in --order is the reference and runs
cold. Because reading a file primes the OS/SMB cache, later strategies look fast
unless --evict-cmd flushes the cache before each run. For a fair cold A/B either pass
--evict-cmd, or run twice with --order onepass,chanwise then chanwise,onepass and
compare each run's first (cold) row.
"""
import argparse
import subprocess
import sys
import time
from pathlib import Path

import cv2
import numpy as np

from main import ensure_bioformats

DOWNSIZE = (128, 128)


class Progress:
    """Throttled single-line progress for a plane-by-plane read."""

    def __init__(self, total: int, label: str, every: float = 0.5):
        self.total = total
        self.label = label
        self.every = every
        self.n = 0
        self.t0 = time.perf_counter()
        self.last = 0.0

    def step(self, k: int = 1) -> None:
        self.n += k
        now = time.perf_counter()
        if now - self.last >= self.every or self.n >= self.total:
            self.last = now
            elapsed = now - self.t0
            rate = self.n / elapsed if elapsed else 0.0
            sys.stderr.write(
                f"\r  {self.label:<18} {self.n:>5}/{self.total} planes "
                f"{elapsed:6.1f}s {rate:6.1f} planes/s"
            )
            sys.stderr.flush()

    def done(self) -> None:
        self.step(0)
        sys.stderr.write("\n")
        sys.stderr.flush()


def _downsize(plane: np.ndarray) -> np.ndarray:
    return cv2.resize(plane, dsize=DOWNSIZE, interpolation=cv2.INTER_AREA)


def quiet_loci(level: str = "ERROR") -> None:
    """Turn down Bio-Formats' very chatty logback output. Call after the JVM starts."""
    import jpype

    DebugTools = jpype.JPackage("loci").common.DebugTools
    try:
        DebugTools.setRootLevel(level)
    except Exception:  # older Bio-Formats fallback
        DebugTools.enableLogging(level)


# --- minimal direct Bio-Formats reader ---------------------------------------

def _pixtype2dtype(reader) -> np.dtype:
    """loci PixelType int -> numpy dtype, mirroring bioio utils._pixtype2dtype."""
    import jpype

    FT = jpype.JPackage("loci").formats.FormatTools
    fmt2type = {
        int(FT.INT8): "i1",
        int(FT.UINT8): "u1",
        int(FT.INT16): "i2",
        int(FT.UINT16): "u2",
        int(FT.INT32): "i4",
        int(FT.UINT32): "u4",
        int(FT.FLOAT): "f4",
        int(FT.DOUBLE): "f8",
    }
    endian = "<" if reader.isLittleEndian() else ">"
    return np.dtype(endian + fmt2type[int(reader.getPixelType())])


class BioformatsReader:
    """Thin wrapper over a Java loci.formats.ImageReader.

    rcpnl-style files: series == FOV, channels live within a series, Z == T == 1.
    """

    def __init__(self, path: str):
        import jpype

        self.r = jpype.JPackage("loci").formats.ImageReader()
        self.r.setId(str(path))
        self.n_series = int(self.r.getSeriesCount())
        self.size_c = int(self.r.getEffectiveSizeC())
        self.size_z = int(self.r.getSizeZ())
        self.size_t = int(self.r.getSizeT())
        self.height = int(self.r.getSizeY())
        self.width = int(self.r.getSizeX())
        self.dtype = _pixtype2dtype(self.r)

    def read_plane(self, series: int, c: int, z: int = 0, t: int = 0) -> np.ndarray:
        self.r.setSeries(series)
        idx = self.r.getIndex(z, c, t)
        buf = self.r.openBytes(idx)
        return np.frombuffer(bytes(buf), self.dtype).reshape(self.height, self.width)

    def close(self):
        self.r.close()


class SkipStrategy(Exception):
    """Raised by a strategy that can't handle the given input (e.g. non-TIFF)."""


class TifffileReader:
    """Read pixels via tifffile while taking structural metadata from Bio-Formats.

    Reads from tifffile's flat page list (all IFDs in file order) rather than its
    series grouping, which doesn't always line up with Bio-Formats' notion of series
    for OME-TIFF (channels-as-series, planes-per-series, etc.). Global page index is
    ``series * planes_per_series + local``, where `local` is Bio-Formats' own
    ``getIndex(z, c, t)`` so the within-series dimension order matches the file. All
    counts/shape/dtype come from `meta` (a BioformatsReader); only pixels use tifffile.
    """

    def __init__(self, path: str, meta: "BioformatsReader"):
        import tifffile

        try:
            self.tif = tifffile.TiffFile(str(path))
        except (tifffile.TiffFileError, ValueError) as e:
            raise SkipStrategy(f"not a TIFF: {e}")
        self.pages = self.tif.pages  # flat IFD list, file order
        self.n_series = meta.n_series
        self.size_c = meta.size_c
        self.size_z = meta.size_z
        self.size_t = meta.size_t
        self.height, self.width = meta.height, meta.width
        self.dtype = meta.dtype
        self._planes_per_series = self.size_c * self.size_z * self.size_t
        # Precompute the within-series plane order from Bio-Formats (no JNI in the
        # timed loop). Pipeline reads t=0 only.
        self._local = {
            (z, c): int(meta.r.getIndex(z, c, 0))
            for z in range(self.size_z)
            for c in range(self.size_c)
        }
        expected = self.n_series * self._planes_per_series
        if len(self.pages) < expected:
            raise SkipStrategy(
                f"tifffile sees {len(self.pages)} pages but Bio-Formats implies "
                f"{self.n_series}*{self._planes_per_series}={expected}; "
                "page layout not understood"
            )

    def read_plane(self, series: int, c: int, z: int = 0, t: int = 0) -> np.ndarray:
        idx = series * self._planes_per_series + self._local[(z, c)]
        return self.pages[idx].asarray()

    def close(self):
        self.tif.close()


# --- read-order strategies (generic over any reader) -------------------------

def _chanwise(reader, label: str) -> list[np.ndarray]:
    """Channel-major: read each channel across all FOVs before the next channel."""
    iters = [(s, z) for s in range(reader.n_series) for z in range(reader.size_z)]
    prog = Progress(reader.size_c * len(iters), label)
    channels = []
    try:
        for c in range(reader.size_c):
            planes = []
            for s, z in iters:
                planes.append(_downsize(reader.read_plane(s, c, z)))
                prog.step()
            channels.append(np.stack(planes))
    finally:
        reader.close()
        prog.done()
    return channels


def _onepass(reader, label: str) -> list[np.ndarray]:
    """Single FOV-major pass over `reader`; slice per channel afterward."""
    n_i = reader.n_series * reader.size_z
    prog = Progress(n_i * reader.size_c, label)
    buffers = [
        np.empty((n_i, *DOWNSIZE), dtype=np.float32) for _ in range(reader.size_c)
    ]
    try:
        i = 0
        for s in range(reader.n_series):
            for z in range(reader.size_z):
                for c in range(reader.size_c):
                    buffers[c][i] = _downsize(reader.read_plane(s, c, z))
                    prog.step()
                i += 1
    finally:
        reader.close()
        prog.done()
    return buffers


def _is_tiff(path: Path) -> bool:
    return path.suffix.lower() in {".tif", ".tiff"}


def _to_comparable(channels: list[np.ndarray]) -> np.ndarray:
    """Stack to (C, I, Y, X) float32 for cross-strategy equality checks."""
    return np.stack([np.asarray(c).astype(np.float32) for c in channels])


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("input", type=Path, help="image file to benchmark")
    ap.add_argument("--repeats", type=int, default=1, help="timed runs per strategy")
    ap.add_argument(
        "--order",
        default="onepass,chanwise",
        help="comma-separated read orders to run; the FIRST one is the reference and "
        "gets the cold-cache read (e.g. --order chanwise,onepass)",
    )
    ap.add_argument(
        "--evict-cmd",
        default=None,
        help="shell command run before EACH timed read to drop the OS/file cache, so "
        "every strategy starts cold (best effort). E.g. on Windows the Sysinternals "
        "tool: --evict-cmd \"RAMMap64.exe -Et\"; on Linux: "
        "\"sync && echo 3 | sudo tee /proc/sys/vm/drop_caches\"; on macOS: \"sudo purge\".",
    )
    args = ap.parse_args()

    ensure_bioformats()
    quiet_loci()

    path = str(args.input)
    # Structural metadata always comes from Bio-Formats. For TIFF inputs we read
    # pixels via tifffile; otherwise both metadata and pixels come from Bio-Formats.
    if _is_tiff(args.input):
        family = "tifffile"
        meta = BioformatsReader(path)

        def make_reader():
            return TifffileReader(path, meta)
    else:
        family = "minimal"
        meta = None

        def make_reader():
            return BioformatsReader(path)

    short2fn = {
        "onepass": _onepass,
        "chanwise": _chanwise,
    }
    order = [s.strip() for s in args.order.split(",") if s.strip()]
    bad = [s for s in order if s not in short2fn]
    if bad:
        ap.error(f"unknown --order entries {bad}; choose from {list(short2fn)}")

    def maybe_evict():
        if args.evict_cmd:
            subprocess.run(args.evict_cmd, shell=True, check=False)

    # No dedicated warm-up read (it would prime the cache and make every timed run
    # warm). The first strategy in --order is the reference; with --evict-cmd every
    # run starts cold, so onepass-vs-chanwise is a fair cold comparison.
    reference = None
    ref_cmp = None
    ref_name = None
    results = {}
    for short in order:
        name = f"{family}-{short}"
        fn = short2fn[short]
        times = []
        out = None
        try:
            for _ in range(args.repeats):
                maybe_evict()
                t0 = time.perf_counter()
                out = fn(make_reader(), name)
                times.append(time.perf_counter() - t0)
        except SkipStrategy as e:
            print(f"  {name:<18} skipped: {e}")
            continue
        results[name] = (min(times), np.median(times))

        cmp = _to_comparable(out)
        if reference is None:
            reference, ref_cmp, ref_name = out, cmp, name
            print(
                f"  reference={name}: channels={cmp.shape[0]} "
                f"planes/channel={cmp.shape[1]} tile={cmp.shape[2]}x{cmp.shape[3]} "
                f"dtype={out[0].dtype}"
            )
            continue
        if cmp.shape != ref_cmp.shape:
            raise AssertionError(f"{name}: shape {cmp.shape} != reference {ref_cmp.shape}")
        max_diff = float(np.max(np.abs(cmp - ref_cmp)))
        identical = np.array_equal(cmp, ref_cmp)
        status = "identical" if identical else f"max|diff|={max_diff:g}"
        print(f"  {name:<18} check: {status}")
        if not identical:
            raise AssertionError(f"{name} stacks differ from reference ({status})")

    if meta is not None:
        meta.close()

    print("\nread+downsize timing (lower is better):")
    print(f"  {'strategy':<18} {'min(s)':>10} {'median(s)':>12} {'rel':>8}")
    ref_min = results[ref_name][0]
    for name, (mn, med) in results.items():
        print(f"  {name:<18} {mn:>10.3f} {med:>12.3f} {ref_min / mn:>7.2f}x")

    if not args.evict_cmd:
        print(
            "\nNote: no --evict-cmd, so only the first strategy ran cold; later runs "
            "hit the OS/SMB cache and will look artificially fast. For a fair cold A/B, "
            "pass --evict-cmd, or run twice flipping --order and compare each first row."
        )


if __name__ == "__main__":
    main()
