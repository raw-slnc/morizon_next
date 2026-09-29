import os
import shutil

from qgis.PyQt.QtCore import *
from qgis.PyQt.QtGui import *
from qgis.PyQt.QtWidgets import *
from qgis.core import *
from qgis.gui import *
from qgis.analysis import QgsRasterCalculator, QgsRasterCalculatorEntry
import processing

from ...utils import get_tiff_info
from ...constants import OUTPUT_SAVEAREA


def generate(basis_dem_filepath: str,
             building_filepath: str,
             output_dir: str) -> str:
    """
    保全対象を含む流域ラスターを生成する
    """
    fixed_basin_vlayer = create_basin_polygon(basis_dem_filepath)
    basis_deminfo = get_tiff_info(basis_dem_filepath)

    # すべての流域を焼きこんだラスター
    basin_rasiterized_filepath = processing.run("gdal:rasterize", {
        'INPUT': fixed_basin_vlayer,
        'BURN': 1,
        'DATA_TYPE': 5,  # Float32
        'EXTENT': f'{basis_deminfo["extent"][0]},{basis_deminfo["extent"][1]},{basis_deminfo["extent"][2]},{basis_deminfo["extent"][3]}',
        'EXTRA': '',
        'FIELD': '',
        'INIT': None,
        'INVERT': False,
        'NODATA': None,
        'OPTIONS': '',
        'UNITS': 1,  # 地理単位
        'HEIGHT': basis_deminfo["resolution"],
        'WIDTH': basis_deminfo["resolution"],
        'OUTPUT': "TEMPORARY_OUTPUT",
    })["OUTPUT"]

    fixed_building_vlayer = processing.run("native:fixgeometries", {
        "INPUT": building_filepath,
        "OUTPUT": "TEMPORARY_OUTPUT"
    })["OUTPUT"]

    overlap_calculated_polygon_vlayer = processing.run("qgis:calculatevectoroverlaps", {
        "INPUT": fixed_basin_vlayer,
        "LAYERS": [fixed_building_vlayer],
        "OUTPUT": "TEMPORARY_OUTPUT"
    })["OUTPUT"]

    filtered_polygon_vlayer = processing.run("qgis:extractbyexpression", {
        "INPUT": overlap_calculated_polygon_vlayer,
        "EXPRESSION": f'\"{fixed_building_vlayer.name()}_area\" > 0',
        "OUTPUT": "TEMPORARY_OUTPUT"
    })["OUTPUT"]

    # 建物ポリゴンを含む流域だけを焼きこんだラスター
    filtered_rasterized_filepath = processing.run("gdal:rasterize", {
        'INPUT': filtered_polygon_vlayer,
        'BURN': 1,
        'DATA_TYPE': 5,  # Float32
        'EXTENT': f'{basis_deminfo["extent"][0]},{basis_deminfo["extent"][1]},{basis_deminfo["extent"][2]},{basis_deminfo["extent"][3]}',
        'EXTRA': '',
        'FIELD': '',
        'INIT': None,
        'INVERT': False,
        'NODATA': None,
        'OPTIONS': '',
        'UNITS': 1,  # 地理単位
        'HEIGHT': basis_deminfo["resolution"],
        'WIDTH': basis_deminfo["resolution"],
        'OUTPUT': "TEMPORARY_OUTPUT",
    })["OUTPUT"]

    output_filepath = os.path.join(
        output_dir, OUTPUT_SAVEAREA['FILE_NAME'] + ".tif")

    # ラスター計算のためにEntry生成
    basin_rasterized_rlayer = QgsRasterLayer(basin_rasiterized_filepath)
    basin_rasterized_entry = QgsRasterCalculatorEntry()
    basin_rasterized_entry.ref = "basin_rasterized@1"
    basin_rasterized_entry.raster = basin_rasterized_rlayer
    basin_rasterized_entry.bandNumber = 1

    filtered_rasterized_rlayer = QgsRasterLayer(filtered_rasterized_filepath)
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
    calc.processCalculation()

    return output_filepath


def create_basin_polygon(basis_dem_filepath):
    """DEMを利用して流域ポリゴンを生成する。必要ならジオメトリの修復を試みる。"""
    basin_filepath = processing.run("grass7:r.watershed", {
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
        'basin': 'TEMPORARY_OUTPUT',
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
    })['basin']

    vectorized_basin_filepath = processing.run("gdal:polygonize", {
        "INPUT": basin_filepath,
        "BAND": 1,
        "OUTPUT": "TEMPORARY_OUTPUT"
    })["OUTPUT"]
    fixed_basin_vlayer = processing.run("native:fixgeometries", {
        "INPUT": vectorized_basin_filepath,
        "OUTPUT": "TEMPORARY_OUTPUT"
    })["OUTPUT"]
    return fixed_basin_vlayer


def dissolve_basin_vlayer(basin_vlayer_filepath):
    """流域ポリゴンをDNフィルドで"dissolveする"""
    return processing.run("native:dissolve", {
        'FIELD': ['DN'],
        'INPUT': basin_vlayer_filepath,
        'OUTPUT': 'TEMPORARY_OUTPUT'
    })["OUTPUT"]
