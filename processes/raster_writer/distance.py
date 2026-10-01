import math
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

from .utils import adjust_extent_and_resolution
from ...utils import get_tiff_info
from ...constants import OUTPUT_DISTANCE


def generate(basis_dem_filepath: str,
             line_vector_filepath: str,
             output_dir: str):
    """
    線分への距離ラスターを生成する。line_vector_filepathにフィーチャが
    1件も無い場合（対象範囲に道路データが存在しない等）はNoneを返し、
    呼び出し側でスキップできるようにする。
    """
    line_vlayer = QgsVectorLayer(line_vector_filepath, "line_vector", "ogr")
    if not line_vlayer.isValid():
        raise RuntimeError(f"道路データを読み込めませんでした: {line_vector_filepath}")
    if line_vlayer.featureCount() == 0:
        return None

    basis_dem_info = get_tiff_info(basis_dem_filepath)

    temp_dir = tempfile.mkdtemp()
    try:
        line_vector_for_rasterize = line_vector_filepath
        if line_vlayer.crs().isValid() and line_vlayer.crs() != basis_dem_info["crs"]:
            line_vector_for_rasterize = os.path.join(temp_dir, "network_reprojected.gpkg")
            processing.run("native:reprojectlayer", {
                "INPUT": line_vector_filepath,
                "TARGET_CRS": basis_dem_info["crs"],
                "OUTPUT": line_vector_for_rasterize,
            })
            line_vlayer = QgsVectorLayer(line_vector_for_rasterize, "line_vector_reprojected", "ogr")
            if not line_vlayer.isValid():
                raise RuntimeError(f"道路データをDEM座標系へ変換できませんでした: {line_vector_filepath}")
            if line_vlayer.featureCount() == 0:
                return None

        line_vector_extent = line_vlayer.extent()
        if line_vector_extent.isEmpty():
            return None

        # DEMと線分を覆うEXTENTを計算する
        x_min = min(basis_dem_info["extent"][0], line_vector_extent.xMinimum())
        x_max = max(basis_dem_info["extent"][1], line_vector_extent.xMaximum())
        y_min = min(basis_dem_info["extent"][2], line_vector_extent.yMinimum())
        y_max = max(basis_dem_info["extent"][3], line_vector_extent.yMaximum())
        if x_max <= x_min or y_max <= y_min:
            return None
        x_min, x_max, y_min, y_max = _restrict_region(
            line_vlayer, basis_dem_info, x_min, x_max, y_min, y_max)

        line_raster_filepath = os.path.join(temp_dir, "network.tif")
        distance_filepath = os.path.join(temp_dir, "distance.tif")
        adjusted_dis_filepath = os.path.join(temp_dir, "distance_adjusted.tif")

        processing.run("gdal:rasterize", {
            "INPUT": line_vector_for_rasterize,
            "BURN": 1.0,
            "NODATA": 0,
            "UNITS": 1,  # georeferenced unit
            "WIDTH": basis_dem_info["resolution"],
            "HEIGHT": basis_dem_info["resolution"],
            "EXTENT": f'{x_min},{x_max},{y_min},{y_max}',
            "OUTPUT": line_raster_filepath,
        })
        _assert_raster_ready(line_raster_filepath, "道路ラスタ")

        _generate_distance_raster(line_raster_filepath, distance_filepath)
        _assert_raster_ready(distance_filepath, "道路距離")

        adjusted_dis_filepath = adjust_extent_and_resolution(
            basis_dem_filepath,
            distance_filepath,
            output_filepath=adjusted_dis_filepath,
            resampling_alg_name="nearest")

        # DEMのNo-DATAの部分は結果でもNo-DATAにするために、expressionに *(dem@1 AND 1) を使う
        distance_rlayer = _make_raster_layer(adjusted_dis_filepath, "道路距離")
        distance_entry = QgsRasterCalculatorEntry()
        distance_entry.ref = 'dis@1'
        distance_entry.raster = distance_rlayer
        distance_entry.bandNumber = 1

        dem_rlayer = _make_raster_layer(basis_dem_filepath, "DEM")
        dem_entry = QgsRasterCalculatorEntry()
        dem_entry.ref = 'dem@1'
        dem_entry.raster = dem_rlayer
        dem_entry.bandNumber = 1

        output_filepath = os.path.join(output_dir, OUTPUT_DISTANCE['FILE_NAME'] + ".tif")

        calc = QgsRasterCalculator(f'dis@1 * (dem@1 AND 1)',
                                   output_filepath,
                                   'GTiff',
                                   dem_rlayer.extent(),
                                   dem_rlayer.width(),
                                   dem_rlayer.height(),
                                   (distance_entry, dem_entry))
        result = calc.processCalculation()
        if not _is_raster_calculator_success(result):
            raise RuntimeError(f"道路距離ラスターの計算に失敗しました: {result}")
        _assert_raster_ready(output_filepath, OUTPUT_DISTANCE["DISPLAY_NAME"])

        return output_filepath
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


def _restrict_region(line_vlayer, basis_dem_info, x_min, x_max, y_min, y_max):
    """距離の計算範囲（DEMと道路を覆う範囲）を、DEMの中の結果が変わらない広さまで狭める。

    DEMの中の値は「一番近い道路までの距離」。DEMの中に道路が1本でもあれば、DEMのどのセルからも
    DEMの対角線以内に道路があるので、DEMの縁から対角線より遠い道路が一番近くなることはない。
    そこで「DEM＋対角線（＋道路をマス目にするときのずれ2セル分）」まで狭める。
    道路がDEMにかからない場合は、一番近い道路がどこにあるか分からないので狭めない。

    道路をマス目にする位置が変わると距離もわずかに変わるため、狭めた範囲も元の範囲の左上を起点にした
    同じマス目にそろえる（ラスター化は左上を起点にマス目を切る）"""
    resolution = basis_dem_info["resolution"]
    dem_x_min, dem_x_max, dem_y_min, dem_y_max = basis_dem_info["extent"]
    dem_rect = QgsRectangle(dem_x_min, dem_y_min, dem_x_max, dem_y_max)
    dem_geometry = QgsGeometry.fromRect(dem_rect)
    request = QgsFeatureRequest().setFilterRect(dem_rect).setNoAttributes()
    if not any(
        feature.hasGeometry() and feature.geometry().intersects(dem_geometry)
        for feature in line_vlayer.getFeatures(request)
    ):
        return x_min, x_max, y_min, y_max

    margin = math.hypot(dem_x_max - dem_x_min, dem_y_max - dem_y_min) + 2 * resolution
    want_x_min = max(x_min, dem_x_min - margin)
    want_x_max = min(x_max, dem_x_max + margin)
    want_y_min = max(y_min, dem_y_min - margin)
    want_y_max = min(y_max, dem_y_max + margin)

    # 元の左上（x_min, y_max）を起点にしたマス目の線に、外側へ合わせる
    new_x_min = x_min + math.floor((want_x_min - x_min) / resolution) * resolution
    new_y_max = y_max - math.floor((y_max - want_y_max) / resolution) * resolution
    new_x_max = new_x_min + math.ceil((want_x_max - new_x_min) / resolution) * resolution
    new_y_min = new_y_max - math.ceil((new_y_max - want_y_min) / resolution) * resolution
    return new_x_min, min(new_x_max, x_max), max(new_y_min, y_min), new_y_max


def _assert_raster_ready(filepath: str, label: str):
    if not filepath or not os.path.exists(filepath):
        raise RuntimeError(f"{label}を作成できませんでした: {filepath}")
    if os.path.getsize(filepath) == 0:
        raise RuntimeError(f"{label}が空です: {filepath}")


def _generate_distance_raster(line_raster_filepath: str, distance_filepath: str):
    """道路ラスタから、各セルの中心から一番近い道路のセルの中心までの水平距離（m）のラスターを作る。

    原版は GRASS の r.grow.distance を使っていたが、近い道路を探す計算が近似で、一番近い道路を見落として
    少し遠い道路で測るセルがある（サンプルの1m DEMで約5%のセルが最大7.3m遠く出た）。GDAL の proximity は
    厳密な距離（scipy の距離変換）とほぼ一致し（外れは0.1%のセルで0.21m以内）、速いので GDAL で計算する"""
    processing.run("gdal:proximity", {
        "INPUT": line_raster_filepath,
        "BAND": 1,
        "VALUES": "1",
        "UNITS": 1,  # georeferenced unit
        "MAX_DISTANCE": 0,
        "REPLACE": 0,
        "NODATA": -3.40282347e+38,
        "OPTIONS": "",
        "CREATION_OPTIONS": "",
        "EXTRA": "",
        "DATA_TYPE": 5,
        "OUTPUT": distance_filepath,
    })


def _make_raster_layer(filepath: str, label: str) -> QgsRasterLayer:
    _assert_raster_ready(filepath, label)
    layer = QgsRasterLayer(filepath)
    if not layer.isValid():
        raise RuntimeError(f"{label}を読み込めませんでした: {filepath}")
    return layer


def _is_raster_calculator_success(result) -> bool:
    success = getattr(getattr(QgsRasterCalculator, "Result", QgsRasterCalculator), "Success", 0)
    return result == success or str(result) in ("0", "Result.Success", "Success")
