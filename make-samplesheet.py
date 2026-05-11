#!/usr/bin/env python3
"""Generate an mcmicro samplesheet CSV from a batch folder of .rcpnl scans."""

import argparse
import csv
import logging
import queue
import re
import sys
import threading
from pathlib import Path

LSP_PATTERN = re.compile(r"LSP\d+")


def _extract_sample_id(folder_name):
    """Return text before '@' in folder_name if it contains LSP\\d{5,}, else None."""
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


# ── CLI ───────────────────────────────────────────────────────────────────────


def cli_main(argv=None):
    parser = argparse.ArgumentParser(
        description=(
            "Scan a batch folder for cycle files, group by LSP sample ID, "
            "and write an mcmicro samplesheet CSV "
            "(columns: sample, cycle_number, filename, image_tiles, Correction)."
        )
    )
    parser.add_argument(
        "batch_dir",
        nargs="?",
        metavar="BATCH_DIR",
        help="Batch folder containing scan folders (searched recursively for cycle files)",
    )
    parser.add_argument(
        "--output-dir",
        metavar="DIR",
        help="Directory for samplesheet.csv (default: batch_dir)",
    )
    parser.add_argument(
        "--file-type",
        choices=["rcpnl", "pysed.ome.tif"],
        default="rcpnl",
        metavar="TYPE",
        help="Cycle file type to search for: rcpnl (default) or pysed.ome.tif",
    )
    args = parser.parse_args(argv)

    if args.batch_dir is None:
        launch_gui()
        return

    logging.basicConfig(
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%H:%M:%S",
        level=logging.INFO,
    )

    batch_dir = Path(args.batch_dir)
    if not batch_dir.is_dir():
        parser.error(f"Not a directory: {batch_dir}")

    output_dir = Path(args.output_dir) if args.output_dir else batch_dir

    out_path, samples, skipped = make_samplesheet(batch_dir, output_dir, args.file_type)

    logging.info(f"Samplesheet written to: {out_path}")
    logging.info(f"Samples detected: {len(samples)}")
    for sample_id in sorted(samples):
        logging.info(f"  {sample_id}: {len(samples[sample_id])} scan(s)")
    if skipped:
        logging.warning(f"Skipped {len(skipped)} file(s) with no recognized sample ID:")
        for p in skipped:
            logging.warning(f"  {p}")


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
    root.title("Make samplesheet")
    root.resizable(True, True)

    batch_var = tk.StringVar()
    output_var = tk.StringVar()
    file_type_var = tk.StringVar(value="rcpnl")

    frm = ttk.Frame(root, padding=12)
    frm.grid(sticky="nsew")
    root.columnconfigure(0, weight=1)
    root.rowconfigure(0, weight=1)
    frm.columnconfigure(1, weight=1)
    frm.rowconfigure(4, weight=1)

    def _dir_row(row, label, var):
        ttk.Label(frm, text=label).grid(row=row, column=0, sticky="w", pady=2)
        ttk.Entry(frm, textvariable=var, width=54).grid(
            row=row, column=1, padx=4, sticky="ew"
        )
        ttk.Button(
            frm,
            text="…",
            width=2,
            command=lambda v=var: v.set(filedialog.askdirectory()),
        ).grid(row=row, column=2)

    _dir_row(0, "Batch folder *", batch_var)
    _dir_row(1, "Output directory *", output_var)

    ft_frm = ttk.Frame(frm)
    ft_frm.grid(row=2, column=0, columnspan=3, sticky="w", pady=2)
    ttk.Label(ft_frm, text="File type").pack(side="left", padx=(0, 8))
    ttk.Radiobutton(ft_frm, text="rcpnl", variable=file_type_var, value="rcpnl").pack(
        side="left", padx=(0, 6)
    )
    ttk.Radiobutton(
        ft_frm, text="pysed.ome.tif", variable=file_type_var, value="pysed.ome.tif"
    ).pack(side="left")

    ttk.Separator(frm, orient="horizontal").grid(
        row=3, column=0, columnspan=3, sticky="ew", pady=6
    )

    log_text = scrolledtext.ScrolledText(
        frm, height=14, width=72, state="disabled", font=(_mono, 9)
    )
    log_text.grid(row=4, column=0, columnspan=3, sticky="nsew", pady=(0, 6))

    btn_run = ttk.Button(frm, text="Make samplesheet")
    btn_run.grid(row=5, column=0, columnspan=3, pady=(0, 2))

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
        batch_str = batch_var.get().strip().strip('"')
        output_str = output_var.get().strip().strip('"')
        if not batch_str or not output_str:
            messagebox.showerror("Missing", "Both fields are required.")
            return
        batch_dir_p = Path(batch_str)
        if not batch_dir_p.is_dir():
            messagebox.showerror("Not found", f"Batch folder not found:\n{batch_dir_p}")
            return

        btn_run.configure(state="disabled")

        def _worker():
            try:
                out_path, samples, skipped = make_samplesheet(
                    batch_dir_p, output_str, file_type=file_type_var.get()
                )
                logging.info(f"Samplesheet written to: {out_path}")
                logging.info(f"Samples detected: {len(samples)}")
                for sample_id in sorted(samples):
                    logging.info(f"  {sample_id}: {len(samples[sample_id])} scan(s)")
                if skipped:
                    logging.warning(
                        f"Skipped {len(skipped)} file(s) with no recognized sample ID:"
                    )
                    for p in skipped:
                        logging.warning(f"  {p}")
            except Exception as e:
                logging.error(f"Failed: {e}")
            finally:
                root.after(0, lambda: btn_run.configure(state="normal"))

        threading.Thread(target=_worker, daemon=True).start()

    btn_run.configure(command=_on_run)
    root.mainloop()


# ── entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    cli_main()
