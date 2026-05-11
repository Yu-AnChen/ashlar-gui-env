"""Batch-run ashlar from a CSV config file."""

import argparse
import csv
import logging
import platform
import queue
import re
import shlex
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

# ── helpers ───────────────────────────────────────────────────────────────────


def _add_channel_names(tiff_path, channel_names):
    import ome_types
    import tifffile

    ome = ome_types.from_tiff(tiff_path)
    n_channels = len(ome.images[0].pixels.channels)
    n_names = len(channel_names)
    assert n_channels == n_names, (
        f"Number of channels ({n_channels}) in '{tiff_path}' does not match "
        f"number of channel names ({n_names})."
    )
    for channel, name in zip(ome.images[0].pixels.channels, channel_names):
        channel.name = name
    tifffile.tiffcomment(tiff_path, ome.to_xml().encode())


def _text_to_bool(text):
    return bool(text) and str(text).lower() in ("1", "yes", "y", "true", "t")


def _load_markers(markers_path):
    """Return channel names from a headerless one-name-per-line markers CSV."""
    lines = Path(markers_path).read_text().splitlines()
    # strip blank lines from head and tail
    while lines and not lines[0].strip():
        lines.pop(0)
    while lines and not lines[-1].strip():
        lines.pop()
    return [ln.strip() for ln in lines]


def _resolve_shortcut(path):
    """Resolve a Windows .lnk shortcut to its target path."""
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


def _find_cycle_files(slide_dir, file_type=None):
    """Find cycle scan files within a slide directory.

    When file_type is given (e.g. 'pysed.ome.tif'), only that suffix is searched.
    Otherwise auto-detects: tries 'rcpnl' then 'xdce'.
    Searches directly in slide_dir, one level of real subdirectories, and any
    subdirectories reached via Windows .lnk shortcuts.
    """
    shortcut_dirs = []
    for lnk in slide_dir.glob("*.lnk"):
        resolved = _resolve_shortcut(lnk)
        if resolved.is_dir():
            shortcut_dirs.append(resolved)

    for ftype in ([file_type] if file_type else ("rcpnl", "xdce")):
        real = [*slide_dir.glob(f"*.{ftype}"), *slide_dir.glob(f"*/*.{ftype}")]
        lnks = [*slide_dir.glob(f"*.{ftype}.lnk"), *slide_dir.glob(f"*/*.{ftype}.lnk")]
        from_shortcut_dirs = [f for d in shortcut_dirs for f in d.glob(f"*.{ftype}")]
        files = sorted(
            {*real, *(_resolve_shortcut(p) for p in lnks), *from_shortcut_dirs},
            key=lambda p: p.name,
        )
        if files:
            files.sort(key=lambda p: p.stat().st_mtime)
            return files, ftype
    return [], file_type or "rcpnl"


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
            mismatches.append({
                "sample": slide["sample"],
                "specified": [p.name for p in files],
                "by_mtime": [p.name for p in by_mtime],
            })
    return mismatches


def _slide_key(slide):
    """Return a stable string identifier for a slide dict (both formats)."""
    return slide.get("sample") or slide.get("Directory", "unknown")


# ── samplesheet generation ────────────────────────────────────────────────────

LSP_PATTERN = re.compile(r"LSP\d+")


def _extract_sample_id(folder_name):
    """Return text before '@' in folder_name if it contains LSP\\d+, else None."""
    candidate = folder_name.split("@")[0]
    return candidate if LSP_PATTERN.search(candidate) else None


def _rolling_path(output_dir, stem="samplesheet", suffix=".csv"):
    """Return a non-existing path, incrementing samplesheet_1, _2, ... as needed."""
    p = output_dir / f"{stem}{suffix}"
    if not p.exists():
        return p
    i = 1
    while (output_dir / f"{stem}_{i}{suffix}").exists():
        i += 1
    return output_dir / f"{stem}_{i}{suffix}"


def make_samplesheet(batch_dir, output_dir, file_type="rcpnl"):
    """Scan batch_dir for cycle files, group by LSP sample ID, write a CSV samplesheet.

    file_type controls which suffix is searched (default: 'rcpnl'; also supports
    'pysed.ome.tif'). Returns (out_path, samples, skipped):
        out_path — Path to the written CSV
        samples  — dict mapping sample_id → [Path, ...] sorted by scan folder name
        skipped  — list of Paths with no recognized LSP sample ID
    """
    batch_dir = Path(batch_dir)
    cycle_files = sorted(batch_dir.rglob(f"*.{file_type}"), key=lambda p: p.parent.name)

    samples: dict = {}
    skipped: list = []
    for f in cycle_files:
        sample_id = _extract_sample_id(f.parent.name)
        if sample_id is None:
            skipped.append(f)
            continue
        samples.setdefault(sample_id, []).append(f)

    for sample_id in samples:
        samples[sample_id].sort(key=lambda p: p.parent.name)

    if not samples:
        raise ValueError(
            f"No .{file_type} files with a recognized LSP sample ID found in {batch_dir}. "
            f"Skipped {len(skipped)} file(s). "
            "Scan folder names must contain LSP followed by at least one digit."
        )

    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    out_path = _rolling_path(output_dir)

    with open(out_path, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["sample", "cycle_number", "filename", "image_tiles", "Correction"])
        for idx, sample_id in enumerate(sorted(samples)):
            if idx > 0:
                writer.writerow([])
            for cycle_num, cyc in enumerate(samples[sample_id], 1):
                writer.writerow([sample_id, cycle_num, cyc.name, str(cyc), "Yes"])

    return out_path, samples, skipped


_BASICPY_MANIFEST = Path(__file__).parent / "basicpy-env" / "pixi.toml"
_BASICPY_MAIN = Path(__file__).parent / "basicpy-env" / "main.py"


def _generate_ffp(cycle_files, illum_dir, file_type, dry_run=False):
    """Generate flat-field profiles using basicpy. Returns list of ffp paths."""
    illum_dir.mkdir(exist_ok=True)
    ffp_list = []
    for cycle_file in cycle_files:
        stem = cycle_file.name.replace(f".{file_type}", "")
        ffp_path = illum_dir / f"{stem}-ffp.ome.tif"
        if ffp_path.exists():
            logging.info(f"    FFP exists: {ffp_path.name}")
        else:
            logging.info(f"    Generating FFP: {ffp_path.name}")
            if not dry_run:
                # Forward-slash paths avoid pixi UNC path mangling on Windows
                cmd = [
                    "pixi", "run",
                    "--manifest-path", str(_BASICPY_MANIFEST),
                    "python", str(_BASICPY_MAIN),
                    "-i", str(cycle_file).replace("\\", "/"),
                    "-o", str(illum_dir).replace("\\", "/"),
                    "--output-flatfield", stem,
                    "--output-darkfield", stem,
                ]
                subprocess.run(cmd, shell=False, check=True)
        ffp_list.append(str(ffp_path))
    return ffp_list


# ── core processing ───────────────────────────────────────────────────────────


def _run_ashlar(cmd, log_path, slide_name, pipe_to_console=True, cancel_event=None):
    """Run ashlar via Popen, streaming output to log file and optionally the console."""
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
    dry_run=False,
    skip_existing=False,
    maximum_shift=30,
    filter_sigma=1,
    output_dir=None,
    file_type=None,
    pipe_ashlar_to_console=True,
    cancel_event=None,
):
    """Stitch one slide: find cycle files, run ashlar, optionally add channel names."""
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
    suffixes: set = set()
    for f in cycle_files:
        s = str(f).lower()
        if s.endswith(".pysed.ome.tif"):
            suffixes.add("pysed.ome.tif")
        elif s.endswith(".rcpnl"):
            suffixes.add("rcpnl")
        elif s.endswith(".xdce"):
            suffixes.add("xdce")
        else:
            suffixes.add("other")
    if len(suffixes) > 1:
        logging.warning(
            f"[{slide_name}] Mixed input file types: {', '.join(sorted(suffixes))}"
        )
    flip_y = suffixes == {"pysed.ome.tif"}
    display_type = detected_type or next(iter(suffixes), "unknown")

    logging.info(f"[{slide_name}] Found {len(cycle_files)} {display_type} cycle file(s)")

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
            ffp_list = _generate_ffp(cycle_files, illum_dir, detected_type, dry_run)

    # build ashlar command
    # pyramidal OME-TIFF output is automatic when -o ends in .ome.tif
    cmd = [
        "ashlar",
        *[str(f) for f in cycle_files],
        "-m",
        str(maximum_shift),
        "-o",
        str(out_tif),
    ]
    if filter_sigma is not None:
        cmd += ["--filter-sigma", str(filter_sigma)]
    if ffp_list:
        cmd += ["--ffp", *ffp_list]
    if flip_y:
        cmd += ["--flip-y"]

    logging.info(f"[{slide_name}] {shlex.join(cmd)}")

    if dry_run:
        logging.info(f"[{slide_name}] [dry-run] skipping execution")
        return True

    returncode = _run_ashlar(
        cmd, log_path, slide_name, pipe_ashlar_to_console, cancel_event=cancel_event
    )
    if returncode != 0:
        logging.error(
            f"[{slide_name}] ashlar failed (exit {returncode}) — see {log_path.name}"
        )
        return False

    # add channel names if markers provided
    if markers_names:
        if out_tif.exists():
            logging.info(f"[{slide_name}] Adding channel names")
            try:
                _add_channel_names(str(out_tif), markers_names)
            except Exception as e:
                logging.warning(f"[{slide_name}] Channel name error: {e}")
        else:
            logging.warning(
                f"[{slide_name}] Output not found; skipping channel names"
            )

    logging.info(f"[{slide_name}] Done → {out_tif}")
    return True


def run_batch(slides, *, max_n_jobs=1, cancel_event=None, **kwargs):
    """Run process_slide for each slide, in parallel when max_n_jobs > 1.

    Ashlar output is always written to per-slide log files. It is also piped
    to the console when running a single job; suppressed in parallel mode to
    avoid interleaved output from concurrent slides.
    """
    kwargs.setdefault("pipe_ashlar_to_console", max_n_jobs == 1)
    results = {}
    if max_n_jobs == 1:
        for slide in slides:
            if cancel_event and cancel_event.is_set():
                logging.info("Batch cancelled")
                break
            key = _slide_key(slide)
            try:
                results[key] = process_slide(
                    slide, cancel_event=cancel_event, **kwargs
                )
            except Exception as e:
                logging.error(f"[{key}] Unexpected error: {e}")
                results[key] = False
    else:
        with ThreadPoolExecutor(max_workers=max_n_jobs) as pool:
            futures = {
                pool.submit(
                    process_slide, slide, cancel_event=cancel_event, **kwargs
                ): slide
                for slide in slides
            }
            try:
                for fut in as_completed(futures):
                    slide = futures[fut]
                    key = _slide_key(slide)
                    try:
                        results[key] = fut.result()
                    except Exception as e:
                        logging.error(f"[{key}] Unexpected error: {e}")
                        results[key] = False
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


# ── CLI ───────────────────────────────────────────────────────────────────────


def _build_parser():
    p = argparse.ArgumentParser(
        description=(
            "Batch-run ashlar from a CSV config. "
            "Use --input-format to select between directory format "
            "(columns: Directory, Correction) and mcmicro samplesheet format "
            "(columns: sample, cycle_number, image_tiles, Correction)."
        )
    )
    p.add_argument(
        "csv_filepath",
        metavar="CSVFILE",
        nargs="?",
        help="CSV file with header: Directory, Correction",
    )
    p.add_argument(
        "-f",
        "--from-dir",
        type=int,
        default=0,
        metavar="FROM",
        help="Starting slide index (0-based, default 0)",
    )
    p.add_argument(
        "-t",
        "--to-dir",
        type=int,
        default=None,
        metavar="TO",
        help="Ending slide index, exclusive (default: all)",
    )
    p.add_argument(
        "--markers",
        metavar="MARKERS_CSV",
        help="headerless CSV with one channel name per line",
    )
    p.add_argument(
        "--output-dir",
        metavar="DIR",
        help="directory for output OME-TIFFs (default: next to each slide folder)",
    )
    p.add_argument(
        "--max-n-jobs",
        type=int,
        default=1,
        metavar="N",
        help="Max parallel ashlar jobs (default: 1)",
    )
    p.add_argument(
        "--maximum-shift",
        type=int,
        default=30,
        metavar="SHIFT",
        help="Maximum per-tile corrective shift in microns (default: 30)",
    )
    p.add_argument(
        "--filter-sigma",
        type=float,
        default=1,
        metavar="SIGMA",
        help="Gaussian pre-filter sigma in pixels (default: 1)",
    )
    p.add_argument(
        "--skip-existing",
        action="store_true",
        help="Skip slides whose output OME-TIFF already exists",
    )
    p.add_argument(
        "--dry-run", action="store_true", help="Print commands without executing them"
    )
    p.add_argument(
        "--file-type",
        choices=["rcpnl", "xdce", "pysed.ome.tif"],
        default=None,
        metavar="TYPE",
        help=(
            "Cycle file type for directory format: rcpnl, xdce, or pysed.ome.tif "
            "(default: auto-detect rcpnl then xdce). "
            "pysed.ome.tif automatically adds --flip-y to the ashlar call."
        ),
    )
    p.add_argument(
        "--input-format",
        choices=["directory", "mcmicro"],
        default="directory",
        metavar="FORMAT",
        help="Input format: 'directory' (default, columns: Directory, Correction) or 'mcmicro' samplesheet (columns: sample, cycle_number, image_tiles, Correction)",
    )
    p.add_argument(
        "--no-order-check",
        action="store_true",
        help="Skip mtime-based cycle order check for mcmicro format",
    )
    p.add_argument("--gui", action="store_true", help="Launch the graphical interface")
    p.add_argument("-v", "--verbose", action="store_true")
    return p


def cli_main(argv=None):
    parser = _build_parser()
    args = parser.parse_args(argv)

    if args.gui or args.csv_filepath is None:
        launch_gui()
        return

    logging.basicConfig(
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%H:%M:%S",
        level=logging.DEBUG if args.verbose else logging.INFO,
    )

    csv_path = Path(args.csv_filepath)
    if not csv_path.is_file() or csv_path.suffix != ".csv":
        parser.error("csv_filepath must be an existing .csv file")

    if args.input_format == "mcmicro":
        if not args.output_dir:
            parser.error("--output-dir is required when using --input-format mcmicro")
        slides = _parse_mcmicro_sheet(csv_path)
        if not args.no_order_check:
            mismatches = _find_cycle_order_mismatches(slides)
            if mismatches:
                print(
                    "Warning: cycle order in samplesheet differs from file modification time:"
                )
                for m in mismatches:
                    print(f"  {m['sample']}:")
                    print(f"    samplesheet: {m['specified']}")
                    print(f"    by mtime:    {m['by_mtime']}")
                ans = input("Proceed with samplesheet order? [y/N] ").strip().lower()
                if ans not in ("y", "yes"):
                    sys.exit("Aborted.")
    else:
        with open(csv_path, newline="") as f:
            slides = list(csv.DictReader(f))

    markers_names = _load_markers(args.markers) if args.markers else None

    cancel_event = threading.Event()
    try:
        run_batch(
            slides[args.from_dir : args.to_dir],
            max_n_jobs=args.max_n_jobs,
            cancel_event=cancel_event,
            markers_names=markers_names,
            dry_run=args.dry_run,
            skip_existing=args.skip_existing,
            maximum_shift=args.maximum_shift,
            filter_sigma=args.filter_sigma,
            output_dir=args.output_dir,
            file_type=args.file_type,
        )
    except KeyboardInterrupt:
        cancel_event.set()
        logging.info("\nInterrupted.")
        sys.exit(1)


# ── GUI ───────────────────────────────────────────────────────────────────────


def launch_gui():
    try:
        import tkinter as tk
        from tkinter import filedialog, messagebox, scrolledtext, ttk
    except ImportError:
        sys.exit("tkinter is not available; use command-line mode instead")

    root = tk.Tk()
    import tkinter.font as _tkfont
    _mono = "Cascadia Code" if "Cascadia Code" in _tkfont.families() else "Courier"
    root.title("run-ashlar")
    root.resizable(True, True)

    csv_var = tk.StringVar()
    fmt_var = tk.StringVar(value="directory")
    markers_var = tk.StringVar()
    output_dir_var = tk.StringVar()
    from_var = tk.IntVar(value=0)
    to_var = tk.StringVar(value="")
    jobs_var = tk.IntVar(value=1)
    margin_var = tk.IntVar(value=30)
    sigma_var = tk.DoubleVar(value=1.0)
    dry_var = tk.BooleanVar(value=False)
    skip_var = tk.BooleanVar(value=False)
    file_type_var = tk.StringVar(value="auto")

    frm = ttk.Frame(root, padding=12)
    frm.grid(sticky="nsew")
    root.columnconfigure(0, weight=1)
    root.rowconfigure(0, weight=1)
    frm.columnconfigure(1, weight=1)
    frm.rowconfigure(9, weight=1)

    def _file_row(row, label, var, filetypes):
        ttk.Label(frm, text=label).grid(row=row, column=0, sticky="w", pady=2)
        ttk.Entry(frm, textvariable=var, width=54).grid(
            row=row, column=1, padx=4, sticky="ew"
        )
        ttk.Button(
            frm,
            text="…",
            width=2,
            command=lambda: var.set(filedialog.askopenfilename(filetypes=filetypes)),
        ).grid(row=row, column=2)

    def _dir_row(row, label, var):
        ttk.Label(frm, text=label).grid(row=row, column=0, sticky="w", pady=2)
        ttk.Entry(frm, textvariable=var, width=54).grid(
            row=row, column=1, padx=4, sticky="ew"
        )
        ttk.Button(
            frm,
            text="…",
            width=2,
            command=lambda: var.set(filedialog.askdirectory()),
        ).grid(row=row, column=2)

    # ── samplesheet helper (collapsible) ─────────────────────────────────────
    _helper_open = [False]
    helper_batch_var = tk.StringVar()
    helper_output_var = tk.StringVar()

    def _toggle_helper():
        if _helper_open[0]:
            helper_frm.grid_remove()
            toggle_btn.configure(text="▶ Samplesheet helper")
        else:
            helper_frm.grid()
            toggle_btn.configure(text="▼ Samplesheet helper")
        _helper_open[0] = not _helper_open[0]

    toggle_btn = ttk.Button(frm, text="▶ Samplesheet helper", command=_toggle_helper)
    toggle_btn.grid(row=0, column=0, columnspan=3, sticky="w", pady=(0, 2))

    helper_frm = ttk.LabelFrame(frm, padding=4)
    helper_frm.columnconfigure(1, weight=1)
    helper_frm.grid(row=1, column=0, columnspan=3, sticky="ew")
    helper_frm.grid_remove()

    def _helper_dir_row(row, label, var):
        ttk.Label(helper_frm, text=label).grid(row=row, column=0, sticky="w", pady=2)
        ttk.Entry(helper_frm, textvariable=var, width=54).grid(
            row=row, column=1, padx=4, sticky="ew"
        )
        _var = var
        ttk.Button(
            helper_frm,
            text="…",
            width=2,
            command=lambda v=_var: v.set(filedialog.askdirectory()),
        ).grid(row=row, column=2)

    _helper_dir_row(0, "Batch folder", helper_batch_var)
    _helper_dir_row(1, "Output directory", helper_output_var)

    helper_ft_var = tk.StringVar(value="rcpnl")
    helper_ft_frm = ttk.Frame(helper_frm)
    helper_ft_frm.grid(row=2, column=0, columnspan=3, sticky="w", pady=2)
    ttk.Label(helper_ft_frm, text="File type").pack(side="left", padx=(0, 8))
    ttk.Radiobutton(
        helper_ft_frm, text="rcpnl", variable=helper_ft_var, value="rcpnl"
    ).pack(side="left", padx=(0, 6))
    ttk.Radiobutton(
        helper_ft_frm, text="pysed.ome.tif", variable=helper_ft_var, value="pysed.ome.tif"
    ).pack(side="left")

    helper_make_btn = ttk.Button(helper_frm, text="Make samplesheet")
    helper_make_btn.grid(row=3, column=0, columnspan=3, pady=(4, 2))

    def _on_make_samplesheet():
        batch_str = helper_batch_var.get().strip().strip('"')
        output_str = helper_output_var.get().strip().strip('"')
        if not batch_str or not output_str:
            messagebox.showerror(
                "Missing", "Both batch folder and output directory are required."
            )
            return
        batch_dir_p = Path(batch_str)
        if not batch_dir_p.is_dir():
            messagebox.showerror("Not found", f"Batch folder not found:\n{batch_dir_p}")
            return
        helper_make_btn.configure(state="disabled")

        def _helper_worker():
            try:
                out_path, samples, skipped = make_samplesheet(
                    batch_dir_p, output_str, file_type=helper_ft_var.get()
                )
                logging.info(f"Samplesheet written to: {out_path}")
                logging.info(f"Samples detected: {len(samples)}")
                for sample_id in sorted(samples):
                    logging.info(f"  {sample_id}: {len(samples[sample_id])} scan(s)")
                if skipped:
                    logging.warning(
                        f"Skipped {len(skipped)} file(s) with no recognized sample ID"
                    )
                root.after(
                    0,
                    lambda p=out_path, d=output_str: (
                        csv_var.set(str(p)),
                        fmt_var.set("mcmicro"),
                        output_dir_var.set(d),
                    ),
                )
            except Exception as e:
                logging.error(f"Make samplesheet failed: {e}")
            finally:
                root.after(0, lambda: helper_make_btn.configure(state="normal"))

        threading.Thread(target=_helper_worker, daemon=True).start()

    helper_make_btn.configure(command=_on_make_samplesheet)

    ttk.Separator(frm, orient="horizontal").grid(
        row=2, column=0, columnspan=3, sticky="ew", pady=6
    )

    # ── main section ─────────────────────────────────────────────────────────
    _file_row(3, "Config CSV *", csv_var, [("CSV", "*.csv"), ("All", "*.*")])

    fmt_frm = ttk.Frame(frm)
    fmt_frm.grid(row=4, column=0, columnspan=3, sticky="w", pady=2)
    ttk.Label(fmt_frm, text="Input format").pack(side="left", padx=(0, 8))
    ttk.Radiobutton(
        fmt_frm, text="Directory", variable=fmt_var, value="directory"
    ).pack(side="left", padx=(0, 6))
    ttk.Radiobutton(
        fmt_frm, text="mcmicro samplesheet", variable=fmt_var, value="mcmicro"
    ).pack(side="left")

    _file_row(5, "Markers CSV", markers_var, [("CSV", "*.csv"), ("All", "*.*")])
    _dir_row(6, "Output directory", output_dir_var)

    opts = ttk.Frame(frm)
    opts.grid(row=7, column=0, columnspan=3, sticky="w", pady=6)

    ttk.Label(opts, text="From slide").pack(side="left", padx=(0, 2))
    ttk.Spinbox(opts, textvariable=from_var, from_=0, to=9999, width=5).pack(
        side="left", padx=(0, 6)
    )
    ttk.Label(opts, text="To slide").pack(side="left", padx=(0, 2))
    ttk.Entry(opts, textvariable=to_var, width=5).pack(side="left", padx=(0, 16))

    for label, var, lo, hi, inc, w in [
        ("Max jobs", jobs_var, 1, 64, 1, 4),
        ("Max shift µm", margin_var, 0, 500, 5, 5),
        ("Filter σ", sigma_var, 0, 10, 0.5, 4),
    ]:
        ttk.Label(opts, text=label).pack(side="left", padx=(0, 2))
        ttk.Spinbox(
            opts, textvariable=var, from_=lo, to=hi, increment=inc, width=w
        ).pack(side="left", padx=(0, 10))

    ttk.Label(opts, text="File type").pack(side="left", padx=(16, 2))
    ttk.Combobox(
        opts,
        textvariable=file_type_var,
        values=["auto", "pysed.ome.tif"],
        state="readonly",
        width=13,
    ).pack(side="left", padx=(0, 10))
    ttk.Checkbutton(opts, text="Dry run", variable=dry_var).pack(
        side="left", padx=(0, 6)
    )
    ttk.Checkbutton(opts, text="Skip existing", variable=skip_var).pack(side="left")

    ttk.Separator(frm, orient="horizontal").grid(
        row=8, column=0, columnspan=3, sticky="ew", pady=6
    )

    log_text = scrolledtext.ScrolledText(
        frm, height=18, width=84, state="disabled", font=(_mono, 9)
    )
    log_text.grid(row=9, column=0, columnspan=3, sticky="nsew", pady=(0, 6))

    prog = ttk.Progressbar(frm, mode="indeterminate")
    prog.grid(row=10, column=0, columnspan=3, sticky="ew", pady=(0, 6))

    # shared state for the log viewer
    active_log_paths = []  # (log_path, out_tif_path) per slide in current batch
    batch_done = [True]  # flipped False→True around each batch run
    batch_start_time = [0.0]  # set to time.time() at batch start to ignore stale logs
    cancel_event = threading.Event()

    def _open_log_viewer():
        if not active_log_paths:
            messagebox.showinfo("No logs", "Run a batch first.")
            return

        win = tk.Toplevel(root)
        win.title("Ashlar log viewer")
        win.resizable(True, True)
        win.columnconfigure(0, weight=1)
        win.rowconfigure(0, weight=1)

        notebook = ttk.Notebook(win)
        notebook.grid(sticky="nsew", padx=8, pady=8)

        # ── Summary tab ──────────────────────────────────────────────────────
        sum_frame = ttk.Frame(notebook)
        sum_frame.columnconfigure(0, weight=1)
        sum_frame.rowconfigure(0, weight=1)
        sum_txt = scrolledtext.ScrolledText(
            sum_frame, height=20, width=60, font=(_mono, 9), state="disabled"
        )
        sum_txt.grid(sticky="nsew")
        notebook.add(sum_frame, text="Summary")

        # ── per-slide log tabs added dynamically ─────────────────────────────
        positions = {log: 0 for log, _ in active_log_paths}
        slide_tabs = {}  # log_path → ScrolledText, added on first file appearance

        def _add_slide_tab(log_path):
            slide_name = log_path.name.replace("-ashlar.log", "")
            frame = ttk.Frame(notebook)
            frame.columnconfigure(0, weight=1)
            frame.rowconfigure(0, weight=1)
            txt = scrolledtext.ScrolledText(
                frame, height=30, width=100, font=(_mono, 9), state="disabled"
            )
            txt.grid(sticky="nsew")
            notebook.add(frame, text=slide_name)
            slide_tabs[log_path] = txt
            return txt

        def _update_summary():
            lines = ["  {:<10}  {}".format("status", "slide"), "  " + "─" * 46]
            for log_path, out_tif in active_log_paths:
                slide_name = log_path.name.replace("-ashlar.log", "")
                log_current = (
                    log_path.exists()
                    and log_path.stat().st_mtime >= batch_start_time[0]
                )
                if out_tif.exists():
                    status = "done"
                elif log_current:
                    status = "failed" if batch_done[0] else "running"
                else:
                    status = "waiting" if not batch_done[0] else "---"
                lines.append(f"  {status:<10}  {slide_name}")
            content = "\n".join(lines) + "\n"
            sum_txt.configure(state="normal")
            sum_txt.delete("1.0", "end")
            sum_txt.insert("end", content)
            sum_txt.configure(state="disabled")

        def _poll_files():
            _update_summary()
            for log_path, _ in active_log_paths:
                if (
                    log_path.exists()
                    and log_path.stat().st_mtime >= batch_start_time[0]
                ):
                    if log_path not in slide_tabs:
                        _add_slide_tab(log_path)
                    try:
                        with open(log_path) as f:
                            f.seek(positions[log_path])
                            new = f.read()
                            positions[log_path] = f.tell()
                        if new:
                            txt = slide_tabs[log_path]
                            txt.configure(state="normal")
                            txt.insert("end", new)
                            txt.see("end")
                            txt.configure(state="disabled")
                    except Exception:
                        pass
            if win.winfo_exists():
                win.after(100, _poll_files)

        win.after(100, _poll_files)

    btn_bar = ttk.Frame(frm)
    btn_bar.grid(row=11, column=0, columnspan=3, pady=(0, 2))
    btn_run = ttk.Button(btn_bar, text="Run ashlar")
    btn_run.pack(side="left", padx=(0, 8))
    btn_cancel = ttk.Button(btn_bar, text="Cancel", state="disabled")
    btn_cancel.pack(side="left", padx=(0, 8))
    ttk.Button(
        btn_bar,
        text="Clear console",
        command=lambda: (
            log_text.configure(state="normal"),
            log_text.delete("1.0", "end"),
            log_text.configure(state="disabled"),
        ),
    ).pack(side="left", padx=(0, 8))
    ttk.Button(btn_bar, text="View logs", command=_open_log_viewer).pack(side="left")

    # redirect logging to the text widget via a queue
    log_queue: queue.Queue = queue.Queue()

    class _QueueHandler(logging.Handler):
        def emit(self, record):
            log_queue.put(self.format(record))

    _handler = _QueueHandler()
    _handler.setFormatter(
        logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", "%H:%M:%S")
    )
    root_logger = logging.getLogger()
    root_logger.addHandler(_handler)
    root_logger.setLevel(logging.INFO)

    def _poll_log():
        while True:
            try:
                msg = log_queue.get_nowait()
                log_text.configure(state="normal")
                log_text.insert("end", msg + "\n")
                log_text.see("end")
                log_text.configure(state="disabled")
            except queue.Empty:
                break
        root.after(120, _poll_log)

    root.after(120, _poll_log)

    def _on_run():
        csv_str = csv_var.get().strip().strip('"')
        if not csv_str:
            messagebox.showerror("Missing", "Please select a config CSV.")
            return
        csv_p = Path(csv_str)
        if not csv_p.is_file():
            messagebox.showerror("Not found", f"Config CSV not found:\n{csv_p}")
            return

        fmt = fmt_var.get()
        if fmt == "mcmicro":
            try:
                slides = _parse_mcmicro_sheet(csv_p)
            except Exception as e:
                messagebox.showerror("Parse error", str(e))
                return
            mismatches = _find_cycle_order_mismatches(slides)
            if mismatches:
                lines = [
                    "Cycle order in samplesheet differs from file modification time:\n"
                ]
                for m in mismatches:
                    lines.append(f"  {m['sample']}:")
                    lines.append(f"    samplesheet: {', '.join(m['specified'])}")
                    lines.append(f"    by mtime:    {', '.join(m['by_mtime'])}")
                lines.append("\nProceed with samplesheet order?")
                if not messagebox.askyesno(
                    "Cycle order mismatch", "\n".join(lines)
                ):
                    return
        else:
            with open(csv_p, newline="") as f:
                slides = list(csv.DictReader(f))

        to_raw = to_var.get().strip()
        to_idx = int(to_raw) if to_raw else None
        subset = slides[from_var.get() : to_idx]

        markers_names = None
        m_path = markers_var.get().strip().strip('"')
        if m_path:
            try:
                markers_names = _load_markers(m_path)
            except Exception as e:
                messagebox.showerror("Markers error", str(e))
                return

        output_dir = output_dir_var.get().strip().strip('"') or None
        if fmt == "mcmicro" and not output_dir:
            messagebox.showerror(
                "Missing",
                "Output directory is required when using mcmicro samplesheet format.",
            )
            return
        # sigma=0 in the spinbox means "no filtering"
        sigma = sigma_var.get() or None

        # precompute log + output paths so the viewer can open immediately
        active_log_paths.clear()
        try:
            for slide in subset:
                if "cycle_files" in slide:
                    slide_name = slide["sample"]
                    out_p = Path(output_dir).resolve() if output_dir else Path.cwd()
                else:
                    slide_dir = Path(slide["Directory"].strip().strip('"')).resolve()
                    slide_name = slide_dir.name
                    out_p = Path(output_dir).resolve() if output_dir else slide_dir.parent
                active_log_paths.append(
                    (
                        out_p / f"{slide_name}-ashlar.log",
                        out_p / f"{slide_name}.ome.tif",
                    )
                )
        except Exception as e:
            messagebox.showerror("Error", str(e))
            return

        log_text.configure(state="normal")
        log_text.delete("1.0", "end")
        log_text.configure(state="disabled")

        cancel_event.clear()
        batch_done[0] = False
        batch_start_time[0] = time.time()
        btn_run.configure(state="disabled")
        btn_cancel.configure(state="normal")
        prog.start(10)

        def _worker():
            try:
                ft = file_type_var.get()
                run_batch(
                    subset,
                    max_n_jobs=jobs_var.get(),
                    cancel_event=cancel_event,
                    markers_names=markers_names,
                    dry_run=dry_var.get(),
                    skip_existing=skip_var.get(),
                    maximum_shift=margin_var.get(),
                    filter_sigma=sigma,
                    output_dir=output_dir,
                    file_type=None if ft == "auto" else ft,
                )
            except Exception as e:
                logging.error(f"Batch error: {e}")
            finally:
                batch_done[0] = True
                root.after(
                    0,
                    lambda: (
                        btn_run.configure(state="normal"),
                        btn_cancel.configure(state="disabled"),
                        prog.stop(),
                    ),
                )

        threading.Thread(target=_worker, daemon=True).start()

    def _on_cancel():
        cancel_event.set()
        btn_cancel.configure(state="disabled")
        logging.info("Cancelling — waiting for running slides to finish…")

    btn_run.configure(command=_on_run)
    btn_cancel.configure(command=_on_cancel)
    root.mainloop()


# ── entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    cli_main()
