import os
import tempfile

from qgis.PyQt.QtCore import *
from qgis.PyQt.QtGui import *
from qgis.PyQt.QtWidgets import *
from qgis.core import *
from qgis.gui import *
from qgis.analysis import QgsRasterCalculator, QgsRasterCalculatorEntry
import processing

from ...settings_manager import SettingsManager
from ..costcsv_parser import CostcsvParser
from ...constants import OUTPUT_COST
from . import shc


def generate(dem_filepath: str, costcsv_filepath: str, output_dir: str) -> str:
    """
    作業システムラスターを生成する

    Args:
        dem_filepath (str):
        costcsv_filepath (str):
        output_dir (str):

    Returns:
        str: 出力ラスターのファイルパス
    """

    # 起伏量か地形の複雑さのいずれかを、傾斜量と対になるデータelement_filepathとして出力(#193)
    if SettingsManager().get_setting("cost_algorithm") == "ruggedness":
        with tempfile.NamedTemporaryFile(
            delete=False, suffix=".tif"
        ) as temp_ruggedness:
            element_filepath = _generate_ruggedness(dem_filepath, temp_ruggedness.name)
    else:
        temp_shc_dir = tempfile.mkdtemp()
        element_filepath = shc.generate(dem_filepath, temp_shc_dir)

    slope_filepath = processing.run(
        "qgis:slope", {"INPUT": dem_filepath, "OUTPUT": "TEMPORARY_OUTPUT"}
    )["OUTPUT"]

    # ラスター計算のためにEntry生成
    ele_rlayer = QgsRasterLayer(element_filepath)
    ele_entry = QgsRasterCalculatorEntry()
    ele_entry.ref = "ele@1"
    ele_entry.raster = ele_rlayer
    ele_entry.bandNumber = 1

    slp_rlayer = QgsRasterLayer(slope_filepath)
    slp_entry = QgsRasterCalculatorEntry()
    slp_entry.ref = "slp@1"
    slp_entry.raster = slp_rlayer
    slp_entry.bandNumber = 1

    # CSVをもとにExpression文字列を生成
    csv_parser = CostcsvParser(costcsv_filepath)
    expression = csv_parser.generate_expression_for_raster_calculator(
        ele_entry.ref, slp_entry.ref
    )

    output_filepath = os.path.join(output_dir, OUTPUT_COST["FILE_NAME"] + ".tif")
    calc = QgsRasterCalculator(
        expression,
        output_filepath,
        "GTiff",
        ele_rlayer.extent(),
        ele_rlayer.width(),
        ele_rlayer.height(),
        (ele_entry, slp_entry),
    )
    calc.processCalculation()

    return output_filepath


def _generate_ruggedness(dem_filepath: str, output_filepath: str) -> str:
    """
    起伏量ラスターを生成する

    Args:
        dem_filepath (str):
        output_filepath (str):

    Returns:
        str: output_filepath
    """
    settings_manager = SettingsManager()
    size = int(settings_manager.get_setting("ruggedness_param"))

    min_filepath = processing.run(
        "grass7:r.neighbors",
        {
            "-a": False,
            "-c": False,
            "GRASS_RASTER_FORMAT_META": "",
            "GRASS_RASTER_FORMAT_OPT": "",
            "GRASS_REGION_CELLSIZE_PARAMETER": 0,
            "GRASS_REGION_PARAMETER": None,
            "gauss": None,
            "input": dem_filepath,
            "method": 3,  # minimum
            "output": "TEMPORARY_OUTPUT",
            "quantile": "",
            "selection": None,
            "size": size,
            "weight": "",
        },
    )["output"]

    max_filepath = processing.run(
        "grass7:r.neighbors",
        {
            "-a": False,
            "-c": False,
            "GRASS_RASTER_FORMAT_META": "",
            "GRASS_RASTER_FORMAT_OPT": "",
            "GRASS_REGION_CELLSIZE_PARAMETER": 0,
            "GRASS_REGION_PARAMETER": None,
            "gauss": None,
            "input": dem_filepath,
            "method": 4,  # maximum
            "output": "TEMPORARY_OUTPUT",
            "quantile": "",
            "selection": None,
            "size": size,
            "weight": "",
        },
    )["output"]

    # ラスター計算のためにEntry生成
    min_rlayer = QgsRasterLayer(min_filepath)
    min_entry = QgsRasterCalculatorEntry()
    min_entry.ref = "min@1"
    min_entry.raster = min_rlayer
    min_entry.bandNumber = 1

    max_rlayer = QgsRasterLayer(max_filepath)
    max_entry = QgsRasterCalculatorEntry()
    max_entry.ref = "max@1"
    max_entry.raster = max_rlayer
    max_entry.bandNumber = 1

    calc = QgsRasterCalculator(
        "max@1 - min@1",
        output_filepath,
        "GTiff",
        min_rlayer.extent(),
        min_rlayer.width(),
        min_rlayer.height(),
        (min_entry, max_entry),
    )
    calc.processCalculation()

    return output_filepath
