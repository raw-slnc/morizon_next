import os
import tempfile

from PyQt5.QtCore import *
from PyQt5.QtGui import *
from PyQt5.QtWidgets import *
from qgis.core import *
from qgis.gui import *
from qgis.analysis import QgsRasterCalculator, QgsRasterCalculatorEntry
import processing

from ...settings_manager import SettingsManager
from ...utils import get_raster_stats
from ...constants import OUTPUT_SHC


def generate(dem_filepath: str, output_dir: str) -> str:
    """
    DEMから地形の複雑性ラスターを生成する
    """
    smoothed_filepath = processing.run("saga:gaussianfilter", {
        "INPUT": dem_filepath,
        "MODE": 1,
        "RADIUS": 12,
        "SIGMA": 3,
        "RESULT": "TEMPORARY_OUTPUT"
    })["RESULT"]

    curvature_filepath = processing.run("saga:slopeaspectcurvature", {
        'ELEVATION': smoothed_filepath,
        'ASPECT': 'TEMPORARY_OUTPUT',
        'C_CROS': 'TEMPORARY_OUTPUT',
        'C_GENE': 'TEMPORARY_OUTPUT',
        'C_LONG': 'TEMPORARY_OUTPUT',
        'C_MAXI': 'TEMPORARY_OUTPUT',
        'C_MINI': 'TEMPORARY_OUTPUT',
        'C_PLAN': 'TEMPORARY_OUTPUT',
        'C_PROF': 'TEMPORARY_OUTPUT',
        'C_ROTO': 'TEMPORARY_OUTPUT',
        'C_TANG': 'TEMPORARY_OUTPUT',
        'C_TOTA': 'TEMPORARY_OUTPUT',
        'SLOPE': 'TEMPORARY_OUTPUT',
        'METHOD': 6,
        'UNIT_ASPECT': 1,
        'UNIT_SLOPE': 1
    })["C_PLAN"]

    # ラスター計算のためにEntry生成
    curvature_rlayer = QgsRasterLayer(curvature_filepath)
    curvature_entry = QgsRasterCalculatorEntry()
    curvature_entry.ref = "curvature@1"
    curvature_entry.raster = curvature_rlayer
    curvature_entry.bandNumber = 1

    # 曲率ラスターから外れ値を除外する(外れ値: 絶対値 > 標準偏差*倍率 ※有効桁数小数点以下第4位まで, 第5位で四捨五入)
    curvature_stddev = round(get_raster_stats(curvature_rlayer)["STD_DEV"], 4)

    NODATA_VALUE = "-3.40282347e+38"
    OUTLIER_THRESHOLD = "3"
    with tempfile.NamedTemporaryFile(delete=False, suffix=".tif") as temp_normalized_curvature:
        calc = QgsRasterCalculator(f'{NODATA_VALUE} * ("{curvature_entry.ref}" < {curvature_stddev} * -{OUTLIER_THRESHOLD} OR {curvature_stddev} * {OUTLIER_THRESHOLD} < "{curvature_entry.ref}") + ("{curvature_entry.ref}" >= {curvature_stddev} * -{OUTLIER_THRESHOLD} AND {curvature_stddev} * {OUTLIER_THRESHOLD} >= "{curvature_entry.ref}") * "{curvature_entry.ref}"',
                                   temp_normalized_curvature.name,
                                   "GTiff",
                                   curvature_rlayer.extent(),
                                   curvature_rlayer.width(),
                                   curvature_rlayer.height(),
                                   (curvature_entry,))
        calc.processCalculation()

        settings_manager = SettingsManager()
        calculation_size = int(settings_manager.get_setting("shc_param"))
        output_filepath = processing.run("grass7:r.neighbors", {
            'input': temp_normalized_curvature.name,
            'output': os.path.join(output_dir, OUTPUT_SHC["FILE_NAME"] + ".tif"),
            '-a': False,
            '-c': True,  # 円状隣接関係を使う
            'GRASS_RASTER_FORMAT_META': '',
            'GRASS_RASTER_FORMAT_OPT': '',
            'GRASS_REGION_CELLSIZE_PARAMETER': 0,
            'GRASS_REGION_PARAMETER': None,
            'gauss': None,
            'method': 6,
            'quantile': '',
            'selection': None,
            'size': calculation_size
        })["output"]

        return output_filepath
