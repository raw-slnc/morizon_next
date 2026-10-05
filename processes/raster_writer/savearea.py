# This file is part of MORIZON NEXT, modified from the original MORIZON (v2.1).
# Modified by Hideharu Masai since 2026-09-29. See the Git history for the changes and their dates.
# Licensed under the GNU General Public License v3. See LICENSE and NOTICE.

import os
import shutil
import tempfile

from qgis.core import QgsRasterLayer, QgsVectorLayer
from qgis.analysis import QgsRasterCalculator, QgsRasterCalculatorEntry
import processing

from ...utils import get_tiff_info
from ...constants import OUTPUT_SAVEAREA
from .utils import resolve_algorithm_id


def generate(basis_dem_filepath: str,
             building_filepath: str,
             output_dir: str,
             feedback=None):
    """
    保全対象を含む流域ラスターを生成し、(出力のパス, 建物が無かったか) を返す。
    building_filepathにフィーチャが1件も無い場合（対象範囲に建物が存在しない等）は、
    道路が無い場合の地利と同じく飛ばさずに、どの流域にも保全対象が無い（流域のある所は全域0）として作る。
    """
    building_vlayer = QgsVectorLayer(building_filepath, "building", "ogr")
    no_building = building_vlayer.featureCount() == 0

    temp_dir = tempfile.mkdtemp()
    try:
        fixed_basin_vlayer = create_basin_polygon(
            basis_dem_filepath, temp_dir, feedback=feedback
        )
        basis_deminfo = get_tiff_info(basis_dem_filepath, feedback=feedback)
        target_extent = ",".join(str(value) for value in basis_deminfo["extent"][:4])

        # すべての流域を焼きこんだラスター
        basin_rasiterized_filepath = os.path.join(temp_dir, "basin_all.tif")
        processing.run("gdal:rasterize", {
            'INPUT': fixed_basin_vlayer,
            'BURN': 1,
            'DATA_TYPE': 5,  # Float32
            'EXTENT': target_extent,
            'EXTRA': '',
            'FIELD': '',
            'INIT': None,
            'INVERT': False,
            'NODATA': None,
            'OPTIONS': '',
            'UNITS': 1,  # 地理単位
            'HEIGHT': basis_deminfo["resolution"],
            'WIDTH': basis_deminfo["resolution"],
            'OUTPUT': basin_rasiterized_filepath,
        }, feedback=feedback)
        _assert_raster_ready(basin_rasiterized_filepath, "流域ラスタ")

        output_filepath = os.path.join(
            output_dir, OUTPUT_SAVEAREA['FILE_NAME'] + ".tif")
        if no_building:
            return _generate_no_building_savearea(basin_rasiterized_filepath, output_filepath, feedback), True

        fixed_building_vlayer = processing.run("native:fixgeometries", {
            "INPUT": building_filepath,
            "OUTPUT": "TEMPORARY_OUTPUT"
        }, feedback=feedback)["OUTPUT"]

        overlap_calculated_polygon_vlayer = processing.run("qgis:calculatevectoroverlaps", {
            "INPUT": fixed_basin_vlayer,
            "LAYERS": [fixed_building_vlayer],
            "OUTPUT": "TEMPORARY_OUTPUT"
        }, feedback=feedback)["OUTPUT"]

        filtered_polygon_vlayer = processing.run("qgis:extractbyexpression", {
            "INPUT": overlap_calculated_polygon_vlayer,
            "EXPRESSION": f'\"{fixed_building_vlayer.name()}_area\" > 0',
            "OUTPUT": "TEMPORARY_OUTPUT"
        }, feedback=feedback)["OUTPUT"]

        # 建物ポリゴンを含む流域だけを焼きこんだラスター。焼きこまないセルは「データなし」でなく 0 にする
        # （NODATA を指定しないと、現行の QGIS では 0 が「データなし」になり、下の計算で建物を含まない流域が
        # 0 でなく「データなし」になっていた。手引 p.88 の手順どおり、データなしの値は 9999 にする）
        filtered_rasterized_filepath = os.path.join(temp_dir, "basin_with_building.tif")
        processing.run("gdal:rasterize", {
            'INPUT': filtered_polygon_vlayer,
            'BURN': 1,
            'DATA_TYPE': 5,  # Float32
            'EXTENT': target_extent,
            'EXTRA': '',
            'FIELD': '',
            'INIT': 0,
            'INVERT': False,
            'NODATA': 9999,
            'OPTIONS': '',
            'UNITS': 1,  # 地理単位
            'HEIGHT': basis_deminfo["resolution"],
            'WIDTH': basis_deminfo["resolution"],
            'OUTPUT': filtered_rasterized_filepath,
        }, feedback=feedback)
        _assert_raster_ready(filtered_rasterized_filepath, "建物流域ラスタ")

        # ラスター計算のためにEntry生成
        basin_rasterized_rlayer = _make_raster_layer(basin_rasiterized_filepath, "流域ラスタ")
        basin_rasterized_entry = QgsRasterCalculatorEntry()
        basin_rasterized_entry.ref = "basin_rasterized@1"
        basin_rasterized_entry.raster = basin_rasterized_rlayer
        basin_rasterized_entry.bandNumber = 1

        filtered_rasterized_rlayer = _make_raster_layer(filtered_rasterized_filepath, "建物流域ラスタ")
        filtered_rasterized_entry = QgsRasterCalculatorEntry()
        filtered_rasterized_entry.ref = "filtered_rasterized@1"
        filtered_rasterized_entry.raster = filtered_rasterized_rlayer
        filtered_rasterized_entry.bandNumber = 1

        """
        判定パターン
        流域ポリゴンが存在しないエリア = No-data
        流域ポリゴンの存在するエリアで、かつ、そのポリゴンが建物ポリゴンを含まないエリア = 0
        建物を含む流域ポリゴンの存在するエリア = 1
        """
        NODATA_VALUE = "-3.40282347e+38"
        calc = QgsRasterCalculator(
            f"""{NODATA_VALUE} * ({basin_rasterized_entry.ref} != 1 AND {filtered_rasterized_entry.ref} != 1) \
                                    + 0 * ({basin_rasterized_entry.ref} = 1 AND {filtered_rasterized_entry.ref} != 1) \
                                    + 1 * ({filtered_rasterized_entry.ref} = 1) \
                                    """,
            output_filepath,
            "GTiff",
            basin_rasterized_rlayer.extent(),
            basin_rasterized_rlayer.width(),
            basin_rasterized_rlayer.height(),
            (basin_rasterized_entry, filtered_rasterized_entry))
        if feedback is not None:
            # QgsRasterCalculator は ProcessingFeedback を受け取れないため工程を手動通知する。
            feedback.pushInfo("保全対象を含む流域ラスターを計算しています")
        result = calc.processCalculation()
        if not _is_raster_calculator_success(result):
            raise RuntimeError(f"保全対象ラスターの計算に失敗しました: {result}")
        _assert_raster_ready(output_filepath, OUTPUT_SAVEAREA["DISPLAY_NAME"])

        return output_filepath, False
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


def _generate_no_building_savearea(basin_rasterized_filepath: str, output_filepath: str, feedback=None) -> str:
    """建物が無いときの保全対象を含む流域ラスター。流域ポリゴンが無いエリア = No-data、
    流域ポリゴンのあるエリア = 0（保全対象を含まない）。判定は建物があるときと同じ"""
    basin_rasterized_rlayer = _make_raster_layer(basin_rasterized_filepath, "流域ラスタ")
    basin_rasterized_entry = QgsRasterCalculatorEntry()
    basin_rasterized_entry.ref = "basin_rasterized@1"
    basin_rasterized_entry.raster = basin_rasterized_rlayer
    basin_rasterized_entry.bandNumber = 1

    NODATA_VALUE = "-3.40282347e+38"
    calc = QgsRasterCalculator(
        f"{NODATA_VALUE} * ({basin_rasterized_entry.ref} != 1) + 0 * ({basin_rasterized_entry.ref} = 1)",
        output_filepath,
        "GTiff",
        basin_rasterized_rlayer.extent(),
        basin_rasterized_rlayer.width(),
        basin_rasterized_rlayer.height(),
        (basin_rasterized_entry,))
    if feedback is not None:
        feedback.pushInfo("保全対象を含む流域ラスターを、建物なし（全域0）として計算しています")
    result = calc.processCalculation()
    if not _is_raster_calculator_success(result):
        raise RuntimeError(f"保全対象ラスターの計算に失敗しました: {result}")
    _assert_raster_ready(output_filepath, OUTPUT_SAVEAREA["DISPLAY_NAME"])
    return output_filepath


def create_basin_polygon(basis_dem_filepath, temp_dir=None, feedback=None):
    """DEMを利用して流域ポリゴンを生成する。必要ならジオメトリの修復を試みる。"""
    owns_temp_dir = temp_dir is None
    if temp_dir is None:
        temp_dir = tempfile.mkdtemp()
    try:
        basin_filepath = os.path.join(temp_dir, "basin.tif")
        processing.run(resolve_algorithm_id("grass:r.watershed", "grass7:r.watershed"), {
            'elevation': basis_dem_filepath,
            '-4': False,
            '-a': False,
            '-b': False,
            '-m': False,
            '-s': False,
            'GRASS_RASTER_FORMAT_META': '',
            'GRASS_RASTER_FORMAT_OPT': '',
            'GRASS_REGION_CELLSIZE_PARAMETER': 0,
            'GRASS_REGION_PARAMETER': None,
            'accumulation': None,
            'basin': basin_filepath,
            'blocking': None,
            'convergence': 5,
            'depression': None,
            'disturbed_land': None,
            'drainage': None,
            'flow': None,
            'half_basin': None,
            'length_slope': None,
            'max_slope_length': None,
            'memory': 300,
            'slope_steepness': None,
            'spi': None,
            'stream': None,
            'tci': None,
            'threshold': 500
        }, feedback=feedback)
        _assert_raster_ready(basin_filepath, "流域")

        vectorized_basin_filepath = os.path.join(temp_dir, "basin.gpkg")
        processing.run("gdal:polygonize", {
            "INPUT": basin_filepath,
            "BAND": 1,
            "OUTPUT": vectorized_basin_filepath
        }, feedback=feedback)
        fixed_basin_vlayer = processing.run("native:fixgeometries", {
            "INPUT": vectorized_basin_filepath,
            "OUTPUT": "TEMPORARY_OUTPUT"
        }, feedback=feedback)["OUTPUT"]
        return fixed_basin_vlayer
    finally:
        if owns_temp_dir:
            shutil.rmtree(temp_dir, ignore_errors=True)


def dissolve_basin_vlayer(basin_vlayer_filepath, feedback=None):
    """流域ポリゴンをDNフィルドで"dissolveする"""
    return processing.run("native:dissolve", {
        'FIELD': ['DN'],
        'INPUT': basin_vlayer_filepath,
        'OUTPUT': 'TEMPORARY_OUTPUT'
    }, feedback=feedback)["OUTPUT"]


def _assert_raster_ready(filepath: str, label: str):
    if not filepath or not os.path.exists(filepath):
        raise RuntimeError(f"{label}を作成できませんでした: {filepath}")
    if os.path.getsize(filepath) == 0:
        raise RuntimeError(f"{label}が空です: {filepath}")


def _make_raster_layer(filepath: str, label: str) -> QgsRasterLayer:
    _assert_raster_ready(filepath, label)
    layer = QgsRasterLayer(filepath)
    if not layer.isValid():
        raise RuntimeError(f"{label}を読み込めませんでした: {filepath}")
    return layer


def _is_raster_calculator_success(result) -> bool:
    success = getattr(getattr(QgsRasterCalculator, "Result", QgsRasterCalculator), "Success", 0)
    return result == success or str(result) in ("0", "Result.Success", "Success")
