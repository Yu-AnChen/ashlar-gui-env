# Plan: Replace Fiji/BaSiC with basicpy for flat-field correction

## Goal

Replace the Fiji-based FFP generation (`_generate_ffp`) in `run-ashlar.py` with
[basicpy-docker-mcmicro](https://github.com/labsyspharm/basicpy-docker-mcmicro),
invoked via a separate pixi environment. The Fiji path is removed entirely.

## Approach

- Add a `[feature.basicpy]` environment to `pixi.toml` using **Python 3.11+**
  (jaxlib has no win_amd64 wheel for Python 3.10).
- Install basicpy-docker-mcmicro as a local or git pypi dependency (it has a
  `pyproject.toml`); pull in `jax`, `jaxlib`, `bioio`, `bioio-bioformats` (custom
  git fork), etc.
- Invoke basicpy from `run-ashlar.py` via the basicpy env's Python interpreter,
  mirroring the mcmicro call:
  ```
  python main.py -i <cycle_file> -o <illum_dir> --output-flatfield <stem> --output-darkfield <stem>
  ```
- Remove the `--fiji-path` CLI flag and "Fiji executable" GUI row entirely.

## BioFormats first-run cache

- Outside Docker, Maven artifacts land in `%USERPROFILE%\.m2\repository` (Windows)
  / `~/.m2/repository`.
- Add a `pixi run setup-basicpy` task that runs `populate_scyjava_cache.py` once
  after `pixi install` to pre-warm the cache.

## Things to verify on win-64 before implementing

1. Does `pixi install` with jax/jaxlib pypi deps resolve cleanly on win-64
   (Python 3.11, CPU-only)?
2. Does `bioio-bioformats` (custom git fork) install without issues on Windows?
3. What does basicpy actually write to disk?
   - Does `--output-flatfield <stem>` produce `<stem>-ffp.tiff` or `<stem>.tiff`
     or something else?
   - Is the extension `.tiff` (double f) or `.tif`?
   - Confirm against the mcmicro call:
     `/opt/main.py -i $image -o . --output-flatfield $prefix --output-darkfield $prefix`
4. Does `populate_scyjava_cache.py` run cleanly on Windows and cache to the
   expected location?

## Implementation notes (for when tests pass)

- The existence check and the path passed to ashlar `--ffp` must use the exact
  filename basicpy writes (`.tiff` vs `.tif`, with or without `-ffp` suffix).
- Follow mcmicro: do **not** pass `--sort_intensity` (default is False; mcmicro
  does not set it).
- The `--output-darkfield` output is generated but not used by ashlar; write it
  to the same `illumination_profiles/` directory anyway.
- In parallel-job mode, multiple basicpy calls may run concurrently — each writes
  to its own `illumination_profiles/` subdirectory so no collision.
