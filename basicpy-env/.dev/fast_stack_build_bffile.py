"""Faster `get_xarray_dask_stack` build for the bioio-bioformats >=2 (bffile) backend.

KEEP FOR FUTURE MIGRATION. This is only relevant if/when basicpy-env moves from
bioio-bioformats<2 (scyjava/JPype) to >=2 (bffile). It is NOT used by the current
code and does NOT apply to the 1.3.x backend (see note below).

Why it exists
-------------
On the 2.x bffile backend, `Reader.get_xarray_dask_stack` wraps every mosaic scene
in its own xarray DataArray (~0.4 s/scene of pure object construction). For a
187-tile .rcpnl that is ~73 s of overhead *before a single pixel is read*. Going
through the lower-level `BioFile.to_dask` per series and stacking with dask skips
that wrapping and produces the same lazy array.

Measured (LSP12961 .rcpnl, 187 scenes, lazy build only, no pixel reads):
    get_xarray_dask_stack ............ ~73 s
    build_fast (below) ............... ~12 s     (~6x; pixels verified identical)

Why it does NOT apply to bioio-bioformats 1.3.x (current backend)
-----------------------------------------------------------------
1. Different internals: `Reader` has no `_bf`; each scene read reopens a fresh
   `BioFile`. Its stock build is already ~6.5 s — faster than this 2.x
   optimization (~12 s) — and full read+downsample is I/O-bound (~11 s), so a
   manual build wins nothing.
2. A single-open variant is also INCORRECT on 1.3.x: `to_dask()` returns lazy
   arrays bound to one shared, mutable reader, so a deferred `.compute()` reads
   the reader's *final* series for every tile. The per-scene reopen the stock
   path does is load-bearing for correctness, not waste.

How to use (2.x only)
---------------------
Replace main.py's `get_xarray_dask_stack` call (currently lines ~251-257) with:

    from fast_stack_build_bffile import build_fast
    istack = build_fast(image)              # lazy (C, I=M*T*Z, Y, X) dask array

Downstream changes: `channel_stack.data` becomes the dask array directly (iterate
`for c, channel_data in enumerate(istack, 1)`), and the single-sited check uses
`istack.shape[1]` instead of `len(istack.coords["I"])`.
"""

import dask.array as da


def build_fast(image):
    """Return a lazy (C, I=M*T*Z, Y, X) dask array equivalent to
    `image.get_xarray_dask_stack(scene_character="M").stack(I=("M","T","Z"))
    .transpose("C","I","Y","X")`, but built via BioFile.to_dask per series to
    skip per-scene xarray wrapping.

    `image` must be a bioio_bioformats.Reader on the bffile backend (v>=2).
    """
    bf = image._bf
    arrs = []
    for i in range(len(image.scenes)):
        a = bf.to_dask(series=i, chunks=(1, 1, 1, -1, -1))  # (T,C,Z,Y,X[,S])
        if a.ndim == 6:  # drop trailing rgb/sample singleton
            a = a[..., 0]
        arrs.append(a)
    stack = da.stack(arrs, axis=0)  # (M,T,C,Z,Y,X)
    M, T, C, Z, Y, X = stack.shape
    return stack.transpose(2, 0, 1, 3, 4, 5).reshape(C, M * T * Z, Y, X)
