# This file is part of MORIZON NEXT, modified from the original MORIZON (v2.1).
# Modified by Hideharu Masai since 2026-09-29. See the Git history for the changes and their dates.
# Licensed under the GNU General Public License v3. See LICENSE and NOTICE.

import os


from ...constants import OUTPUT_SLOPE
from .utils import replace_with_adjusted_extent_and_resolution
from ...utils import run_processing


def generate(dem_filepath: str, output_dir: str, feedback=None) -> str:
    """
    DEMから傾斜ラスターを生成する
    """
    output_filepath = os.path.join(
        output_dir, OUTPUT_SLOPE['FILE_NAME'] + ".tif")
    run_processing("qgis:slope", {
        "INPUT": dem_filepath,
        "OUTPUT": output_filepath
    }, feedback=feedback)
    return replace_with_adjusted_extent_and_resolution(
        dem_filepath, output_filepath, feedback=feedback
    )
