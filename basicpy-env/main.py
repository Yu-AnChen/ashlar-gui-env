#!/usr/bin/env python3
import argparse
import logging
from argparse import ArgumentParser as AP
from os.path import splitext
from pathlib import Path

import cv2
import jpype
import numpy as np
import scyjava
import tifffile
import torch
import torch.nn.functional as F
from basicpy import BaSiC

scyjava.config.add_option("-Xmx6g")

logging.basicConfig(
    format="%(asctime)s.%(msecs)03d %(name)-20s %(levelname)-8s : %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    level=logging.WARNING,
    force=True,
)
logger = logging.getLogger("basicpy-docker-mcmicro")
logger.setLevel(logging.INFO)

# Path to the vendored Bio-Formats uber-jar, fetched next to this script by
# populate_scyjava_cache.py (`pixi run setup-basicpy` locally; a build step in the
# container). Resolves to ./jars locally and /opt/jars in the image.
BIOFORMATS_JAR = Path(__file__).resolve().parent / "jars" / "bioformats_package.jar"


def ensure_bioformats():
    """Put the vendored Bio-Formats uber-jar on the JVM classpath and start the
    JVM, before any bioio_bioformats.Reader is constructed. Once the JVM is
    running, bioio_bioformats' own scyjava/jgo/maven resolution becomes a no-op,
    sidestepping its fragile transitive-dependency download (which otherwise
    fails at runtime with NoClassDefFoundError).
    """
    if scyjava.jvm_started():
        return
    if not BIOFORMATS_JAR.exists():
        raise RuntimeError(
            f"Bio-Formats jar not found at {BIOFORMATS_JAR}.\n"
            "Fetch it first with `pixi run setup-basicpy` (or run "
            "populate_scyjava_cache.py)."
        )
    scyjava.config.add_classpath(str(BIOFORMATS_JAR))
    scyjava.start_jvm()


# OME-TIFF filename suffixes whose plane layout we've verified to map correctly through
# TifffileReader. Only these use the (much faster on network drives) tifffile pixel path
# under --reader auto; everything else falls back to Bio-Formats.
TRUSTED_OMETIFF_SUFFIXES = (".pysed.ome.tif",)


def quiet_loci(level: str = "ERROR") -> None:
    """Turn down Bio-Formats' very chatty logback output. Call after the JVM starts."""
    DebugTools = jpype.JPackage("loci").common.DebugTools
    try:
        DebugTools.setRootLevel(level)
    except Exception:  # older Bio-Formats fallback
        DebugTools.enableLogging(level)


def _pixtype2dtype(reader) -> np.dtype:
    """loci PixelType int -> numpy dtype (mirrors bioio_bioformats.utils._pixtype2dtype)."""
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

    series == FOV, channels live within a series. Provides both metadata and pixels.
    """

    def __init__(self, path: str):
        self.r = jpype.JPackage("loci").formats.ImageReader()
        self.r.setId(str(path))
        if int(self.r.getRGBChannelCount()) > 1 or bool(self.r.isInterleaved()):
            raise RuntimeError(
                "RGB/interleaved planes are not supported; expected one grayscale "
                "sample per plane."
            )
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


class TifffileReader:
    """Read pixels via tifffile while taking structural metadata from Bio-Formats.

    Reads from tifffile's flat page list (all IFDs in file order); the global page index
    is ``series * planes_per_series + getIndex(z, c, t)`` where the within-series order
    comes from Bio-Formats (`meta`) so it matches the file's dimension order. Only the
    pixel reads use tifffile; counts/shape/dtype come from `meta`.
    """

    def __init__(self, path: str, meta: "BioformatsReader"):
        self.tif = tifffile.TiffFile(str(path))
        self.pages = self.tif.pages  # flat IFD list, file order
        self.n_series = meta.n_series
        self.size_c = meta.size_c
        self.size_z = meta.size_z
        self.size_t = meta.size_t
        self.height, self.width = meta.height, meta.width
        self.dtype = meta.dtype
        self._planes_per_series = self.size_c * self.size_z * self.size_t
        # Precompute the within-series plane order from Bio-Formats (no JNI in the loop).
        self._local = {
            (z, c, t): int(meta.r.getIndex(z, c, t))
            for z in range(self.size_z)
            for c in range(self.size_c)
            for t in range(self.size_t)
        }
        expected = self.n_series * self._planes_per_series
        if len(self.pages) < expected:
            raise RuntimeError(
                f"tifffile sees {len(self.pages)} pages but Bio-Formats implies "
                f"{self.n_series}*{self._planes_per_series}={expected}; page layout not "
                "understood."
            )

    def read_plane(self, series: int, c: int, z: int = 0, t: int = 0) -> np.ndarray:
        idx = series * self._planes_per_series + self._local[(z, c, t)]
        return self.pages[idx].asarray()

    def close(self):
        self.tif.close()


def verify_plane_mapping(tiff_reader, meta, k: int = 6) -> None:
    """Spot-check that TifffileReader and Bio-Formats return identical pixels for a
    spread of planes, so a wrong plane mapping fails loudly instead of mis-slicing."""
    series = sorted({0, meta.n_series - 1})
    channels = sorted({0, meta.size_c - 1})
    checks = [(s, c, 0, 0) for s in series for c in channels][:k]
    for s, c, z, t in checks:
        if not np.array_equal(
            tiff_reader.read_plane(s, c, z, t), meta.read_plane(s, c, z, t)
        ):
            raise RuntimeError(
                "tifffile vs Bio-Formats plane mismatch at "
                f"(series={s}, c={c}, z={z}, t={t}); refusing to use the tifffile reader."
            )


def build_reader(path, args):
    """Open a pixel reader for `path`. Metadata always comes from Bio-Formats; the
    tifffile pixel path is used only for trusted OME-TIFF formats (or when forced),
    after verifying its plane mapping against Bio-Formats."""
    name = path.name.lower()
    trusted = name.endswith(TRUSTED_OMETIFF_SUFFIXES)
    use_tifffile = args.reader == "tifffile" or (args.reader == "auto" and trusted)

    meta = BioformatsReader(str(path))
    if not use_tifffile:
        logger.info("Reading pixels via Bio-Formats")
        return meta
    logger.info("Reading pixels via tifffile (metadata via Bio-Formats)")
    reader = TifffileReader(str(path), meta)
    verify_plane_mapping(reader, meta)
    meta.close()
    return reader


def get_args():
    # Script description
    description = """Calculate the flatfield and darkfield of a RAW image using the BaSiC algorithm."""

    # Add parser
    parser = AP(
        description=description, formatter_class=argparse.RawDescriptionHelpFormatter
    )

    # Sections
    inputs = parser.add_argument_group(
        title="Required Input", description="Paths to required inputs"
    )

    inputs.add_argument(
        "-i",
        "--input",
        dest="input",
        action="store",
        required=True,
        help="Path to input file",
    )

    output = parser.add_argument_group(
        title="Output", description="Paths to output file"
    )
    output.add_argument(
        "-o",
        "--output_folder",
        dest="output_folder",
        action="store",
        required=True,
        help="Path to output folder",
    )

    optional = parser.add_argument_group(
        title="Optional Input for the tool",
        description="Optional arguments for the tool",
    )
    optional.add_argument(
        "-sf",
        "--smoothness_flatfield",
        dest="smoothness_flatfield",
        action="store",
        required=False,
        type=float,
        default=2.5,
        help="Larger value makes the flatfield smoother.",
    )
    optional.add_argument(
        "-sd",
        "--smoothness_darkfield",
        dest="smoothness_darkfield",
        action="store",
        required=False,
        type=float,
        default=5.0,
        help="Larger value makes the darkfield smoother.",
    )
    optional.add_argument(
        "-sc",
        "--sparse_cost_darkfield",
        dest="sparse_cost_darkfield",
        action="store",
        required=False,
        type=float,
        default=0.01,
        help="Larger value encorages the darkfield sparseness.",
    )
    optional.add_argument(
        "-mi",
        "--max_reweight_iterations",
        dest="max_reweight_iterations",
        action="store",
        required=False,
        type=int,
        default=20,
        help="Maximum number of reweighting iterations.",
    )
    optional.add_argument(
        "-df",
        "--darkfield",
        dest="darkfield",
        action="store_true",
        required=False,
        default=False,
        help="Flag to calculate the darkfield [default=False].",
    )
    optional.add_argument(
        "-na",
        "--no_autotune",
        dest="no_autotune",
        action="store_false",
        required=False,
        default=True,
        help="Flag to autotune the parameters [default=True].",
    )
    optional.add_argument(
        "-ie",
        "--ignore_single_image_error",
        dest="ignore_single_image_error",
        action="store_true",
        required=False,
        default=False,
        help="Ignore error for single-sited image [default=False].",
    )
    optional.add_argument(
        "-f",
        "--fitting_mode",
        dest="fitting_mode",
        choices=["ladmap", "approximate"],
        action="store",
        required=False,
        default="ladmap",
        help="Fitting mode to use, ladmap or approximate [default = 'ladmap'].",
    )
    optional.add_argument(
        "-d",
        "--device",
        dest="device",
        choices=["cpu", "gpu"],
        action="store",
        required=False,
        default="cpu",
        help="Device to use, cpu or gpu [default = 'cpu'].",
    )
    optional.add_argument(
        "-s",
        "--sort_intensity",
        action="store_true",
        dest="sort_intensity",
        required=False,
        default=False,
        help="If True, sort the intensity pixelwise (suitable for non-timelapse images).",
    )
    optional.add_argument(
        "--fourier_l0_norm_cost_coef",
        dest="autotune_fourier_l0_norm_cost_coef",
        action="store",
        required=False,
        type=float,
        default=1e4,
        help="Relative weight of the l0 norm cost in the Fourier domain for autotuning.",
    )
    optional.add_argument(
        "--reader",
        dest="reader",
        choices=["auto", "bioformats", "tifffile"],
        action="store",
        required=False,
        default="auto",
        help="Pixel-read backend for single-file input. 'auto' uses tifffile only for "
        f"trusted OME-TIFF formats ({', '.join(TRUSTED_OMETIFF_SUFFIXES)}) and Bio-Formats "
        "otherwise; 'tifffile'/'bioformats' force the backend [default='auto'].",
    )
    optional.add_argument(
        "--output-flatfield",
        dest="output_flatfield",
        required=False,
        default=None,
        help="Filename for flatfield output. If empty will default to {input filename}. A sufix will be added to differenciate between flatfield and darkfield.",
    )
    optional.add_argument(
        "--output-darkfield",
        dest="output_darkfield",
        required=False,
        default=None,
        help="Filename for darkfield output. If empty will default to {input filename}. A sufix will be added to differenciate between flatfield and darkfield.",
    )

    arg = parser.parse_args()

    # Convert input and output to Pathlib
    arg.input = Path(arg.input)
    arg.output_folder = Path(arg.output_folder)

    if arg.output_flatfield is None:
        arg.output_flatfield = splitext(arg.input.name)[0]

    if arg.output_darkfield is None:
        arg.output_darkfield = splitext(arg.input.name)[0]

    return arg


def _resize_back(img, height, width):

    return F.interpolate(
        torch.from_numpy(img)[None, None],  # (1, 1, H, W)
        size=(height, width),
        mode="bilinear",
        align_corners=True,  # match BaSiCPy's own call
    )[0, 0].numpy()


def main(args):

    # Put the vendored Bio-Formats uber-jar on the classpath before any Reader
    # is constructed (sidesteps bioio_bioformats' fragile jgo/maven download).
    ensure_bioformats()
    quiet_loci()

    # Run BASIC
    basic = BaSiC(
        smoothness_flatfield=args.smoothness_flatfield,
        smoothness_darkfield=args.smoothness_darkfield,
        sparse_cost_darkfield=args.sparse_cost_darkfield,
        max_reweight_iterations=args.max_reweight_iterations,
        fitting_mode=args.fitting_mode,
        get_darkfield=args.darkfield,
        sort_intensity=args.sort_intensity,
    )

    # Initialize flatfields and darkfields
    flatfields = []
    darkfields = []

    # Check if input is a folder or a file
    if args.input.is_file():
        logger.info(f"opening image at {args.input}")
        reader = build_reader(args.input, args)
        try:
            planes_idx = [
                (s, z, t)
                for s in range(reader.n_series)
                for z in range(reader.size_z)
                for t in range(reader.size_t)
            ]
            if len(planes_idx) < 2 and not args.ignore_single_image_error:
                raise RuntimeError(
                    "The image is single sited. Was it saved in the correct way?"
                )
            H, W = reader.height, reader.width
            n_planes = len(planes_idx)
            for c in range(reader.size_c):
                logger.info(f"Begin processing channel {c + 1}/{reader.size_c}")
                logger.info(f"Reading and downsizing {n_planes} planes")
                channel_data = np.stack(
                    [
                        cv2.resize(
                            reader.read_plane(s, c, z, t),
                            dsize=(128, 128),
                            interpolation=cv2.INTER_AREA,
                        )
                        for s, z, t in planes_idx
                    ]
                )

                if not args.no_autotune:
                    logger.info("Autotuning parameters")
                    basic.autotune(
                        channel_data,
                        fourier_l0_norm_cost_coef=args.autotune_fourier_l0_norm_cost_coef,
                    )
                logger.info("Generating illumination correction profiles")
                basic.fit(channel_data)
                flatfields.append(_resize_back(basic.flatfield, H, W))
                darkfields.append(_resize_back(basic.darkfield, H, W))
                logger.info(f"End processing channel {c + 1}/{reader.size_c}")
        finally:
            reader.close()

    # If input is a folder
    else:
        import aicsimageio

        images_data = None
        channels = None
        num_images = 0
        for image_path in args.input.iterdir():
            logger.info(f"opening images at {image_path}")
            image = aicsimageio.AICSImage(image_path)
            num_images += 1
            if channels is None:
                channels = image.channel_names
                images_data = [[] * len(channels)]
            else:
                assert channels == image.channel_names
        for channel in range(len(channels)):
            logger.info(f"Begin processing channel {channel + 1}")
            logger.info(f"Total image files to load: {num_images}")
            images_data = []
            for image_path in args.input.iterdir():
                logger.info(f"Opening image {image_path}")
                image = aicsimageio.AICSImage(image_path)
                logger.info(f"Total image fields to load: {len(image.scenes)}")
                for i, scene in enumerate(image.scenes, 1):
                    logger.info(f"Loading field {i}")
                    image.set_scene(scene)
                    images_data.append(image.get_image_data("MTZYX", C=channel))
            images_data = np.array(images_data).reshape(
                [-1, *images_data[0].shape[-2:]]
            )
            if images_data.shape[0] < 2 and not args.ignore_single_image_error:
                raise RuntimeError(
                    "The image is single sited. Was it saved in the correct way?"
                )
            if not args.no_autotune:
                logger.info("Autotuning parameters")
                basic.autotune(
                    images_data,
                    fourier_l0_norm_cost_coef=args.autotune_fourier_l0_norm_cost_coef,
                )
            logger.info("Generating illumination correction profiles")
            basic.fit(images_data)
            flatfields.append(basic.flatfield)
            darkfields.append(basic.darkfield)
            logger.info(f"End processing channel {channel}")

    flatfields = np.array(flatfields)
    darkfields = np.array(darkfields)

    # Get output file names, splitext gets the file name without the extension
    flatfield_path = args.output_folder / f"{args.output_flatfield}-ffp.ome.tif"
    darkfield_path = args.output_folder / f"{args.output_darkfield}-dfp.ome.tif"

    # Save flatfields and darkfields
    tf_kwargs = dict(
        photometric="minisblack",
        compression="adobe_deflate",
        predictor=False,
        ome=True,
    )
    tifffile.imwrite(flatfield_path, flatfields, **tf_kwargs)
    tifffile.imwrite(darkfield_path, darkfields, **tf_kwargs)


if __name__ == "__main__":
    # Import arguments
    args = get_args()

    # Run main and check time
    main(args)
