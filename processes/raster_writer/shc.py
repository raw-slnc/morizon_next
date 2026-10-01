import os
import shutil
import tempfile

from qgis.PyQt.QtCore import *
from qgis.PyQt.QtGui import *
from qgis.PyQt.QtWidgets import *
from qgis.core import *
from qgis.gui import *
from qgis.analysis import QgsRasterCalculator, QgsRasterCalculatorEntry
import processing

from ...settings_manager import SettingsManager, ShcMethodManager
from ...utils import get_raster_stats
from ...constants import OUTPUT_SHC
from .utils import replace_with_adjusted_extent_and_resolution, resolve_algorithm_id
from . import terrain_numpy


def generate(dem_filepath: str, output_dir: str) -> str:
    """
    DEMから地形の複雑性ラスターを生成する
    """
    temp_dir = tempfile.mkdtemp()
    try:
        if ShcMethodManager().load_use_saga():
            curvature_filepath = _plan_curvature_by_saga(dem_filepath, temp_dir)
        else:
            # 原版の設計どおりの平滑化（σ=3, 半径12セル）と平面曲率をプラグイン内で計算する
            curvature_filepath = terrain_numpy.write_plan_curvature(
                dem_filepath, os.path.join(temp_dir, "curvature_plan.tif")
            )
        _assert_raster_ready(curvature_filepath, "平面曲率")

        # ラスター計算のためにEntry生成
        curvature_rlayer = _make_raster_layer(curvature_filepath, "平面曲率")
        curvature_entry = QgsRasterCalculatorEntry()
        curvature_entry.ref = "curvature@1"
        curvature_entry.raster = curvature_rlayer
        curvature_entry.bandNumber = 1

        # 曲率ラスターから外れ値を除外する(外れ値: 絶対値 > 標準偏差*倍率 ※有効桁数小数点以下第4位まで, 第5位で四捨五入)
        curvature_stddev = round(get_raster_stats(curvature_rlayer)["STD_DEV"], 4)

        NODATA_VALUE = "-3.40282347e+38"
        OUTLIER_THRESHOLD = "3"
        normalized_curvature_filepath = os.path.join(temp_dir, "normalized_curvature.tif")
        calc = QgsRasterCalculator(f'{NODATA_VALUE} * ("{curvature_entry.ref}" < {curvature_stddev} * -{OUTLIER_THRESHOLD} OR {curvature_stddev} * {OUTLIER_THRESHOLD} < "{curvature_entry.ref}") + ("{curvature_entry.ref}" >= {curvature_stddev} * -{OUTLIER_THRESHOLD} AND {curvature_stddev} * {OUTLIER_THRESHOLD} >= "{curvature_entry.ref}") * "{curvature_entry.ref}"',
                                   normalized_curvature_filepath,
                                   "GTiff",
                                   curvature_rlayer.extent(),
                                   curvature_rlayer.width(),
                                   curvature_rlayer.height(),
                                   (curvature_entry,))
        result = calc.processCalculation()
        if not _is_raster_calculator_success(result):
            raise RuntimeError(f"地形の複雑さの外れ値処理に失敗しました: {result}")
        _assert_raster_ready(normalized_curvature_filepath, "外れ値処理済み曲率")

        settings_manager = SettingsManager()
        calculation_size = int(settings_manager.get_setting("shc_param"))
        output_filepath = os.path.join(output_dir, OUTPUT_SHC["FILE_NAME"] + ".tif")
        processing.run(resolve_algorithm_id("grass:r.neighbors", "grass7:r.neighbors"), {
            'input': normalized_curvature_filepath,
            'output': output_filepath,
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
        })
        _assert_raster_ready(output_filepath, OUTPUT_SHC["DISPLAY_NAME"])

        return replace_with_adjusted_extent_and_resolution(dem_filepath, output_filepath)
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


def _plan_curvature_by_saga(dem_filepath: str, temp_dir: str) -> str:
    """従来の SAGA による平滑化・平面曲率（設定タブの「SAGA ON」のときだけ使う）。
    注意: NextGen プロバイダー（SAGA 9）では GaussianFilter のパラメータ名が KERNEL_RADIUS / SIGMA に
    変わっており、ここで渡す MODE / RADIUS は使われない（原版の SAGA 2.3 向けの指定のまま残している）"""
    smoothed_filepath = os.path.join(temp_dir, "smoothed.tif")
    processing.run(resolve_algorithm_id("sagang:gaussianfilter", "saga:gaussianfilter"), {
        "INPUT": dem_filepath,
        "MODE": 1,
        "RADIUS": 12,
        "SIGMA": 3,
        "RESULT": smoothed_filepath
    })
    _assert_raster_ready(smoothed_filepath, "平滑化DEM")

    curvature_outputs = {
        'ASPECT': os.path.join(temp_dir, "aspect.tif"),
        'C_CROS': os.path.join(temp_dir, "curvature_cross_sectional.tif"),
        'C_GENE': os.path.join(temp_dir, "curvature_general.tif"),
        'C_LONG': os.path.join(temp_dir, "curvature_longitudinal.tif"),
        'C_MAXI': os.path.join(temp_dir, "curvature_maximal.tif"),
        'C_MINI': os.path.join(temp_dir, "curvature_minimal.tif"),
        'C_PLAN': os.path.join(temp_dir, "curvature_plan.tif"),
        'C_PROF': os.path.join(temp_dir, "curvature_profile.tif"),
        'C_ROTO': os.path.join(temp_dir, "curvature_rotor.tif"),
        'C_TANG': os.path.join(temp_dir, "curvature_tangential.tif"),
        'C_TOTA': os.path.join(temp_dir, "curvature_total.tif"),
        'SLOPE': os.path.join(temp_dir, "slope.tif"),
    }
    processing.run(resolve_algorithm_id("sagang:slopeaspectcurvature", "saga:slopeaspectcurvature"), {
        'ELEVATION': smoothed_filepath,
        **curvature_outputs,
        'METHOD': 6,
        'UNIT_ASPECT': 1,
        'UNIT_SLOPE': 1
    })
    curvature_filepath = curvature_outputs["C_PLAN"]
    _assert_raster_ready(curvature_filepath, "平面曲率")
    return curvature_filepath


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
