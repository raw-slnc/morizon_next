import os

import processing

from ...constants import OUTPUT_SLOPE
from .utils import replace_with_adjusted_extent_and_resolution


def generate(dem_filepath: str, output_dir: str) -> str:
    """
    DEMから傾斜ラスターを生成する
    """
    output_filepath = os.path.join(
        output_dir, OUTPUT_SLOPE['FILE_NAME'] + ".tif")
    processing.run("qgis:slope", {
        "INPUT": dem_filepath,
        "OUTPUT": output_filepath
    })
    return replace_with_adjusted_extent_and_resolution(dem_filepath, output_filepath)
