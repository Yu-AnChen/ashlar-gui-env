"""Unified tk GUI for run-ashlar.

One window with three intent-grouped tabs sharing a single log console:
    Stitch         — samplesheet helper (collapsible) + ashlar batch run
    Channel names  — apply edited markers to OME-TIFFs + OMERO copy in/out
    Compress       — compress pysed OME-TIFF(s)

All real work lives in run_ashlar.core; this module is presentation only.
"""

import csv
import logging
import queue
import threading
import time
from pathlib import Path

from . import core


def launch():
    try:
        import tkinter as tk  # noqa: F401
    except ImportError:
        import sys

        sys.exit("tkinter is not available; use command-line mode instead")
    App().root.mainloop()


class App:
    def __init__(self):
        import tkinter as tk
        import tkinter.font as tkfont
        from tkinter import scrolledtext, ttk

        self.tk = tk
        self.ttk = ttk
        self.root = tk.Tk()
        self.root.title("run-ashlar")
        self.root.resizable(True, True)
        self.mono = "Cascadia Code" if "Cascadia Code" in tkfont.families() else "Courier"

        self.root.columnconfigure(0, weight=1)
        self.root.rowconfigure(0, weight=1)

        outer = ttk.Frame(self.root, padding=8)
        outer.grid(sticky="nsew")
        outer.columnconfigure(0, weight=1)
        outer.rowconfigure(0, weight=3)
        outer.rowconfigure(2, weight=2)

        notebook = ttk.Notebook(outer)
        notebook.grid(row=0, column=0, sticky="nsew")

        self._build_stitch_tab(notebook)
        self._build_channels_tab(notebook)
        self._build_compress_tab(notebook)

        ttk.Separator(outer, orient="horizontal").grid(
            row=1, column=0, sticky="ew", pady=4
        )

        # ── shared log console ──────────────────────────────────────────────────
        log_frame = ttk.Frame(outer)
        log_frame.grid(row=2, column=0, sticky="nsew")
        log_frame.columnconfigure(0, weight=1)
        log_frame.rowconfigure(0, weight=1)
        self.log_text = scrolledtext.ScrolledText(
            log_frame, height=12, width=96, state="disabled", font=(self.mono, 9)
        )
        self.log_text.grid(row=0, column=0, sticky="nsew")

        # ── footer: version (left) + clear console (right), one line ──────────────
        footer = ttk.Frame(outer)
        footer.grid(row=3, column=0, sticky="ew", pady=(4, 0))
        footer.columnconfigure(0, weight=1)
        self.version_var = tk.StringVar(value="ashlar …")
        ttk.Label(
            footer, textvariable=self.version_var, foreground="#888", font=(self.mono, 9)
        ).grid(row=0, column=0, sticky="w")
        ttk.Button(footer, text="Clear console", command=self._clear_console).grid(
            row=0, column=1, sticky="e"
        )

        self._setup_logging()
        self.root.after(120, self._poll_log)
        self._run_thread(self._load_version)

    # ── logging plumbing (shared) ────────────────────────────────────────────────

    def _setup_logging(self):
        self.log_queue: queue.Queue = queue.Queue()
        app = self

        class _QueueHandler(logging.Handler):
            def emit(self, record):
                app.log_queue.put(self.format(record))

        handler = _QueueHandler()
        handler.setFormatter(
            logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", "%H:%M:%S")
        )
        root_logger = logging.getLogger()
        root_logger.addHandler(handler)
        root_logger.setLevel(logging.INFO)

    def _poll_log(self):
        while True:
            try:
                msg = self.log_queue.get_nowait()
            except queue.Empty:
                break
            self.log_text.configure(state="normal")
            self.log_text.insert("end", msg + "\n")
            self.log_text.see("end")
            self.log_text.configure(state="disabled")
        self.root.after(120, self._poll_log)

    def _clear_console(self):
        self.log_text.configure(state="normal")
        self.log_text.delete("1.0", "end")
        self.log_text.configure(state="disabled")

    def _load_version(self):
        v = core.ashlar_version()
        self.root.after(0, lambda: self.version_var.set(f"ashlar {v}"))

    # ── small widget helpers ─────────────────────────────────────────────────────

    def _file_row(self, frm, row, label, var, filetypes):
        ttk = self.ttk
        from tkinter import filedialog

        ttk.Label(frm, text=label).grid(row=row, column=0, sticky="w", pady=2)
        ttk.Entry(frm, textvariable=var, width=54).grid(
            row=row, column=1, padx=4, sticky="ew"
        )
        ttk.Button(
            frm, text="…", width=2,
            command=lambda: self._set_if(var, filedialog.askopenfilename(filetypes=filetypes)),
        ).grid(row=row, column=2)

    def _dir_row(self, frm, row, label, var):
        ttk = self.ttk
        from tkinter import filedialog

        ttk.Label(frm, text=label).grid(row=row, column=0, sticky="w", pady=2)
        entry = ttk.Entry(frm, textvariable=var, width=54)
        entry.grid(row=row, column=1, padx=4, sticky="ew")
        btn = ttk.Button(
            frm, text="…", width=2,
            command=lambda: self._set_if(var, filedialog.askdirectory()),
        )
        btn.grid(row=row, column=2)
        return entry, btn

    @staticmethod
    def _set_if(var, value):
        if value:
            var.set(value)

    @staticmethod
    def _is_num_prefix(text, allow_dot):
        """True if text is empty or a valid (in-progress) non-negative number.

        Used as a key-validation predicate. Empty is allowed so a field can be
        cleared mid-edit, and a lone '.' / trailing '.' is allowed so floats can
        be typed; the .get() backstop in the run handler catches those.
        """
        if text == "":
            return True
        if allow_dot:
            if text.count(".") > 1:
                return False
            text = text.replace(".", "", 1)
            return text == "" or text.isdigit()
        return text.isdigit()

    def _run_thread(self, target):
        threading.Thread(target=target, daemon=True).start()

    # ══ Stitch tab ═══════════════════════════════════════════════════════════════

    def _build_stitch_tab(self, notebook):
        tk, ttk = self.tk, self.ttk
        from tkinter import messagebox

        tab = ttk.Frame(notebook, padding=10)
        notebook.add(tab, text="Stitch")
        tab.columnconfigure(1, weight=1)

        self.csv_var = tk.StringVar()
        self.fmt_var = tk.StringVar(value="directory")
        self.markers_var = tk.StringVar()
        self.output_dir_var = tk.StringVar()
        self.jobs_var = tk.IntVar(value=2)
        self.margin_var = tk.IntVar(value=30)
        self.sigma_var = tk.DoubleVar(value=1.0)
        self.dry_var = tk.BooleanVar(value=False)
        self.skip_var = tk.BooleanVar(value=False)
        self.file_type_var = tk.StringVar(value="auto")

        # debug settings (collapsible)
        self._orion_open = False
        self.njobs_var = tk.IntVar(value=5)
        self.align_channel_var = tk.IntVar(value=0)
        self.stitch_alpha_var = tk.DoubleVar(value=0.01)
        self.output_channels_var = tk.StringVar()
        self.max_error_var = tk.StringVar()
        self.temp_dir_var = tk.StringVar()
        self.no_mask_var = tk.BooleanVar(value=False)
        self.only_qc_var = tk.BooleanVar(value=False)
        self.flip_x_var = tk.BooleanVar(value=False)
        self.flip_y_var = tk.BooleanVar(value=False)
        self.flip_mx_var = tk.BooleanVar(value=False)
        self.flip_my_var = tk.BooleanVar(value=False)

        # ── collapsible samplesheet helper ───────────────────────────────────────
        self._helper_open = False
        self.helper_batch_var = tk.StringVar()
        self.helper_output_var = tk.StringVar()
        self.helper_ft_var = tk.StringVar(value="rcpnl")

        self.toggle_btn = ttk.Button(
            tab, text="▶ Samplesheet helper", command=self._toggle_helper
        )
        self.toggle_btn.grid(row=0, column=0, columnspan=3, sticky="w", pady=(0, 2))

        self.helper_frm = ttk.Frame(tab, padding=(12, 2, 4, 4))
        self.helper_frm.columnconfigure(1, weight=1)
        self.helper_frm.grid(row=1, column=0, columnspan=3, sticky="ew")
        self.helper_frm.grid_remove()

        self._dir_row(self.helper_frm, 0, "Batch folder", self.helper_batch_var)
        self._dir_row(self.helper_frm, 1, "Output directory", self.helper_output_var)

        hft = ttk.Frame(self.helper_frm)
        hft.grid(row=2, column=0, columnspan=3, sticky="w", pady=2)
        ttk.Label(hft, text="File type").pack(side="left", padx=(0, 8))
        ttk.Radiobutton(hft, text="rcpnl", variable=self.helper_ft_var, value="rcpnl").pack(
            side="left", padx=(0, 6)
        )
        ttk.Radiobutton(
            hft, text="pysed.ome.tif", variable=self.helper_ft_var, value="pysed.ome.tif"
        ).pack(side="left")

        self.helper_make_btn = ttk.Button(
            self.helper_frm, text="Make samplesheet", command=self._on_make_samplesheet
        )
        self.helper_make_btn.grid(row=3, column=0, columnspan=3, pady=(4, 2))

        ttk.Separator(tab, orient="horizontal").grid(
            row=2, column=0, columnspan=3, sticky="ew", pady=6
        )

        # ── main stitch form ─────────────────────────────────────────────────────
        self._file_row(tab, 3, "Config CSV *", self.csv_var, [("CSV", "*.csv"), ("All", "*.*")])

        fmt = ttk.Frame(tab)
        fmt.grid(row=4, column=0, columnspan=3, sticky="w", pady=2)
        ttk.Label(fmt, text="Input format").pack(side="left", padx=(0, 8))
        ttk.Radiobutton(fmt, text="Directory", variable=self.fmt_var, value="directory").pack(
            side="left", padx=(0, 6)
        )
        ttk.Radiobutton(
            fmt, text="mcmicro samplesheet", variable=self.fmt_var, value="mcmicro"
        ).pack(side="left")

        self._file_row(tab, 5, "Markers (override)", self.markers_var, [("CSV", "*.csv"), ("All", "*.*")])
        self._dir_row(tab, 6, "Output directory", self.output_dir_var)
        self._dir_row(tab, 7, "Cache directory", self.temp_dir_var)

        # key-level validation: block non-numeric typing before it can reach .get()
        vint = (self.root.register(lambda p: self._is_num_prefix(p, False)), "%P")
        vfloat = (self.root.register(lambda p: self._is_num_prefix(p, True)), "%P")

        opts = ttk.Frame(tab)
        opts.grid(row=8, column=0, columnspan=3, sticky="w", pady=6)
        for label, var, hi in [
            ("Parallel slides", self.jobs_var, 4),   # hard-capped at 4 (see clamp_parallelism)
            ("Assembly jobs", self.njobs_var, 64),
        ]:
            ttk.Label(opts, text=label).pack(side="left", padx=(0, 2))
            ttk.Spinbox(
                opts, textvariable=var, from_=1, to=hi, width=4,
                validate="key", validatecommand=vint,
            ).pack(side="left", padx=(0, 12))
        ttk.Label(opts, text="File type").pack(side="left", padx=(16, 2))
        ttk.Combobox(
            opts, textvariable=self.file_type_var, values=["auto", "pysed.ome.tif"],
            state="readonly", width=13,
        ).pack(side="left", padx=(0, 10))

        opts2 = ttk.Frame(tab)
        opts2.grid(row=9, column=0, columnspan=3, sticky="w")
        for text, var in [
            ("Dry run", self.dry_var),
            ("Skip existing", self.skip_var),
            ("No mask background", self.no_mask_var),
            ("Only QC", self.only_qc_var),
        ]:
            ttk.Checkbutton(opts2, text=text, variable=var).pack(side="left", padx=(0, 8))

        # ── collapsible Debug settings ───────────────────────────────────────────
        self.orion_toggle_btn = ttk.Button(
            tab, text="▶ Debug settings", command=self._toggle_orion
        )
        self.orion_toggle_btn.grid(row=10, column=0, columnspan=3, sticky="w", pady=(6, 2))

        self.orion_frm = ttk.Frame(tab, padding=(12, 2, 4, 4))
        self.orion_frm.columnconfigure(1, weight=1)
        self.orion_frm.grid(row=11, column=0, columnspan=3, sticky="ew")
        self.orion_frm.grid_remove()
        self._build_orion_options(self.orion_frm, vint, vfloat)

        self.stitch_prog = ttk.Progressbar(tab, mode="indeterminate")
        self.stitch_prog.grid(row=12, column=0, columnspan=3, sticky="ew", pady=(8, 6))

        bar = ttk.Frame(tab)
        bar.grid(row=13, column=0, columnspan=3)
        self.btn_run = ttk.Button(bar, text="Run ashlar", command=self._on_run_stitch)
        self.btn_run.pack(side="left", padx=(0, 8))
        self.btn_cancel = ttk.Button(
            bar, text="Cancel", state="disabled", command=self._on_cancel_stitch
        )
        self.btn_cancel.pack(side="left", padx=(0, 8))
        ttk.Button(bar, text="View logs", command=self._open_log_viewer).pack(side="left")

        # stitch run state
        self.stitch_cancel = threading.Event()
        self.active_log_paths = []      # (slide_key, log_path) per slide
        self.slide_status = {}          # slide_key -> running/done/failed/cancelled
        self.batch_done = [True]
        self.batch_start_time = [0.0]

        _ = messagebox  # imported lazily where used

    def _toggle_helper(self):
        if self._helper_open:
            self.helper_frm.grid_remove()
            self.toggle_btn.configure(text="▶ Samplesheet helper")
        else:
            self.helper_frm.grid()
            self.toggle_btn.configure(text="▼ Samplesheet helper")
        self._helper_open = not self._helper_open

    def _toggle_orion(self):
        if self._orion_open:
            self.orion_frm.grid_remove()
            self.orion_toggle_btn.configure(text="▶ Debug settings")
        else:
            self.orion_frm.grid()
            self.orion_toggle_btn.configure(text="▼ Debug settings")
        self._orion_open = not self._orion_open

    def _build_orion_options(self, frm, vint, vfloat):
        ttk = self.ttk

        row1 = ttk.Frame(frm)
        row1.grid(row=0, column=0, columnspan=3, sticky="w", pady=2)
        for label, var, lo, hi, inc, w, vcmd in [
            ("Max shift µm", self.margin_var, 0, 500, 5, 5, vint),
            ("Filter sigma", self.sigma_var, 0, 10, 0.5, 4, vfloat),
            ("Align channel", self.align_channel_var, 0, 99, 1, 4, vint),
            ("Stitch alpha", self.stitch_alpha_var, 0, 1, 0.01, 6, vfloat),
        ]:
            ttk.Label(row1, text=label).pack(side="left", padx=(0, 2))
            ttk.Spinbox(
                row1, textvariable=var, from_=lo, to=hi, increment=inc, width=w,
                validate="key", validatecommand=vcmd,
            ).pack(side="left", padx=(0, 12))

        row2 = ttk.Frame(frm)
        row2.grid(row=1, column=0, columnspan=3, sticky="w", pady=2)
        ttk.Label(row2, text="Output channels").pack(side="left", padx=(0, 2))
        ttk.Entry(row2, textvariable=self.output_channels_var, width=16).pack(
            side="left", padx=(0, 4)
        )
        ttk.Label(row2, text="(e.g. 0 1 2; blank = all)").pack(side="left", padx=(0, 16))
        ttk.Label(row2, text="Max error").pack(side="left", padx=(0, 2))
        ttk.Entry(
            row2, textvariable=self.max_error_var, width=6,
            validate="key", validatecommand=vfloat,
        ).pack(side="left")

        row3 = ttk.Frame(frm)
        row3.grid(row=2, column=0, columnspan=3, sticky="w", pady=2)
        for text, var in [
            ("Flip X", self.flip_x_var),
            ("Flip Y", self.flip_y_var),
            ("Flip mosaic X", self.flip_mx_var),
            ("Flip mosaic Y", self.flip_my_var),
        ]:
            ttk.Checkbutton(row3, text=text, variable=var).pack(side="left", padx=(0, 8))

    def _on_make_samplesheet(self):
        from tkinter import messagebox

        batch_str = self.helper_batch_var.get().strip().strip('"')
        output_str = self.helper_output_var.get().strip().strip('"')
        if not batch_str or not output_str:
            messagebox.showerror("Missing", "Both batch folder and output directory are required.")
            return
        batch_dir = Path(batch_str)
        if not batch_dir.is_dir():
            messagebox.showerror("Not found", f"Batch folder not found:\n{batch_dir}")
            return
        ft = self.helper_ft_var.get()
        self.helper_make_btn.configure(state="disabled")

        def worker():
            try:
                out_path, samples, skipped = core.make_samplesheet(batch_dir, output_str, file_type=ft)
                logging.info(f"Samplesheet written to: {out_path}")
                logging.info(f"Samples detected: {len(samples)}")
                for sid in sorted(samples):
                    logging.info(f"  {sid}: {len(samples[sid])} scan(s)")
                if skipped:
                    logging.warning(f"Skipped {len(skipped)} file(s) with no recognized sample ID")

                # pysed: also write per-sample markers and notify for review
                if ft == "pysed.ome.tif":
                    out_dir = Path(output_str)
                    out_dir.mkdir(parents=True, exist_ok=True)
                    for sid in sorted(samples):
                        try:
                            names, cycles = core.extract_markers(samples[sid])
                            mpath = out_dir / f"{sid}-markers.csv"
                            core.write_markers(mpath, names, cycles)
                            logging.info(f"  Wrote {mpath.name} ({len(names)} channels)")
                            for nm in names:
                                if core.is_placeholder_name(nm):
                                    logging.warning(f"  {sid}: channel name looks unset: {nm!r}")
                        except Exception as e:
                            logging.warning(f"  {sid}: could not extract channel names: {e}")
                    logging.info("Review the *-markers.csv files before stitching.")

                self.root.after(0, lambda: (
                    self.csv_var.set(str(out_path)),
                    self.fmt_var.set("mcmicro"),
                    self.output_dir_var.set(output_str),
                ))
            except Exception as e:
                logging.error(f"Make samplesheet failed: {e}")
            finally:
                self.root.after(0, lambda: self.helper_make_btn.configure(state="normal"))

        self._run_thread(worker)

    def _on_run_stitch(self):
        from tkinter import messagebox

        csv_str = self.csv_var.get().strip().strip('"')
        if not csv_str:
            messagebox.showerror("Missing", "Please select a config CSV.")
            return
        csv_p = Path(csv_str)
        if not csv_p.is_file():
            messagebox.showerror("Not found", f"Config CSV not found:\n{csv_p}")
            return

        fmt = self.fmt_var.get()
        if fmt == "mcmicro":
            try:
                slides = core._parse_mcmicro_sheet(csv_p)
            except Exception as e:
                messagebox.showerror("Parse error", str(e))
                return
            mismatches = core._find_cycle_order_mismatches(slides)
            if mismatches:
                lines = ["Cycle order in samplesheet differs from file modification time:\n"]
                for m in mismatches:
                    lines.append(f"  {m['sample']}:")
                    lines.append(f"    samplesheet: {', '.join(m['specified'])}")
                    lines.append(f"    by mtime:    {', '.join(m['by_mtime'])}")
                lines.append("\nProceed with samplesheet order?")
                if not messagebox.askyesno("Cycle order mismatch", "\n".join(lines)):
                    return
        else:
            with open(csv_p, newline="") as f:
                slides = list(csv.DictReader(f))

        try:
            max_jobs = self.jobs_var.get()
            max_shift = self.margin_var.get()
            sigma = self.sigma_var.get()  # 0 → orion's default (no filtering); handled in core
            njobs = self.njobs_var.get()
            align_channel = self.align_channel_var.get()
            stitch_alpha = self.stitch_alpha_var.get()
            oc_raw = self.output_channels_var.get().strip()
            output_channels = (
                [int(x) for x in oc_raw.replace(",", " ").split()] if oc_raw else None
            )
            me_raw = self.max_error_var.get().strip()
            max_error = float(me_raw) if me_raw else None
        except (ValueError, self.tk.TclError):
            messagebox.showerror(
                "Invalid input",
                "Numeric fields (jobs, shift, sigma, align channel, stitch alpha, "
                "output channels, max error) must be valid numbers.",
            )
            return
        subset = slides

        markers_names = None
        m_path = self.markers_var.get().strip().strip('"')
        if m_path:
            try:
                markers_names = core.read_markers(m_path)
            except Exception as e:
                messagebox.showerror("Markers error", str(e))
                return

        output_dir = self.output_dir_var.get().strip().strip('"') or None
        if fmt == "mcmicro" and not output_dir:
            messagebox.showerror(
                "Missing", "Output directory is required when using mcmicro samplesheet format."
            )
            return
        if output_dir:
            try:
                Path(output_dir).mkdir(parents=True, exist_ok=True)
            except OSError as e:
                messagebox.showerror(
                    "Output directory",
                    f"Could not create output directory:\n{output_dir}\n\n{e}",
                )
                return

        # precompute log + output paths so the viewer can open immediately
        self.active_log_paths.clear()
        self.slide_status.clear()
        try:
            for slide in subset:
                if "cycle_files" in slide:
                    slide_name = slide["sample"]
                    out_p = Path(output_dir).resolve() if output_dir else Path.cwd()
                else:
                    slide_dir = Path(slide["Directory"].strip().strip('"')).resolve()
                    slide_name = slide_dir.name
                    out_p = Path(output_dir).resolve() if output_dir else slide_dir.parent
                self.active_log_paths.append(
                    (core._slide_key(slide), out_p / f"{slide_name}-ashlar.log")
                )
        except Exception as e:
            messagebox.showerror("Error", f"{type(e).__name__}: {e}")
            return

        self._clear_console()
        self.stitch_cancel.clear()
        self.batch_done[0] = False
        self.batch_start_time[0] = time.time()
        self.btn_run.configure(state="disabled")
        self.btn_cancel.configure(state="normal")
        self.stitch_prog.start(10)

        ft = self.file_type_var.get()

        def worker():
            try:
                core.run_batch(
                    subset,
                    max_n_jobs=max_jobs,
                    cancel_event=self.stitch_cancel,
                    on_status=self._set_slide_status,
                    orion=core.OrionOptions(
                        maximum_shift=max_shift,
                        filter_sigma=sigma,
                        align_channel=align_channel,
                        output_channels=output_channels,
                        stitch_alpha=stitch_alpha,
                        max_error=max_error,
                        flip_x=self.flip_x_var.get(),
                        flip_y=self.flip_y_var.get(),
                        n_jobs=njobs,
                        temp_dir=self.temp_dir_var.get().strip().strip('"') or None,
                        no_mask_background=self.no_mask_var.get(),
                        only_qc=self.only_qc_var.get(),
                        flip_mosaic_x=self.flip_mx_var.get(),
                        flip_mosaic_y=self.flip_my_var.get(),
                    ),
                    markers_names=markers_names,
                    extract_pysed_markers=True,
                    dry_run=self.dry_var.get(),
                    skip_existing=self.skip_var.get(),
                    output_dir=output_dir,
                    file_type=None if ft == "auto" else ft,
                )
            except Exception as e:
                logging.error(f"Batch error: {e}")
            finally:
                self.batch_done[0] = True
                self.root.after(0, lambda: (
                    self.btn_run.configure(state="normal"),
                    self.btn_cancel.configure(state="disabled"),
                    self.stitch_prog.stop(),
                ))

        self._run_thread(worker)

    def _on_cancel_stitch(self):
        self.stitch_cancel.set()
        self.btn_cancel.configure(state="disabled")
        logging.info("Cancelling — waiting for running slides to finish…")

    def _set_slide_status(self, key, status):
        # called from run_batch worker thread(s); a single dict write is atomic
        # enough under the GIL, and the viewer only reads via .get()
        self.slide_status[key] = status

    def _open_log_viewer(self):
        tk, ttk = self.tk, self.ttk
        from tkinter import messagebox, scrolledtext

        if not self.active_log_paths:
            messagebox.showinfo("No logs", "Run a batch first.")
            return

        win = tk.Toplevel(self.root)
        win.title("Ashlar log viewer")
        win.resizable(True, True)
        win.columnconfigure(0, weight=1)
        win.rowconfigure(0, weight=1)
        notebook = ttk.Notebook(win)
        notebook.grid(sticky="nsew", padx=8, pady=8)

        sum_frame = ttk.Frame(notebook)
        sum_frame.columnconfigure(0, weight=1)
        sum_frame.rowconfigure(0, weight=1)
        sum_txt = scrolledtext.ScrolledText(
            sum_frame, height=20, width=60, font=(self.mono, 9), state="disabled"
        )
        sum_txt.grid(sticky="nsew")
        notebook.add(sum_frame, text="Summary")

        positions = {log: 0 for _, log in self.active_log_paths}
        slide_tabs = {}

        def add_slide_tab(log_path):
            frame = ttk.Frame(notebook)
            frame.columnconfigure(0, weight=1)
            frame.rowconfigure(0, weight=1)
            txt = scrolledtext.ScrolledText(
                frame, height=30, width=100, font=(self.mono, 9), state="disabled"
            )
            txt.grid(sticky="nsew")
            notebook.add(frame, text=log_path.name.replace("-ashlar.log", ""))
            slide_tabs[log_path] = txt
            return txt

        def update_summary():
            lines = ["  {:<16}  {}".format("status", "slide"), "  " + "─" * 52]
            for key, log_path in self.active_log_paths:
                slide_name = log_path.name.replace("-ashlar.log", "")
                # status comes from the batch itself, not from output timestamps
                status = self.slide_status.get(key)
                if status is None:
                    status = "waiting" if not self.batch_done[0] else "---"
                lines.append(f"  {status:<16}  {slide_name}")
            sum_txt.configure(state="normal")
            sum_txt.delete("1.0", "end")
            sum_txt.insert("end", "\n".join(lines) + "\n")
            sum_txt.configure(state="disabled")

        def poll_files():
            update_summary()
            for _, log_path in self.active_log_paths:
                if log_path.exists() and log_path.stat().st_mtime >= self.batch_start_time[0]:
                    if log_path not in slide_tabs:
                        add_slide_tab(log_path)
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
                win.after(100, poll_files)

        win.after(100, poll_files)

    # ══ Channel names tab ════════════════════════════════════════════════════════

    def _build_channels_tab(self, notebook):
        tk, ttk = self.tk, self.ttk

        tab = ttk.Frame(notebook, padding=10)
        notebook.add(tab, text="Channel names")
        tab.columnconfigure(1, weight=1)

        self.ch_dir_var = tk.StringVar()
        self.ch_markers_var = tk.StringVar()

        ttk.Label(
            tab,
            text="Apply markers to OME-TIFFs. Leave the markers field empty to auto-pair "
            "each  <sample>.ome.tif  with its  <sample>-markers.csv  in the directory.",
            justify="left", wraplength=620,
        ).grid(row=0, column=0, columnspan=3, sticky="w", pady=(0, 6))

        self._dir_row(tab, 1, "OME-TIFF dir / file", self.ch_dir_var)
        self._file_row(tab, 2, "Markers (optional)", self.ch_markers_var, [("CSV", "*.csv"), ("All", "*.*")])

        self.ch_apply_btn = ttk.Button(tab, text="Apply channel names", command=self._on_apply_channels)
        self.ch_apply_btn.grid(row=3, column=0, columnspan=3, pady=(2, 6))

        ttk.Separator(tab, orient="horizontal").grid(row=4, column=0, columnspan=3, sticky="ew", pady=6)

        ttk.Label(
            tab,
            text="OMERO names — paste a comma-separated list copied from OMERO, or load "
            "names from a file to copy into OMERO.",
            justify="left", wraplength=620,
        ).grid(row=5, column=0, columnspan=3, sticky="w")

        from tkinter import scrolledtext

        self.omero_box = scrolledtext.ScrolledText(tab, height=4, width=80, font=(self.mono, 9))
        self.omero_box.grid(row=6, column=0, columnspan=3, sticky="ew", pady=(2, 4))

        obar = ttk.Frame(tab)
        obar.grid(row=7, column=0, columnspan=3)
        ttk.Button(obar, text="Load names from file…", command=self._on_omero_load).pack(side="left", padx=(0, 8))
        ttk.Button(obar, text="Copy to clipboard", command=self._on_omero_copy).pack(side="left", padx=(0, 8))
        ttk.Button(obar, text="Apply names to file…", command=self._on_omero_apply_file).pack(side="left", padx=(0, 8))
        ttk.Button(obar, text="Apply names to folder…", command=self._on_omero_apply_dir).pack(side="left")

    def _channel_targets(self):
        """Return list of OME-TIFF Paths from the dir/file entry, or None on error."""
        from tkinter import messagebox

        s = self.ch_dir_var.get().strip().strip('"')
        if not s:
            messagebox.showerror("Missing", "Choose an OME-TIFF directory or file.")
            return None
        p = Path(s)
        if p.is_dir():
            tiffs = sorted(p.glob("*.ome.tif"))
            if not tiffs:
                messagebox.showwarning("No files", f"No .ome.tif files in:\n{p}")
                return None
            return tiffs
        if p.is_file():
            return [p]
        messagebox.showerror("Not found", f"Path not found:\n{p}")
        return None

    def _on_apply_channels(self):
        from tkinter import messagebox

        tiffs = self._channel_targets()
        if tiffs is None:
            return
        markers = self.ch_markers_var.get().strip().strip('"')
        markers_path = Path(markers) if markers else None
        if markers_path and not markers_path.is_file():
            messagebox.showerror("Not found", f"Markers file not found:\n{markers_path}")
            return

        self.ch_apply_btn.configure(state="disabled")

        def worker():
            n_ok = 0
            for tif in tiffs:
                if markers_path is None:
                    mp = tif.with_name(tif.name.replace(".ome.tif", "-markers.csv"))
                    if not mp.exists():
                        logging.warning(f"{tif.name}: no {mp.name}; skipping")
                        continue
                else:
                    mp = markers_path
                logging.info(f"{tif.name}: applying {mp.name}")
                try:
                    n = core.apply_markers_to_tiff(tif, mp)
                    logging.info(f"  wrote {n} channel name(s)")
                    n_ok += 1
                except Exception as e:
                    logging.error(f"  failed: {e}")
            logging.info(f"Finished: {n_ok}/{len(tiffs)} file(s) updated")
            self.root.after(0, lambda: self.ch_apply_btn.configure(state="normal"))

        self._run_thread(worker)

    def _on_omero_load(self):
        from tkinter import filedialog, messagebox

        path = filedialog.askopenfilename(
            filetypes=[("Markers/OME-TIFF", "*.csv *.tif *.tiff"), ("All", "*.*")]
        )
        if not path:
            return
        p = Path(path)
        try:
            if p.suffix.lower() in (".tif", ".tiff"):
                names = core.read_channel_names(p)
            else:
                names = core.read_markers(p)
        except Exception as e:
            messagebox.showerror("Read error", str(e))
            return
        self.omero_box.delete("1.0", "end")
        self.omero_box.insert("end", core.names_to_omero_string(names))

    def _on_omero_copy(self):
        text = self.omero_box.get("1.0", "end").strip()
        self.root.clipboard_clear()
        self.root.clipboard_append(text)
        logging.info("Copied channel names to clipboard.")

    def _on_omero_apply_file(self):
        from tkinter import filedialog

        names = self._omero_names_or_warn()
        if names is None:
            return
        path = filedialog.askopenfilename(filetypes=[("OME-TIFF", "*.ome.tif *.tif *.tiff"), ("All", "*.*")])
        if path:
            self._omero_apply(names, [Path(path)])

    def _on_omero_apply_dir(self):
        from tkinter import filedialog, messagebox

        names = self._omero_names_or_warn()
        if names is None:
            return
        d = filedialog.askdirectory()
        if not d:
            return
        tiffs = sorted(Path(d).glob("*.ome.tif"))
        if not tiffs:
            messagebox.showwarning("No files", f"No .ome.tif files in:\n{d}")
            return
        self._omero_apply(names, tiffs)

    def _omero_names_or_warn(self):
        from tkinter import messagebox

        names = core.parse_marker_text(self.omero_box.get("1.0", "end"))
        if not names:
            messagebox.showerror("Empty", "The OMERO names box is empty.")
            return None
        return names

    def _omero_apply(self, names, tiffs):
        def worker():
            n_ok = 0
            for tif in tiffs:
                logging.info(f"{tif.name}: applying {len(names)} channel name(s) from OMERO box")
                try:
                    core.add_channel_names(str(tif), names)
                    logging.info("  done")
                    n_ok += 1
                except Exception as e:
                    logging.error(f"  failed: {e}")
            logging.info(f"Finished: {n_ok}/{len(tiffs)} file(s) updated")

        self._run_thread(worker)

    # ══ Compress tab ═════════════════════════════════════════════════════════════

    def _build_compress_tab(self, notebook):
        tk, ttk = self.tk, self.ttk

        tab = ttk.Frame(notebook, padding=10)
        notebook.add(tab, text="Compress")
        tab.columnconfigure(1, weight=1)

        self.cz_input_var = tk.StringVar()
        self.cz_output_var = tk.StringVar()
        self.cz_inplace_var = tk.BooleanVar(value=False)

        ttk.Label(
            tab,
            text="Compress .pysed.ome.tif files (recursively under the input folder). "
            "Output mirrors the input's subfolder structure; originals are kept unless "
            "'Compress in place' is checked.",
            justify="left", wraplength=620,
        ).grid(row=0, column=0, columnspan=3, sticky="w", pady=(0, 6))

        self._dir_row(tab, 1, "Input folder *", self.cz_input_var)
        self.cz_out_entry, self.cz_out_btn = self._dir_row(
            tab, 2, "Output directory *", self.cz_output_var
        )

        ttk.Checkbutton(
            tab, text="Compress in place (overwrite originals)",
            variable=self.cz_inplace_var, command=self._on_inplace_toggle,
        ).grid(row=3, column=0, columnspan=3, sticky="w", pady=2)
        self._on_inplace_toggle()  # apply initial enabled/disabled state

        self.cz_prog = ttk.Progressbar(tab, mode="indeterminate")
        self.cz_prog.grid(row=4, column=0, columnspan=3, sticky="ew", pady=(8, 6))

        bar = ttk.Frame(tab)
        bar.grid(row=5, column=0, columnspan=3)
        self.cz_run_btn = ttk.Button(bar, text="Compress", command=self._on_compress)
        self.cz_run_btn.pack(side="left", padx=(0, 8))
        self.cz_cancel_btn = ttk.Button(bar, text="Cancel", state="disabled", command=self._on_cancel_compress)
        self.cz_cancel_btn.pack(side="left")

        self.compress_cancel = threading.Event()

    def _on_inplace_toggle(self):
        # the output dir is irrelevant when compressing in place — grey it out
        state = "disabled" if self.cz_inplace_var.get() else "normal"
        self.cz_out_entry.configure(state=state)
        self.cz_out_btn.configure(state=state)

    def _on_compress(self):
        from tkinter import messagebox

        in_str = self.cz_input_var.get().strip().strip('"')
        if not in_str:
            messagebox.showerror("Missing", "Choose an input folder.")
            return
        in_dir = Path(in_str)
        if not in_dir.is_dir():
            messagebox.showerror("Not found", f"Input folder not found:\n{in_dir}")
            return
        in_place = self.cz_inplace_var.get()
        out_str = self.cz_output_var.get().strip().strip('"')
        if not in_place and not out_str:
            messagebox.showerror("Missing", "Choose an output directory (or check 'Compress in place').")
            return

        files = core.find_pysed_files(in_dir)
        if not files:
            messagebox.showwarning("No files", f"No .pysed.ome.tif files under:\n{in_dir}")
            return

        self.compress_cancel.clear()
        self.cz_run_btn.configure(state="disabled")
        self.cz_cancel_btn.configure(state="normal")
        self.cz_prog.start(10)

        def worker():
            try:
                logging.info(f"Compressing {len(files)} file(s) from {in_dir}")
                core.compress_pysed_batch(
                    files, out_str or None, in_place=in_place,
                    base_dir=in_dir, cancel_event=self.compress_cancel,
                )
            except Exception as e:
                logging.error(f"Compress error: {e}")
            finally:
                self.root.after(0, lambda: (
                    self.cz_run_btn.configure(state="normal"),
                    self.cz_cancel_btn.configure(state="disabled"),
                    self.cz_prog.stop(),
                ))

        self._run_thread(worker)

    def _on_cancel_compress(self):
        self.compress_cancel.set()
        self.cz_cancel_btn.configure(state="disabled")
        logging.info("Cancelling — finishing the current file…")
