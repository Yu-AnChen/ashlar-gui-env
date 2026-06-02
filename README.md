# run-ashlar

Batch-stitch cyclic microscopy slides with
[ashlar](https://github.com/labsyspharm/ashlar). A single graphical interface
groups the whole workflow into three tabs — **Stitch**, **Channel names**, and
**Compress** — and every step is also available headless from the command line.

## Installation

This project uses [pixi](https://pixi.sh) to manage the environment.

### 1 — Install git (Windows only)

ashlar is installed directly from GitHub, so git must be available during
`pixi install`. Windows does not ship with git. Install it temporarily via pixi:

```sh
pixi global install git
```

You can uninstall it once installation is complete:

```sh
pixi global remove git
```

### 2 — Install the main environment

Run once inside this folder:

```sh
pixi install --locked
```

This installs Python 3.10, ashlar (from source), and all dependencies into an
isolated environment.

### 3 — Install and set up the basicpy environment

Flat-field correction uses a separate pixi environment. Install and warm up its
BioFormats cache with two commands (also defined as pixi tasks):

```sh
pixi run install-basicpy
pixi run setup-basicpy
```

`setup-basicpy` downloads BioFormats JARs on first run — this takes a minute or
two and only needs to happen once.

---

## Quick start — GUI

**Windows:** double-click `run-ashlar.bat`.

**macOS / Linux:** double-click `run-ashlar.sh` (or right-click → Open on
macOS the first time), or run:

```sh
pixi run gui
```

The window has three tabs and a shared log console at the bottom:

- **Stitch** — generate a samplesheet (collapsible helper) and run the ashlar
  batch. This is the main tab.
- **Channel names** — apply marker names to already-stitched OME-TIFFs and copy
  names to/from OMERO.
- **Compress** — recompress `.pysed.ome.tif` files.

---

## Stitch tab

### Input fields

| Field                  | Required | Description                                                          |
| ---------------------- | -------- | -------------------------------------------------------------------- |
| **Config CSV**         | Yes      | Lists the slides to process (directory or mcmicro format, see below) |
| **Input format**       | Yes      | `Directory` or `mcmicro samplesheet`                                 |
| **Markers (override)** | No       | A markers file applied to every slide, overriding auto-extraction    |
| **Output directory**   | depends  | Where output files go (required for mcmicro format)                  |

Paths can be typed directly, pasted with Windows *Copy as path* (surrounding
quotes are stripped automatically), or picked with the **…** browse button.

### Options

| Option                              | Default | Description                                                                 |
| ----------------------------------- | ------- | --------------------------------------------------------------------------- |
| **From slide / To slide**           | 0 / all | Process only a slice of the CSV (0-based, *To* is exclusive)                |
| **Max jobs**                        | 1       | Number of slides processed in parallel                                      |
| **Max shift µm**                    | 30      | Maximum allowed per-tile shift passed to ashlar (`-m`)                      |
| **Filter sigma**                    | 1.0     | Gaussian pre-filter sigma in pixels (`--filter-sigma`). Set to 0 to disable |
| **File type**                       | auto    | Cycle file type for directory format (`auto` = rcpnl then xdce, or pysed)   |
| **Dry run**                         | off     | Print ashlar commands without executing them                                |
| **Skip existing**                   | off     | Skip any slide whose output OME-TIFF already exists                         |
| **Auto-extract pysed channel names** | on      | For `.pysed.ome.tif` input, extract channel names (see below)              |

### Samplesheet helper

The collapsible **Samplesheet helper** scans a batch folder, groups cycle files
by their `LSP…` sample ID, and writes an mcmicro samplesheet CSV. When the file
type is `pysed.ome.tif` it also writes a `<sample>-markers.csv` per sample and
asks you to review them. On success it fills in the Config CSV, switches the
input format to *mcmicro*, and sets the output directory.

### Channel names during stitching

`.rcpnl`/`.xdce` files carry no channel metadata and ashlar does not write
channel names, so names come only from `.pysed.ome.tif` inputs (which carry
them) or from a markers file you supply.

For `.pysed.ome.tif` input with **Auto-extract pysed channel names** on:

1. Before ashlar runs, names are extracted from the cycle files (in cycle order)
   and written to `<slide>-markers.csv` next to the output.
2. ashlar runs — this is your window to **review and edit** that CSV.
3. When stitching finishes, the CSV is re-read from disk and its names are
   written into the output OME-TIFF, so any edits you made take effect.

A **Markers (override)** file, when given, takes precedence and is applied to
every slide as-is.

### Running a batch

1. Fill in the **Config CSV** and pick the input format.
2. Adjust options if needed.
3. Click **Run ashlar**. The progress bar starts and the console streams output.
4. To stop early, click **Cancel** — a running slide finishes its current step;
   slides not yet started are skipped.

### Console and log viewer

The shared console shows timestamped log lines. **Clear console** resets it.
**View logs** opens a viewer with a **Summary** tab (per-slide *waiting /
running / done / failed*) and a live per-slide ashlar output tab. A plain-text
`<slide-name>-ashlar.log` is also written next to each output.

---

## Config CSV format

Two formats are supported; pick one with the **Input format** selector.

### Directory format

| Column       | Required | Description                                                           |
| ------------ | -------- | --------------------------------------------------------------------- |
| `Directory`  | Yes      | Path to the slide folder                                              |
| `Correction` | No       | Set to `1`, `yes`, or `true` to run flat-field correction via basicpy |

```csv
Directory,Correction
D:\data\slide_001,
D:\data\slide_002,1
"C:\Users\Me\Documents\slide 003",
```

Extra columns and blank lines are ignored. Windows *Copy as path* quotes are
stripped automatically.

#### Windows shortcut support

If cycle files (`.rcpnl`, `.xdce`, or `.pysed.ome.tif`) are not stored directly
inside the slide folder, Windows `.lnk` shortcuts are resolved transparently —
both shortcuts pointing at a cycle file and shortcuts pointing at a directory of
cycle files. One level of real subdirectories is searched as well.

### mcmicro samplesheet format

Columns: `sample`, `cycle_number`, `image_tiles`, `Correction`. Rows are grouped
by `sample` and ordered by `cycle_number`. The Samplesheet helper generates this
format for you. An output directory is required.

---

## Markers file format

The canonical file written by run-ashlar is a 3-column CSV:

```csv
channel_number,cycle_number,marker_name
1,1,DAPI
2,1,CD45
3,2,CD3
```

When **reading** a markers file (override field, or the Channel names tab), the
format is detected automatically — these are all accepted:

- the 3-column CSV above (rows ordered by `channel_number`),
- a bare one-name-per-line list,
- an OMERO-style comma-separated line (`DAPI, CD45, CD3`).

The number of names must match the channel count in the target OME-TIFF.

---

## Channel names tab

For applying marker names to OME-TIFFs that are already stitched — to fix names,
or to add them after the fact.

- **OME-TIFF dir / file** + empty **Markers** → each `<sample>.ome.tif` is paired
  with its `<sample>-markers.csv` in the same folder.
- **OME-TIFF dir / file** + a **Markers** file → that one file is applied to all
  targets.
- **OMERO names** box — load names from a markers file or OME-TIFF into an
  OMERO-style string and copy it to the clipboard, or paste a string copied out
  of OMERO and apply it to a file. Useful when names were added in OMERO after
  upload and need to be written back into the file.

---

## Compress tab

Recompresses `.pysed.ome.tif` files (zlib + predictor, with IFDs clustered for
fast Bio-Formats access). The output keeps the same structure and OME-XML, so it
remains a drop-in ashlar input.

- **Input folder** is searched recursively for `.pysed.ome.tif`.
- **Output directory** receives the compressed copies, mirroring the input's
  subfolder structure (originals are kept).
- **Compress in place** overwrites the originals instead.

Files that are already compressed are hardlinked (or copied) rather than
recompressed.

---

## Output

For each slide the Stitch tab writes, next to the slide folder (or in the
configured output directory):

| File                       | Description                                          |
| -------------------------- | ---------------------------------------------------- |
| `<slide-name>.ome.tif`     | Pyramidal OME-TIFF produced by ashlar                |
| `<slide-name>-ashlar.log`  | Full ashlar log (version, command, output)           |
| `<slide-name>-markers.csv` | Extracted channel names (pysed input only; editable) |

When flat-field correction is enabled, illumination profiles are written to an
`illumination_profiles/` folder:

| File                       | Description        |
| -------------------------- | ------------------ |
| `<cycle-name>-ffp.ome.tif` | Flat-field profile |
| `<cycle-name>-dfp.ome.tif` | Dark-field profile |

---

## Command-line mode

The GUI is the recommended interface. Every step is also a subcommand of
`run-ashlar.py` for scripted or headless use:

```sh
# stitch a batch
pixi run python run-ashlar.py stitch slides.csv --markers markers.csv --max-n-jobs 4

# generate an mcmicro samplesheet
pixi run python run-ashlar.py samplesheet /path/to/batch --output-dir /path/to/out

# apply channel names (auto-pair <sample>-markers.csv, or pass --markers)
pixi run python run-ashlar.py channels apply --tiff-dir /path/to/out
pixi run python run-ashlar.py channels extract cycle1.pysed.ome.tif cycle2.pysed.ome.tif -o markers.csv
pixi run python run-ashlar.py channels omero markers.csv

# compress pysed files
pixi run python run-ashlar.py compress /path/to/batch --output-dir /path/to/compressed
```

Run `pixi run python run-ashlar.py --help` (or `<subcommand> --help`) for all
options. With no subcommand, or `--gui`, the GUI launches.
