"""Command-line interface for run-ashlar.

Subcommands (all thin wrappers over run_ashlar.core):
    stitch       batch-run ashlar from a CSV config
    samplesheet  generate an mcmicro samplesheet from a batch folder
    channels     apply / extract channel names, or print an OMERO string
    compress     compress pysed OME-TIFF(s)

With no subcommand (or --gui) the graphical interface is launched.
"""

import argparse
import csv
import logging
import sys
import threading
from pathlib import Path

from . import core


# ── argument parser ──────────────────────────────────────────────────────────────


def build_parser():
    p = argparse.ArgumentParser(
        prog="run-ashlar",
        description="Batch ashlar stitching with a tk GUI and CLI. "
        "Run with no subcommand (or --gui) to launch the GUI.",
    )
    p.add_argument("--gui", action="store_true", help="Launch the graphical interface")
    sub = p.add_subparsers(dest="command")

    # ── stitch ──────────────────────────────────────────────────────────────────
    sp = sub.add_parser(
        "stitch",
        help="batch-run ashlar from a CSV config",
        description=(
            "Batch-run ashlar from a CSV config. Use --input-format to select "
            "directory format (columns: Directory, Correction) or mcmicro "
            "samplesheet format (columns: sample, cycle_number, image_tiles, Correction)."
        ),
    )
    sp.add_argument("csv_filepath", metavar="CSVFILE", help="CSV config file")
    sp.add_argument(
        "--markers", metavar="MARKERS",
        help="markers file applied to every slide (overrides per-sample extraction)",
    )
    sp.add_argument(
        "--output-dir", metavar="DIR",
        help="directory for output OME-TIFFs (default: next to each slide folder)",
    )
    sp.add_argument(
        "--max-n-jobs", type=int, default=2, metavar="N",
        help="Parallel slides X (default: 2, capped at 4; X*--n-jobs bounded by CPU)",
    )
    sp.add_argument(
        "--n-jobs", type=int, default=5, metavar="N",
        help="Orion assembly jobs per slide (default: 5)",
    )
    sp.add_argument(
        "--maximum-shift", type=int, default=30, metavar="SHIFT",
        help="Maximum per-tile corrective shift in microns (default: 30)",
    )
    sp.add_argument(
        "--filter-sigma", type=float, default=1, metavar="SIGMA",
        help="Gaussian pre-filter sigma in pixels (default: 1)",
    )
    sp.add_argument(
        "-c", "--align-channel", type=int, default=0, metavar="CH",
        help="Channel index to align on (default: 0)",
    )
    sp.add_argument(
        "--output-channels", type=int, nargs="+", default=None, metavar="CH",
        help="Subset of channel indices to write (default: all)",
    )
    sp.add_argument(
        "--stitch-alpha", type=float, default=0.01, metavar="A",
        help="Stitching alpha parameter (default: 0.01)",
    )
    sp.add_argument(
        "--maximum-error", type=float, default=None, metavar="E",
        help="Maximum alignment error tolerance (default: orion's auto)",
    )
    sp.add_argument(
        "--temp-dir", default=None, metavar="DIR",
        help="Scratch dir for the intermediate zarr (default: $ASHLAR_TMPDIR or output dir)",
    )
    sp.add_argument(
        "--no-mask-background", action="store_true",
        help="Do not automatically mask out the background region",
    )
    sp.add_argument(
        "--only-qc", action="store_true",
        help="Run alignment and write QC plots/pickles only; skip mosaic generation",
    )
    sp.add_argument("--flip-x", action="store_true", help="Flip tile positions left-to-right")
    sp.add_argument("--flip-y", action="store_true", help="Flip tile positions top-to-bottom")
    sp.add_argument("--flip-mosaic-x", action="store_true", help="Flip output image left-to-right")
    sp.add_argument("--flip-mosaic-y", action="store_true", help="Flip output image top-to-bottom")
    sp.add_argument(
        "--skip-existing", action="store_true",
        help="Skip slides whose output OME-TIFF already exists",
    )
    sp.add_argument(
        "--dry-run", action="store_true", help="Print commands without executing them"
    )
    sp.add_argument(
        "--file-type", choices=["rcpnl", "xdce", "pysed.ome.tif"], default=None,
        metavar="TYPE",
        help="Cycle file type for directory format (default: auto-detect rcpnl then xdce). "
        "pysed.ome.tif inputs are y-flipped automatically by rcashlar-orion.",
    )
    sp.add_argument(
        "--input-format", choices=["directory", "mcmicro"], default="directory",
        metavar="FORMAT", help="Input format (default: directory)",
    )
    sp.add_argument(
        "--no-order-check", action="store_true",
        help="Skip mtime-based cycle order check for mcmicro format",
    )
    sp.add_argument(
        "--no-extract-markers", action="store_true",
        help="Do not auto-extract channel names from pysed inputs",
    )
    sp.add_argument("-v", "--verbose", action="store_true")
    sp.set_defaults(func=cmd_stitch)

    # ── samplesheet ───────────────────────────────────────────────────────────────
    ss = sub.add_parser(
        "samplesheet",
        help="generate an mcmicro samplesheet from a batch folder",
        description="Scan a batch folder for cycle files, group by LSP sample ID, "
        "and write an mcmicro samplesheet CSV.",
    )
    ss.add_argument("batch_dir", metavar="BATCH_DIR", help="Batch folder (searched recursively)")
    ss.add_argument(
        "--output-dir", metavar="DIR",
        help="Directory for samplesheet.csv (default: batch_dir)",
    )
    ss.add_argument(
        "--file-type", choices=["rcpnl", "pysed.ome.tif"], default="rcpnl",
        metavar="TYPE", help="Cycle file type to search for (default: rcpnl)",
    )
    ss.set_defaults(func=cmd_samplesheet)

    # ── channels ──────────────────────────────────────────────────────────────────
    ch = sub.add_parser("channels", help="channel-name operations")
    chsub = ch.add_subparsers(dest="channels_command", required=True)

    a = chsub.add_parser("apply", help="write channel names into OME-TIFF(s)")
    a.add_argument("tiffs", nargs="*", metavar="TIFF", help="OME-TIFF file(s)")
    a.add_argument("--tiff-dir", metavar="DIR", help="apply to every *.ome.tif in DIR")
    a.add_argument(
        "--markers", metavar="MARKERS",
        help="markers file; with --tiff-dir and omitted, auto-pair <sample>-markers.csv",
    )
    a.set_defaults(func=cmd_channels_apply)

    e = chsub.add_parser("extract", help="extract channel names to a markers CSV")
    e.add_argument(
        "files", nargs="+", metavar="FILE",
        help="cycle file(s) in order (.pysed.ome.tif/.tif/.xml)",
    )
    e.add_argument("-o", "--output", required=True, metavar="CSV", help="output markers CSV")
    e.set_defaults(func=cmd_channels_extract)

    o = chsub.add_parser("omero", help="print channel names as an OMERO comma string")
    o.add_argument("source", metavar="SOURCE", help="a markers file or an OME-TIFF")
    o.set_defaults(func=cmd_channels_omero)

    # ── compress ──────────────────────────────────────────────────────────────────
    cp = sub.add_parser("compress", help="compress pysed OME-TIFF(s)")
    cp.add_argument(
        "inputs", nargs="+", metavar="INPUT",
        help="pysed .ome.tif file(s) or folder(s) (searched recursively)",
    )
    cp.add_argument(
        "-o", "--output-dir", metavar="DIR",
        help="output directory (required unless --in-place)",
    )
    cp.add_argument(
        "--in-place", action="store_true",
        help="overwrite inputs instead of writing to --output-dir",
    )
    cp.set_defaults(func=cmd_compress)

    return p


# ── handlers ─────────────────────────────────────────────────────────────────────


def cmd_stitch(args, parser):
    csv_path = Path(args.csv_filepath)
    if not csv_path.is_file() or csv_path.suffix != ".csv":
        parser.error("csv_filepath must be an existing .csv file")

    if args.input_format == "mcmicro":
        if not args.output_dir:
            parser.error("--output-dir is required when using --input-format mcmicro")
        slides = core._parse_mcmicro_sheet(csv_path)
        if not args.no_order_check:
            mismatches = core._find_cycle_order_mismatches(slides)
            if mismatches:
                print("Warning: cycle order in samplesheet differs from file modification time:")
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

    markers_names = core.read_markers(args.markers) if args.markers else None

    cancel_event = threading.Event()
    orion = core.OrionOptions(
        maximum_shift=args.maximum_shift,
        filter_sigma=args.filter_sigma,
        align_channel=args.align_channel,
        output_channels=args.output_channels,
        stitch_alpha=args.stitch_alpha,
        max_error=args.maximum_error,
        n_jobs=args.n_jobs,
        temp_dir=args.temp_dir,
        no_mask_background=args.no_mask_background,
        only_qc=args.only_qc,
        flip_x=args.flip_x,
        flip_y=args.flip_y,
        flip_mosaic_x=args.flip_mosaic_x,
        flip_mosaic_y=args.flip_mosaic_y,
    )
    try:
        core.run_batch(
            slides,
            max_n_jobs=args.max_n_jobs,
            cancel_event=cancel_event,
            orion=orion,
            markers_names=markers_names,
            extract_pysed_markers=not args.no_extract_markers,
            dry_run=args.dry_run,
            skip_existing=args.skip_existing,
            output_dir=args.output_dir,
            file_type=args.file_type,
        )
    except KeyboardInterrupt:
        cancel_event.set()
        logging.info("\nInterrupted.")
        sys.exit(1)


def cmd_samplesheet(args, parser):
    batch_dir = Path(args.batch_dir)
    if not batch_dir.is_dir():
        parser.error(f"Not a directory: {batch_dir}")
    output_dir = Path(args.output_dir) if args.output_dir else batch_dir

    out_path, samples, skipped = core.make_samplesheet(
        batch_dir, output_dir, args.file_type
    )
    logging.info(f"Samplesheet written to: {out_path}")
    logging.info(f"Samples detected: {len(samples)}")
    for sample_id in sorted(samples):
        logging.info(f"  {sample_id}: {len(samples[sample_id])} scan(s)")
    if skipped:
        logging.warning(f"Skipped {len(skipped)} file(s) with no recognized sample ID:")
        for p in skipped:
            logging.warning(f"  {p}")


def cmd_channels_apply(args, parser):
    if args.tiff_dir:
        tiff_dir = Path(args.tiff_dir)
        if not tiff_dir.is_dir():
            parser.error(f"Not a directory: {tiff_dir}")
        tiffs = sorted(tiff_dir.glob("*.ome.tif"))
        if not tiffs:
            parser.error(f"No .ome.tif files in {tiff_dir}")
    elif args.tiffs:
        tiffs = [Path(t) for t in args.tiffs]
    else:
        parser.error("provide TIFF file(s) or --tiff-dir")

    n_ok = 0
    for tif in tiffs:
        # per-sample auto-pairing when no explicit markers file is given
        if args.markers:
            markers_path = Path(args.markers)
        else:
            markers_path = tif.with_name(tif.name.replace(".ome.tif", "-markers.csv"))
            if not markers_path.exists():
                logging.warning(f"{tif.name}: no markers file ({markers_path.name}); skipping")
                continue
        logging.info(f"{tif.name}: applying {markers_path.name}")
        try:
            n = core.apply_markers_to_tiff(tif, markers_path)
            logging.info(f"  wrote {n} channel name(s)")
            n_ok += 1
        except Exception as e:
            logging.error(f"  failed: {e}")
    logging.info(f"Finished: {n_ok}/{len(tiffs)} file(s) updated")


def cmd_channels_extract(args, parser):
    files = [Path(f) for f in args.files]
    names, cycle_numbers = core.extract_markers(files)
    core.write_markers(args.output, names, cycle_numbers)
    logging.info(f"Wrote {len(names)} channel name(s) to {args.output}")
    for nm in names:
        if core.is_placeholder_name(nm):
            logging.warning(f"Channel name looks unset: {nm!r}")


def cmd_channels_omero(args, parser):
    source = Path(args.source)
    if source.suffix.lower() in (".tif", ".tiff"):
        names = core.read_channel_names(source)
    else:
        names = core.read_markers(source)
    print(core.names_to_omero_string(names))


def cmd_compress(args, parser):
    if not args.in_place and not args.output_dir:
        parser.error("--output-dir is required unless --in-place")
    results = {}
    for inp in args.inputs:
        inp = Path(inp)
        if inp.is_dir():
            files = core.find_pysed_files(inp)
            if not files:
                logging.warning(f"No .pysed.ome.tif files in {inp}")
                continue
            results.update(
                core.compress_pysed_batch(
                    files, args.output_dir, in_place=args.in_place, base_dir=inp
                )
            )
        elif inp.is_file():
            results.update(
                core.compress_pysed_batch(
                    [inp], args.output_dir, in_place=args.in_place
                )
            )
        else:
            logging.error(f"Not found: {inp}")


# ── entry point ──────────────────────────────────────────────────────────────────


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.gui or args.command is None:
        from . import gui

        gui.launch()
        return

    logging.basicConfig(
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%H:%M:%S",
        level=logging.INFO,
    )
    if getattr(args, "verbose", False):
        logging.getLogger().setLevel(logging.DEBUG)

    args.func(args, parser)


if __name__ == "__main__":
    main()
