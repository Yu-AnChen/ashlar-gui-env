#!/usr/bin/env python3
import argparse
import logging
from argparse import ArgumentParser as AP
import os
from os.path import splitext
from pathlib import Path

import dask
import numpy as np
import bioio_bioformats
from basicpy import BaSiC
import scyjava
import tifffile


logging.basicConfig(
    format="%(asctime)s.%(msecs)03d %(name)-20s %(levelname)-8s : %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    level=logging.WARNING,
    force=True,
)
logger = logging.getLogger("basicpy-docker-mcmicro")
logger.setLevel(logging.INFO)


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


def main(args):

    # Run BASIC
    basic = BaSiC(
        smoothness_flatfield=args.smoothness_flatfield,
        smoothness_darkfield=args.smoothness_darkfield,
        sparse_cost_darkfield=args.sparse_cost_darkfield,
        max_reweight_iterations=args.max_reweight_iterations,
        fitting_mode=args.fitting_mode,
        get_darkfield=args.darkfield,
        sort_intensity=args.sort_intensity,
        # resize_mode='skimage_dask',
    )

    # Initialize flatfields and darkfields
    flatfields = []
    darkfields = []

    if 'BASICPY_DOCKER_MCMICRO' in os.environ:
        # If we're running inside our own container, configure scyjava to use
        # our custom location for maven and jgo to stash their files. (jgo and
        # maven should already be configured via environment variables)
        # Otherwise leave the scyjava defaults alone so the script is usable
        # outside the container as well.
        container_scyjava_base = Path('/opt/scyjava')
        scyjava.config.set_cache_dir(container_scyjava_base / '.jgo')
        scyjava.config.set_m2_repo(container_scyjava_base / '.m2' / 'repository')

    dask.config.set(scheduler='synchronous')

    # Check if input is a folder or a file
    if args.input.is_file():
        logger.info(f"opening image at {args.input}")
        image = bioio_bioformats.Reader(args.input)
        if image.dims.order not in ('TCZYX', 'MTCZYX'):
            raise RuntimeError(
                f"Unexpected image dimension order: {image.dims.order}"
            )
        istack = image.get_xarray_dask_stack(
            drop_non_matching_scenes=True,
            scene_character='M',
        )
        if istack.dims != ('M', 'T', 'C', 'Z', 'Y', 'X'):
            raise RuntimeError(
                f"Unexpected stack dimension order: {istack.dims}"
            )
        istack = istack.stack(I=('M', 'T', 'Z')).transpose('C', 'I', 'Y', 'X')
        if len(istack.coords['I']) < 2 and not args.ignore_single_image_error:
            raise RuntimeError(
                "The image is single sited. Was it saved in the correct way?"
            )
        for c, channel_stack in enumerate(istack, 1):
            logger.info(f'Begin processing channel {c}')
            channel_data = channel_stack.data
            if not args.no_autotune:
                logger.info('Autotuning parameters')
                channel_data = channel_data.compute()
                basic.autotune(
                    channel_data,
                    fourier_l0_norm_cost_coef=args.autotune_fourier_l0_norm_cost_coef,
                )
            logger.info('Generating illumination correction profiles')
            basic.fit(channel_data)
            flatfields.append(basic.flatfield)
            darkfields.append(basic.darkfield)
            logger.info(f'End processing channel {c}')

    # If input is a folder
    else:
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
            logger.info(f'Begin processing channel {channel + 1}')
            logger.info(f'Total image files to load: {num_images}')
            images_data = []
            for image_path in args.input.iterdir():
                logger.info(f'Opening image {image_path}')
                image = aicsimageio.AICSImage(image_path)
                logger.info(f'Total image fields to load: {len(image.scenes)}')
                for i, scene in enumerate(image.scenes, 1):
                    logger.info(f'Loading field {i}')
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
                logger.info('Autotuning parameters')
                basic.autotune(
                    images_data,
                    fourier_l0_norm_cost_coef=args.autotune_fourier_l0_norm_cost_coef,
                )
            logger.info('Generating illumination correction profiles')
            basic.fit(images_data)
            flatfields.append(basic.flatfield)
            darkfields.append(basic.darkfield)
            logger.info(f'End processing channel {channel}')

    flatfields = np.array(flatfields)
    darkfields = np.array(darkfields)

    # Get output file names, splitext gets the file name without the extension
    flatfield_path = args.output_folder / f'{args.output_flatfield}-ffp.ome.tif'
    darkfield_path = args.output_folder / f'{args.output_darkfield}-dfp.ome.tif'

    # Save flatfields and darkfields
    tf_kwargs = dict(
        photometric='minisblack',
        compression='adobe_deflate',
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