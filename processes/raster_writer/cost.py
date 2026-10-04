# This file is part of MORIZON NEXT, modified from the original MORIZON (v2.1).
# Modified by Hideharu Masai since 2026-09-29. See the Git history for the changes and their dates.
# Licensed under the GNU General Public License v3. See LICENSE and NOTICE.

import os
import shutil
import tempfile

from qgis.core import QgsRasterLayer
from qgis.analysis import QgsRasterCalculator, QgsRasterCalculatorEntry
import processing

from ...settings_manager import SettingsManager
from ..costcsv_parser import CostcsvParser
from ...constants import OUTPUT_COST
from . import shc
from .utils import replace_with_adjusted_extent_and_resolution, resolve_algorithm_id


def generate(dem_filepath: str, costcsv_filepath: str, output_dir: str, feedback=None) -> str:
    """
    作業システムラスターを生成する

    Args:
        dem_filepath (str):
        costcsv_filepath (str):
        output_dir (str):

    Returns:
        str: 出力ラスターのファイルパス
    """

    temp_dir = tempfile.mkdtemp()
    try:
        # 起伏量か地形の複雑さのいずれかを、傾斜量と対になるデータelement_filepathとして出力(#193)
        if SettingsManager().get_setting("cost_algorithm") == "ruggedness":
            element_filepath = _generate_ruggedness(
                dem_filepath, os.path.join(temp_dir, "ruggedness.tif"), feedback=feedback
            )
        else:
            shc_dir = os.path.join(temp_dir, "shc")
            os.makedirs(shc_dir, exist_ok=True)
            element_filepath = shc.generate(dem_filepath, shc_dir, feedback=feedback)

        slope_filepath = os.path.join(temp_dir, "slope.tif")
        processing.run(
            "qgis:slope", {"INPUT": dem_filepath, "OUTPUT": slope_filepath},
            feedback=feedback,
        )
        _assert_raster_ready(slope_filepath, "傾斜")

        # ラスター計算のためにEntry生成
        ele_rlayer = _make_raster_layer(element_filepath, "作業システム地形要素")
        ele_entry = QgsRasterCalculatorEntry()
        ele_entry.ref = "ele@1"
        ele_entry.raster = ele_rlayer
        ele_entry.bandNumber = 1

        slp_rlayer = _make_raster_layer(slope_filepath, "作業システム傾斜")
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
        if feedback is not None:
            # QgsRasterCalculator 自体は ProcessingFeedback を受け取れない。
            feedback.pushInfo("作業システムラスターを計算しています")
        result = calc.processCalculation()
        if not _is_raster_calculator_success(result):
            raise RuntimeError(f"作業システムラスターの計算に失敗しました: {result}")
        _assert_raster_ready(output_filepath, OUTPUT_COST["DISPLAY_NAME"])

        return replace_with_adjusted_extent_and_resolution(
            dem_filepath, output_filepath, feedback=feedback
        )
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


def _generate_ruggedness(dem_filepath: str, output_filepath: str, feedback=None) -> str:
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
    temp_dir = os.path.dirname(output_filepath) or tempfile.gettempdir()
    min_filepath = os.path.join(temp_dir, "ruggedness_min.tif")
    max_filepath = os.path.join(temp_dir, "ruggedness_max.tif")

    processing.run(
        resolve_algorithm_id("grass:r.neighbors", "grass7:r.neighbors"),
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
            "output": min_filepath,
            "quantile": "",
            "selection": None,
            "size": size,
            "weight": "",
        },
        feedback=feedback,
    )
    _assert_raster_ready(min_filepath, "起伏量最小値")

    processing.run(
        resolve_algorithm_id("grass:r.neighbors", "grass7:r.neighbors"),
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
            "output": max_filepath,
            "quantile": "",
            "selection": None,
            "size": size,
            "weight": "",
        },
        feedback=feedback,
    )
    _assert_raster_ready(max_filepath, "起伏量最大値")

    # ラスター計算のためにEntry生成
    min_rlayer = _make_raster_layer(min_filepath, "起伏量最小値")
    min_entry = QgsRasterCalculatorEntry()
    min_entry.ref = "min@1"
    min_entry.raster = min_rlayer
    min_entry.bandNumber = 1

    max_rlayer = _make_raster_layer(max_filepath, "起伏量最大値")
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
    if feedback is not None:
        feedback.pushInfo("起伏量ラスターを計算しています")
    result = calc.processCalculation()
    if not _is_raster_calculator_success(result):
        raise RuntimeError(f"起伏量ラスターの計算に失敗しました: {result}")
    _assert_raster_ready(output_filepath, "起伏量")

    return output_filepath


def _assert_raster_ready(filepath: str, label: str):
    if not filepath or not os.path.exists(filepath):
        raise RuntimeError(f"{label}ラスターを作成できませんでした: {filepath}")
    if os.path.getsize(filepath) == 0:
        raise RuntimeError(f"{label}ラスターが空です: {filepath}")


def _make_raster_layer(filepath: str, label: str) -> QgsRasterLayer:
    _assert_raster_ready(filepath, label)
    layer = QgsRasterLayer(filepath)
    if not layer.isValid():
        raise RuntimeError(f"{label}ラスターを読み込めませんでした: {filepath}")
    return layer


def _is_raster_calculator_success(result) -> bool:
    success = getattr(getattr(QgsRasterCalculator, "Result", QgsRasterCalculator), "Success", 0)
    return result == success or str(result) in ("0", "Result.Success", "Success")
