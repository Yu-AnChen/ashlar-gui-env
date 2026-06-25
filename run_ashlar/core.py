"""Non-GUI logic for run-ashlar: samplesheets, flat-field, stitching, markers, compress.

This module is the single source of truth shared by the GUI (run_ashlar.gui) and
the CLI (run_ashlar.cli). It must not import tkinter.
"""

import csv
import logging
import platform
import re
import shlex
import shutil
import struct
import subprocess
import tempfile
import time
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

# ── generic helpers ─────────────────────────────────────────────────────────────


def _text_to_bool(text):
    return bool(text) and str(text).lower() in ("1", "yes", "y", "true", "t")


def _unc(path):
    """Convert backslashes to forward slashes for reliable UNC handling on Windows."""
    return str(path).replace("\\", "/")


def _resolve_shortcut(path):
    """Resolve a Windows .lnk shortcut to its target; return path unchanged otherwise."""
    if platform.system() == "Windows" and str(path).endswith(".lnk"):
        import pythoncom
        import win32com.client

        pythoncom.CoInitialize()
        try:
            shell = win32com.client.Dispatch("WScript.Shell")
            target = shell.CreateShortCut(str(path)).Targetpath
            assert target != "", f"Shortcut has no target: {path}"
            return Path(target)
        finally:
            pythoncom.CoUninitialize()
    return Path(path)


def _rolling_path(output_dir, stem="samplesheet", suffix=".csv"):
    """Return a non-existing path, incrementing samplesheet_1, _2, ... as needed."""
    p = output_dir / f"{stem}{suffix}"
    if not p.exists():
        return p
    i = 1
    while (output_dir / f"{stem}_{i}{suffix}").exists():
        i += 1
    return output_dir / f"{stem}_{i}{suffix}"


# ── cycle-file discovery ─────────────────────────────────────────────────────────


def _find_cycle_files(slide_dir, file_type=None):
    """Find cycle scan files within a slide directory.

    When file_type is given (e.g. 'pysed.ome.tif'), only that suffix is searched.
    Otherwise auto-detects: tries 'rcpnl' then 'xdce'.
    Searches directly in slide_dir, one level of real subdirectories, and any
    subdirectories reached via Windows .lnk shortcuts. All .lnk files are
    resolved by target path, not by shortcut filename, so names like
    'cycle1.rcpnl - Shortcut.lnk' are handled correctly.
    """
    shortcut_dirs = []
    shortcut_files = []
    for lnk in [*slide_dir.glob("*.lnk"), *slide_dir.glob("*/*.lnk")]:
        resolved = _resolve_shortcut(lnk)
        if resolved.is_dir():
            shortcut_dirs.append(resolved)
        elif resolved.is_file():
            shortcut_files.append(resolved)

    for ftype in [file_type] if file_type else ("rcpnl", "xdce"):
        real = [*slide_dir.glob(f"*.{ftype}"), *slide_dir.glob(f"*/*.{ftype}")]
        from_shortcut_files = [
            f for f in shortcut_files if str(f).lower().endswith(f".{ftype}")
        ]
        from_shortcut_dirs = [f for d in shortcut_dirs for f in d.glob(f"*.{ftype}")]
        files = sorted(
            {*real, *from_shortcut_files, *from_shortcut_dirs},
            key=lambda p: (p.stat().st_mtime, p.name),
        )
        if files:
            return files, ftype
    return [], file_type or "rcpnl"


def _suffix_of(path):
    """Return the recognized cycle-file suffix for a path, or 'other'."""
    s = str(path).lower()
    if s.endswith(".pysed.ome.tif"):
        return "pysed.ome.tif"
    if s.endswith(".rcpnl"):
        return "rcpnl"
    if s.endswith(".xdce"):
        return "xdce"
    return "other"


# ── samplesheet (mcmicro) ────────────────────────────────────────────────────────


def _parse_mcmicro_sheet(csv_path):
    """Parse an mcmicro samplesheet (columns: sample, cycle_number, image_tiles).

    Rows within each sample are sorted by cycle_number. Windows .lnk shortcuts
    in image_tiles are resolved. An optional Correction column is respected.
    Returns a list of dicts compatible with process_slide:
        {'sample': str, 'cycle_files': [Path, ...], 'Correction': ''}
    """
    with open(csv_path, newline="") as f:
        rows = list(csv.DictReader(f))
    samples: dict = {}
    for row in rows:
        sample = row["sample"].strip()
        image = row["image_tiles"].strip().strip('"')
        if not sample or not image:
            continue
        cycle_num = int(row.get("cycle_number", 0) or 0)
        correction = row.get("Correction", "").strip()
        samples.setdefault(sample, {"rows": [], "correction": correction})
        samples[sample]["rows"].append((cycle_num, _resolve_shortcut(Path(image))))
    result = []
    for s, data in samples.items():
        sorted_files = [p for _, p in sorted(data["rows"], key=lambda x: x[0])]
        result.append(
            {"sample": s, "cycle_files": sorted_files, "Correction": data["correction"]}
        )
    return result


def _find_cycle_order_mismatches(slides):
    """Return entries for slides whose samplesheet cycle order differs from mtime order."""
    mismatches = []
    for slide in slides:
        files = slide["cycle_files"]
        try:
            by_mtime = sorted(files, key=lambda p: p.stat().st_mtime)
        except Exception:
            continue
        if [p.name for p in by_mtime] != [p.name for p in files]:
            mismatches.append(
                {
                    "sample": slide["sample"],
                    "specified": [p.name for p in files],
                    "by_mtime": [p.name for p in by_mtime],
                }
            )
    return mismatches


def _slide_key(slide):
    """Return a stable string identifier for a slide dict (both formats)."""
    return slide.get("sample") or slide.get("Directory", "unknown")


# ── samplesheet generation ───────────────────────────────────────────────────────

LSP_PATTERN = re.compile(r"LSP\d+")


def _extract_sample_id(name):
    """Return text before '@' and first '_' in name if it contains LSP\\d+, else None."""
    candidate = name.split("@")[0].split("_")[0]
    return candidate if LSP_PATTERN.search(candidate) else None


def make_samplesheet(batch_dir, output_dir, file_type="rcpnl"):
    """Scan batch_dir for cycle files, group by LSP sample ID, write a CSV samplesheet.

    file_type controls which suffix is searched (default: 'rcpnl'; also supports
    'pysed.ome.tif'). Files are grouped by the LSP sample ID found in each
    filename. Returns (out_path, samples, skipped):
        out_path — Path to the written CSV
        samples  — dict mapping sample_id → [Path, ...] sorted by filename
        skipped  — list of Paths with no recognized LSP sample ID
    """
    batch_dir = Path(batch_dir)
    cycle_files = sorted(batch_dir.rglob(f"*.{file_type}"), key=lambda p: p.name)

    samples: dict = {}
    skipped: list = []
    for f in cycle_files:
        sample_id = _extract_sample_id(f.name)
        if sample_id is None:
            skipped.append(f)
            continue
        samples.setdefault(sample_id, []).append(f)

    for sample_id in samples:
        samples[sample_id].sort(key=lambda p: p.name)

    if not samples:
        raise ValueError(
            f"No .{file_type} files with a recognized LSP sample ID found in {batch_dir}. "
            f"Skipped {len(skipped)} file(s). "
            "File names must contain LSP followed by at least one digit."
        )

    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    out_path = _rolling_path(output_dir)

    with open(out_path, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(
            ["sample", "cycle_number", "filename", "image_tiles", "Correction"]
        )
        for idx, sample_id in enumerate(sorted(samples)):
            if idx > 0:
                writer.writerow([])
            for cycle_num, cyc in enumerate(samples[sample_id], 1):
                writer.writerow([sample_id, cycle_num, cyc.name, str(cyc), "Yes"])

    return out_path, samples, skipped


# ── channel-name markers ─────────────────────────────────────────────────────────

PLACEHOLDER_RE = re.compile(r"^Channel\s*\d+$", re.IGNORECASE)


def is_placeholder_name(name):
    """True if a channel name looks like an unset OME placeholder (e.g. 'Channel 1')."""
    return bool(PLACEHOLDER_RE.match(str(name).strip()))


def _find_channels(root):
    """Return Channel elements from the first Pixels block of an OME-XML tree.

    Multi-FOV OME-XML has one Image/Pixels block per tile, each with an identical
    channel list — collecting from all series would give duplicates.
    """
    pixels = root.find(".//{*}Pixels")
    if pixels is None:
        pixels = root.find(".//Pixels")
    if pixels is not None:
        channels = pixels.findall("{*}Channel") or pixels.findall("Channel")
        return channels
    return root.findall(".//{*}Channel") or root.findall(".//Channel")


def read_channel_names(path):
    """Read channel names from one file's OME-XML (.ome.tif/.tif/.tiff or .xml)."""
    path = Path(path)
    if path.suffix.lower() == ".xml":
        xml_str = path.read_text()
    elif path.suffix.lower() in (".tif", ".tiff"):
        import tifffile

        with tifffile.TiffFile(path) as tif:
            xml_str = tif.pages[0].tags[270].value
    else:
        raise ValueError(f"Cannot read channel names from file type: {path}")
    root = ET.fromstring(xml_str)
    channels = _find_channels(root)
    return [ch.get("Name") or f"Channel {i + 1}" for i, ch in enumerate(channels)]


def extract_markers(cycle_files):
    """Extract channel names across ordered cycle files (pysed input).

    Returns (names, cycle_numbers): flat lists where cycle_numbers[i] is the
    1-based index of the cycle file that channel i came from. The order matches
    the channel order ashlar produces (inputs concatenated in the given order).
    """
    names: list = []
    cycle_numbers: list = []
    for cycle_num, f in enumerate(cycle_files, 1):
        ch_names = read_channel_names(f)
        names.extend(ch_names)
        cycle_numbers.extend([cycle_num] * len(ch_names))
    return names, cycle_numbers


def write_markers(path, names, cycle_numbers=None):
    """Write the canonical 3-column markers CSV: channel_number, cycle_number, marker_name."""
    if cycle_numbers is None:
        cycle_numbers = [1] * len(names)
    with open(path, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["channel_number", "cycle_number", "marker_name"])
        for i, (name, cyc) in enumerate(zip(names, cycle_numbers), 1):
            writer.writerow([i, cyc, name])


def parse_marker_text(text):
    """Liberally parse marker names from text. Auto-detects, no format flag.

    Accepts:
      1. canonical 3-column CSV with a header containing 'marker_name'
         (rows are ordered by channel_number when present)
      2. an OMERO-style single comma-joined line ('DAPI, CD3, CD8')
      3. a bare one-name-per-line list
    """
    lines = [ln.rstrip("\r") for ln in text.splitlines()]
    while lines and not lines[0].strip():
        lines.pop(0)
    while lines and not lines[-1].strip():
        lines.pop()
    if not lines:
        return []

    nonblank = [ln for ln in lines if ln.strip()]

    # 1. 3-column CSV with a recognizable header
    if "marker_name" in nonblank[0].lower() and "," in nonblank[0]:
        rows = list(csv.reader(nonblank))
        header = [h.strip().lower() for h in rows[0]]
        mi = header.index("marker_name")
        ci = header.index("channel_number") if "channel_number" in header else None
        body = [r for r in rows[1:] if any(c.strip() for c in r)]
        if ci is not None:
            body.sort(key=lambda r: int(r[ci]) if r[ci].strip().isdigit() else 0)
        return [r[mi].strip() for r in body if len(r) > mi]

    # 2. OMERO comma-joined single line
    if len(nonblank) == 1 and "," in nonblank[0]:
        return [s.strip() for s in nonblank[0].split(",") if s.strip()]

    # 3. one name per line
    return [ln.strip() for ln in nonblank]


def read_markers(path):
    """Read marker names from a file using the liberal parser (see parse_marker_text)."""
    return parse_marker_text(Path(path).read_text())


def names_to_omero_string(names):
    """Join channel names into an OMERO-style comma-separated string."""
    return ", ".join(names)


def add_channel_names(tiff_path, channel_names):
    """Write channel names into an OME-TIFF's OME-XML (no pixel data touched)."""
    import ome_types
    import tifffile

    ome = ome_types.from_tiff(tiff_path)
    channels = ome.images[0].pixels.channels
    if len(channels) != len(channel_names):
        raise ValueError(
            f"Number of channels ({len(channels)}) in '{tiff_path}' does not match "
            f"number of channel names ({len(channel_names)})."
        )
    for channel, name in zip(channels, channel_names):
        channel.name = name
    tifffile.tiffcomment(tiff_path, ome.to_xml().encode())


def apply_markers_to_tiff(tiff_path, markers_path):
    """Read a markers file and write its names into an OME-TIFF. Returns name count."""
    names = read_markers(markers_path)
    add_channel_names(str(tiff_path), names)
    return len(names)


# ── flat-field correction (basicpy in its own pixi env) ──────────────────────────

_BASICPY_MANIFEST = Path(__file__).resolve().parent.parent / "basicpy-env" / "pixi.toml"
_BASICPY_MAIN = Path(__file__).resolve().parent.parent / "basicpy-env" / "main.py"


def _generate_ffp(
    cycle_files, illum_dir, file_type, dry_run=False, cancel_event=None, progress=None
):
    """Generate flat-field profiles using basicpy (subprocess). Returns list of ffp paths.

    progress, if given, is called as progress('flat-field i/N') as each profile is
    processed, so a caller can surface the BaSiC phase in a status display.
    """
    Path(_unc(illum_dir)).mkdir(exist_ok=True, parents=True)
    ffp_list = []
    n = len(cycle_files)
    for i, cycle_file in enumerate(cycle_files, 1):
        if cancel_event and cancel_event.is_set():
            logging.info("    FFP generation cancelled")
            break
        if progress:
            progress(f"flat-field {i}/{n}")
        stem = cycle_file.name.replace(f".{file_type}", "")
        ffp_path = Path(_unc(illum_dir / f"{stem}-ffp.ome.tif"))
        if ffp_path.exists():
            logging.info(f"    FFP exists: {ffp_path.name}")
        else:
            logging.info(f"    Generating FFP: {ffp_path.name}")
            if not dry_run:
                cmd = [
                    "pixi",
                    "run",
                    "--manifest-path",
                    str(_BASICPY_MANIFEST),
                    "python",
                    str(_BASICPY_MAIN),
                    "-i",
                    _unc(cycle_file),
                    "-o",
                    _unc(illum_dir),
                    "--output-flatfield",
                    stem,
                    "--output-darkfield",
                    stem,
                ]
                proc = subprocess.Popen(cmd, shell=False)
                while proc.poll() is None:
                    if cancel_event and cancel_event.is_set():
                        proc.terminate()
                        logging.info("    FFP generation cancelled")
                        return ffp_list
                    time.sleep(0.5)
                if proc.returncode != 0:
                    raise subprocess.CalledProcessError(proc.returncode, cmd)
        ffp_list.append(_unc(ffp_path))
    return ffp_list


# ── ashlar ───────────────────────────────────────────────────────────────────────


def _run_ashlar(cmd, log_path, slide_name, pipe_to_console=True, cancel_event=None):
    """Run ashlar via Popen, streaming output to a log file and optionally the console."""
    try:
        version = subprocess.check_output(["ashlar", "--version"]).decode().strip()
    except Exception:
        version = "unknown"

    with open(log_path, "w") as log_f:
        log_f.write(
            f"ashlar version:\n{version}\n\nashlar command:\n{shlex.join(cmd)}\n\nashlar output:\n"
        )
        log_f.flush()

        proc = subprocess.Popen(
            cmd,
            shell=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
        for line in proc.stdout:
            log_f.write(line)
            log_f.flush()
            if pipe_to_console:
                line_s = line.rstrip()
                if line_s:
                    logging.info(f"[{slide_name}]   {line_s}")
            if cancel_event and cancel_event.is_set():
                proc.terminate()
                log_f.write("\n[cancelled]\n")
                log_f.flush()
                break
        proc.wait()

    return proc.returncode


def process_slide(
    slide,
    *,
    markers_names=None,
    extract_pysed_markers=True,
    dry_run=False,
    skip_existing=False,
    maximum_shift=30,
    filter_sigma=1,
    output_dir=None,
    file_type=None,
    pipe_ashlar_to_console=True,
    cancel_event=None,
    progress=None,
):
    """Stitch one slide: find cycle files, run ashlar, then write channel names.

    Channel-name precedence:
      1. an explicit markers list (markers_names) — applied to this slide
      2. for pysed input with extract_pysed_markers, names auto-extracted from the
         cycle files and written to <slide>-markers.csv *before* ashlar runs, then
         re-read from disk and applied *after* — so edits made during the stitch
         (its runtime is the review window) take effect.
      3. otherwise channel names are left untouched.
    """
    if cancel_event and cancel_event.is_set():
        return False

    if "cycle_files" in slide:
        # mcmicro samplesheet format — cycle files are pre-resolved
        slide_name = slide["sample"]
        cycle_files = slide["cycle_files"]
        detected_type = next(
            (
                ft
                for ft in ("pysed.ome.tif", "rcpnl", "xdce")
                if any(str(f).lower().endswith(ft) for f in cycle_files)
            ),
            "unknown",
        )
        out_parent = Path(output_dir).resolve() if output_dir else Path.cwd()
        slide_dir = None
    else:
        # directory format
        slide_dir = Path(slide["Directory"].strip().strip('"')).resolve()
        out_parent = Path(output_dir).resolve() if output_dir else slide_dir.parent
        slide_name = slide_dir.name
        cycle_files = None
        detected_type = None

    out_tif = out_parent / f"{slide_name}.ome.tif"
    log_path = out_parent / f"{slide_name}-ashlar.log"
    markers_path = out_parent / f"{slide_name}-markers.csv"

    if skip_existing and out_tif.exists():
        logging.info(f"[{slide_name}] Skipping — output already exists")
        return True

    if cycle_files is None:
        cycle_files, detected_type = _find_cycle_files(slide_dir, file_type)
        if not cycle_files:
            label = file_type or "rcpnl or xdce"
            logging.warning(f"[{slide_name}] No {label} files found")
            return False

    # check for mixed file types; add --flip-y when all inputs are pysed.ome.tif
    suffixes = {_suffix_of(f) for f in cycle_files}
    if len(suffixes) > 1:
        logging.warning(
            f"[{slide_name}] Mixed input file types: {', '.join(sorted(suffixes))}"
        )
    is_pysed = suffixes == {"pysed.ome.tif"}
    display_type = detected_type or next(iter(suffixes), "unknown")

    logging.info(
        f"[{slide_name}] Found {len(cycle_files)} {display_type} cycle file(s)"
    )

    # ── channel names: extract early for pysed when no explicit markers ──────────
    if markers_names is None and is_pysed and extract_pysed_markers and not dry_run:
        try:
            names, cycle_numbers = extract_markers(cycle_files)
            write_markers(markers_path, names, cycle_numbers)
            logging.info(
                f"[{slide_name}] Wrote {markers_path.name} ({len(names)} channels) — "
                "edit it before stitching finishes to override the names"
            )
            for nm in names:
                if is_placeholder_name(nm):
                    logging.warning(
                        f"[{slide_name}] Channel name looks unset: {nm!r}"
                    )
        except Exception as e:
            logging.warning(f"[{slide_name}] Could not extract channel names: {e}")

    # flat-field correction profiles
    ffp_list = None
    if _text_to_bool(slide.get("Correction", "")):
        # for mcmicro format slide_dir is None; use out_parent as the FFP base instead
        ffp_base = slide_dir if slide_dir is not None else out_parent
        illum_dir = ffp_base / "illumination_profiles"
        if detected_type not in ("rcpnl", "xdce", "pysed.ome.tif"):
            logging.warning(
                f"[{slide_name}] Correction skipped — cannot determine file type "
                f"(got '{detected_type}')"
            )
        else:
            ffp_list = _generate_ffp(
                cycle_files, illum_dir, detected_type, dry_run, cancel_event, progress
            )

    # build ashlar command (pyramidal OME-TIFF output is automatic for .ome.tif)
    cmd = [
        "ashlar",
        *[str(f) for f in cycle_files],
        "-m",
        str(maximum_shift),
        "-o",
        str(out_tif),
    ]
    if filter_sigma:  # 0 or None → ashlar's default (no filtering)
        cmd += ["--filter-sigma", str(filter_sigma)]
    if ffp_list:
        cmd += ["--ffp", *ffp_list]
    if is_pysed:
        cmd += ["--flip-y"]

    logging.info(f"[{slide_name}] {shlex.join(cmd)}")

    if dry_run:
        logging.info(f"[{slide_name}] [dry-run] skipping execution")
        return True

    if progress:
        progress("stitching")
    returncode = _run_ashlar(
        cmd, log_path, slide_name, pipe_ashlar_to_console, cancel_event=cancel_event
    )
    if returncode != 0:
        logging.error(
            f"[{slide_name}] ashlar failed (exit {returncode}) — see {log_path.name}"
        )
        return False

    # ── channel names: apply after stitch ───────────────────────────────────────
    # explicit markers win; otherwise re-read the markers CSV from disk so edits
    # made during the ashlar run are honored.
    names_to_apply = None
    source = None
    if markers_names is not None:
        names_to_apply, source = markers_names, "provided markers"
    elif markers_path.exists():
        try:
            names_to_apply, source = read_markers(markers_path), markers_path.name
        except Exception as e:
            logging.warning(f"[{slide_name}] Could not read {markers_path.name}: {e}")

    if names_to_apply:
        if out_tif.exists():
            logging.info(f"[{slide_name}] Writing channel names from {source}")
            try:
                add_channel_names(str(out_tif), names_to_apply)
            except Exception as e:
                logging.warning(f"[{slide_name}] Channel name error: {e}")
        else:
            logging.warning(f"[{slide_name}] Output not found; skipping channel names")

    logging.info(f"[{slide_name}] Done → {out_tif}")
    return True


def run_batch(slides, *, max_n_jobs=1, cancel_event=None, on_status=None, **kwargs):
    """Run process_slide for each slide, in parallel when max_n_jobs > 1.

    Ashlar output is always written to per-slide log files. It is also piped to
    the console when running a single job; suppressed in parallel mode to avoid
    interleaved output from concurrent slides.

    on_status, if given, is called as on_status(slide_key, status) where status
    is 'running' when a slide starts and 'done'/'failed'/'cancelled' when it
    finishes. It may be called from worker threads, so the callback must be
    thread-safe and must not touch GUI widgets directly.
    """
    kwargs.setdefault("pipe_ashlar_to_console", max_n_jobs == 1)

    def _run_one(slide):
        key = _slide_key(slide)
        report = (lambda s: on_status(key, s)) if on_status else None
        if report:
            report("running")
        try:
            ok = process_slide(
                slide, cancel_event=cancel_event, progress=report, **kwargs
            )
        except Exception as e:
            logging.error(f"[{key}] Unexpected error: {e}")
            ok = False
        if report:
            cancelled = bool(cancel_event and cancel_event.is_set())
            report("done" if ok else ("cancelled" if cancelled else "failed"))
        return key, ok

    results = {}
    if max_n_jobs == 1:
        for slide in slides:
            if cancel_event and cancel_event.is_set():
                logging.info("Batch cancelled")
                break
            key, ok = _run_one(slide)
            results[key] = ok
    else:
        with ThreadPoolExecutor(max_workers=max_n_jobs) as pool:
            futures = {pool.submit(_run_one, slide): slide for slide in slides}
            try:
                for fut in as_completed(futures):
                    key, ok = fut.result()
                    results[key] = ok
                    if cancel_event and cancel_event.is_set():
                        for f in futures:
                            f.cancel()
                        logging.info("Batch cancelled")
                        break
            except KeyboardInterrupt:
                if cancel_event:
                    cancel_event.set()
                for f in futures:
                    f.cancel()
                logging.info("Interrupted — cancelling remaining slides")

    n_ok = sum(v for v in results.values())
    n_total = len(results)
    logging.info(f"Finished: {n_ok}/{n_total} slide(s) succeeded")
    return results


# ── pysed compression ────────────────────────────────────────────────────────────


class _MessageFilter(logging.Filter):
    def __init__(self, *substrings):
        super().__init__()
        self.substrings = substrings

    def filter(self, record):
        return not any(s in record.getMessage() for s in self.substrings)


logging.getLogger("tifffile").addFilter(
    _MessageFilter("OME series contains invalid TiffData index")
)


def cluster_bigtiff_ifds(path):
    """Redirect a BigTIFF's IFD chain to IFDs appended at EOF (one-seek IFD reads).

    Modifies the file in-place; no pixel data is read or written:
    - Appends IFDs 1..N at EOF (StripOffsets already correct; only next-IFD patched)
    - Patches IFD0's next-IFD pointer to the first appended IFD

    Lets Bio-Formats (showinf -nopix) read the IFD chain with a single seek rather
    than seeking through all pixel data.

    Precondition: the file must be freshly written with interleaved IFDs (each IFD
    immediately precedes its pixel data, as tifffile writes them). Not idempotent —
    rerunning on an already-clustered file raises ValueError.
    """
    import tifffile

    path = Path(path)

    with tifffile.TiffFile(path) as tif:
        endian = tif.byteorder  # '<' (little) or '>' (big)
        ifd_offsets = [p.offset for p in tif.pages]
        data_offsets = [p.dataoffsets[0] for p in tif.pages]

    n = len(ifd_offsets)
    ifd_sizes = [data_offsets[i] - ifd_offsets[i] for i in range(n)]
    if any(s <= 0 for s in ifd_sizes):
        raise ValueError(
            "cluster_bigtiff_ifds expects freshly written, interleaved IFDs "
            "(each IFD precedes its pixel data); got a non-positive IFD size — "
            "the file may already be clustered"
        )

    # Load all raw IFD bytes into memory (total << 1 MB)
    with path.open("rb") as f:
        raw_ifds = []
        for i in range(n):
            f.seek(ifd_offsets[i])
            raw_ifds.append(bytearray(f.read(ifd_sizes[i])))

    def _set_next_ifd(ifd_ba, next_ifd):
        n_ent = struct.unpack_from(endian + "Q", ifd_ba, 0)[0]
        struct.pack_into(endian + "Q", ifd_ba, 8 + n_ent * 20, next_ifd)

    file_size = path.stat().st_size  # IFD1 will land here

    with path.open("r+b") as f:
        # Patch IFD0's next-IFD pointer to point to the appended IFD1
        _set_next_ifd(raw_ifds[0], file_size if n > 1 else 0)
        f.seek(ifd_offsets[0])
        f.write(bytes(raw_ifds[0]))

        # Append IFDs 1..N at EOF; compute next-IFD offset on the fly
        f.seek(0, 2)
        for i in range(1, n):
            next_off = f.tell() + ifd_sizes[i] if i < n - 1 else 0
            _set_next_ifd(raw_ifds[i], next_off)
            f.write(bytes(raw_ifds[i]))


def compress_pysed(in_path, out_path):
    """Write a compressed, IFD-clustered copy of a pysed OME-TIFF.

    The output keeps the same page/FOV structure and OME-XML, so it remains a
    drop-in ashlar input. Already-compressed inputs are hardlinked (copy fallback)
    rather than recompressed. When in_path == out_path the file is compressed
    in place via a temp file and atomic replace.
    """
    import tifffile

    in_path, out_path = Path(in_path), Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    same_file = out_path.exists() and out_path.samefile(in_path)

    with tifffile.TiffFile(in_path) as tif:
        already_compressed = tif.pages[0].compression != tifffile.COMPRESSION.NONE

    if already_compressed:
        if same_file:
            logging.info(f"Already compressed, in-place no-op: {in_path.name}")
            return out_path
        logging.info(f"Already compressed, linking: {in_path.name}")
        if out_path.exists():
            out_path.unlink()
        try:
            out_path.hardlink_to(in_path)
        except OSError:
            shutil.copy2(in_path, out_path)
        return out_path

    logging.info(f"Compressing: {in_path.name}")
    with tempfile.NamedTemporaryFile(
        delete=False, suffix=".tif", dir=out_path.parent
    ) as f:
        tmp_path = Path(f.name)
    try:
        # Step 1: write compressed with tifffile (IFDs interleaved — tifffile default)
        with tifffile.TiffFile(in_path) as src_tif:
            ome_xml = src_tif.ome_metadata.encode()
            n_pages = len(src_tif.pages)
            with tifffile.TiffWriter(tmp_path, bigtiff=True) as tif_w:
                for ii in range(n_pages):
                    tif_w.write(
                        src_tif.pages[ii].asarray(),
                        # zstd avoided: Bio-Formats v8.5.0 (airlift pure-Java zstd)
                        # fails with "Output buffer too small" when predictor=True
                        # because it misreads the content_size field and
                        # underestimates the output buffer from the (tiny) compressed
                        # size. zlib is immune — Bio-Formats sizes output from image
                        # dimensions.
                        compression="zlib",
                        predictor=True,
                        metadata=None,
                        description=ome_xml if ii == 0 else None,
                    )

        # Step 2: append IFDs at EOF and patch next-IFD pointers (no pixel I/O)
        t0 = time.perf_counter()
        cluster_bigtiff_ifds(tmp_path)
        logging.info(f"  Clustered IFDs in {time.perf_counter() - t0:.2f}s")
        tmp_path.replace(out_path)
    except Exception:
        tmp_path.unlink(missing_ok=True)
        raise
    return out_path


def find_pysed_files(root):
    """Return sorted .pysed.ome.tif paths under root (recursive)."""
    return sorted(Path(root).rglob("*.pysed.ome.tif"))


def compress_pysed_batch(
    inputs, output_dir=None, *, in_place=False, base_dir=None, cancel_event=None
):
    """Compress many pysed files. Returns {str(path): bool}.

    Non-destructive by default: each input is written under output_dir. When
    base_dir is given, the input's path relative to base_dir is mirrored under
    output_dir (avoids cross-folder name collisions). When in_place is True the
    inputs are overwritten and output_dir/base_dir are ignored.
    """
    results = {}
    for p in inputs:
        if cancel_event and cancel_event.is_set():
            logging.info("Compression cancelled")
            break
        p = Path(p)
        if in_place:
            out = p
        else:
            rel = p.relative_to(base_dir) if base_dir is not None else Path(p.name)
            out = Path(output_dir) / rel
        try:
            compress_pysed(p, out)
            results[str(p)] = True
        except Exception as e:
            logging.error(f"Compress failed {p.name}: {e}")
            results[str(p)] = False
    n_ok = sum(results.values())
    logging.info(f"Finished: {n_ok}/{len(results)} file(s) compressed")
    return results
