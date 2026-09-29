import os

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
             output_dir: str) -> str:
    """
    線分への距離ラスターを生成する
    """
    basis_dem_info = get_tiff_info(basis_dem_filepath)

    line_vector_extent = processing.run("qgis:polygonfromlayerextent", {
        "INPUT": line_vector_filepath,
        "OUTPUT": "TEMPORARY_OUTPUT"
    })["OUTPUT"].extent()

    # DEMと線分を覆うEXTENTを計算する
    x_min = min(basis_dem_info["extent"][0], line_vector_extent.xMinimum())
    x_max = max(basis_dem_info["extent"][1], line_vector_extent.xMaximum())
    y_min = min(basis_dem_info["extent"][2], line_vector_extent.yMinimum())
    y_max = max(basis_dem_info["extent"][3], line_vector_extent.yMaximum())

    line_raster_filepath = processing.run("gdal:rasterize", {
        "INPUT": line_vector_filepath,
        "BURN": 1.0,
        "NODATA": 0,
        "UNITS": 1,  # georeferenced unit
        "WIDTH": basis_dem_info["resolution"],
        "HEIGHT": basis_dem_info["resolution"],
        "EXTENT": line_vector_extent,
        "OUTPUT": "TEMPORARY_OUTPUT",
    })["OUTPUT"]

    distance_filepath = processing.run("grass7:r.grow.distance", {
        "input": line_raster_filepath,
        "-": False,
        "-m": True,
        "GRASS_REGION_PARAMETER": f'{x_min},{x_max},{y_min},{y_max}',
        "GRASS_REGION_CELLSIZE_PARAMETER": basis_dem_info["resolution"],
        "distance": "TEMPORARY_OUTPUT",
        "value": "TEMPORARY_OUTPUT",
        "metric": 0  # 値はメートル単位で焼き込まれる
    })['distance']

    adjusted_dis_filepath = adjust_extent_and_resolution(basis_dem_filepath,
                                                         distance_filepath,
                                                         resampling_alg_name="nearest")

    # DEMのNo-DATAの部分は結果でもNo-DATAにするために、expressionに *(dem@1 AND 1) を使う
    distance_rlayer = QgsRasterLayer(adjusted_dis_filepath)
    distance_entry = QgsRasterCalculatorEntry()
    distance_entry.ref = 'dis@1'
    distance_entry.raster = distance_rlayer
    distance_entry.bandNumber = 1

    dem_rlayer = QgsRasterLayer(basis_dem_filepath)
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
    calc.processCalculation()

    # remove intermediate files
    os.remove(line_raster_filepath)
    os.remove(distance_filepath)

    return output_filepath
