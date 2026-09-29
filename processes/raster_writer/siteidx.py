import os

from PyQt5.QtCore import *
from PyQt5.QtGui import *
from PyQt5.QtWidgets import *
from qgis.core import *
from qgis.gui import *
from qgis.analysis import QgsRasterCalculator, QgsRasterCalculatorEntry

from .utils import adjust_extent_and_resolution
from ...settings_manager import SettingsManager
from ...constants import (
    OUTPUT_SITEIDX_SUGI,
    OUTPUT_SITEIDX_HINOKI,
    OUTPUT_SITEIDX_KARAMATSU
)


def generate(basis_dem_filepath: str,
             npp_filepath: str,
             srad_filepath: str,
             vtex_filepath: str,
             output_dir: str) -> list:
    """
    基準DEMと子要素3ラスターから地位指数スコアラスター群を生成する

    Args:
        basis_dem_filepath (str)
        npp_filepath (str)
        srad_filepath (str)
        vtex_filepath (str)
        output_dir (str)

    Returns:
        list: 生成されたラスター群のパスの配列, sugi, hinoki, karamatsuの順
    """

    # 基準DEMと同じ領域・解像度で切り抜き
    adjusted_npp_filepath = adjust_extent_and_resolution(basis_dem_filepath,
                                                         npp_filepath,
                                                         resampling_alg_name="nearest")
    adjusted_srad_filepath = adjust_extent_and_resolution(basis_dem_filepath,
                                                          srad_filepath,
                                                          resampling_alg_name="nearest")
    adjusted_vtex_filepath = adjust_extent_and_resolution(basis_dem_filepath,
                                                          vtex_filepath,
                                                          resampling_alg_name="nearest")

    # ラスター計算のためにEntry生成
    npp_rlayer = QgsRasterLayer(adjusted_npp_filepath)
    npp_entry = QgsRasterCalculatorEntry()
    npp_entry.ref = 'npp@1'
    npp_entry.raster = npp_rlayer
    npp_entry.bandNumber = 1

    srad_rlayer = QgsRasterLayer(adjusted_srad_filepath)
    srad_entry = QgsRasterCalculatorEntry()
    srad_entry.ref = 'srad@1'
    srad_entry.raster = srad_rlayer
    srad_entry.bandNumber = 1

    vtex_rlayer = QgsRasterLayer(adjusted_vtex_filepath)
    vtex_entry = QgsRasterCalculatorEntry()
    vtex_entry.ref = 'vtex@1'
    vtex_entry.raster = vtex_rlayer
    vtex_entry.bandNumber = 1

    dem_rlayer = QgsRasterLayer(basis_dem_filepath)
    dem_entry = QgsRasterCalculatorEntry()
    dem_entry.ref = 'dem@1'
    dem_entry.raster = dem_rlayer
    dem_entry.bandNumber = 1

    # 地位指数パラメータを設定値から取得
    smanager = SettingsManager()
    settings = smanager.get_settings()

    # ラスター計算実行
    output_sugi_filepath = os.path.join(output_dir,
                                        OUTPUT_SITEIDX_SUGI["FILE_NAME"] + ".tif")
    output_hinoki_filepath = os.path.join(output_dir,
                                          OUTPUT_SITEIDX_HINOKI["FILE_NAME"] + ".tif")
    output_karamatsu_filepath = os.path.join(output_dir,
                                             OUTPUT_SITEIDX_KARAMATSU["FILE_NAME"] + ".tif")

    for output_filepath, siteidx_params in (
        (output_sugi_filepath, settings["siteidx_sugi_params"]),
        (output_hinoki_filepath, settings["siteidx_hinoki_params"]),
        (output_karamatsu_filepath, settings["siteidx_karamatsu_params"]),
    ):
        # 地位指数 = 定数 + (NPP - NPP1) * NPP2 - (SRAD - SRAD1) * 0.01 * SRAD2 - (VTEX - VTEX1) * 0.01 * VTEX2
        # DEMのNo-DATAの部分は結果でもNo-DATAにするために、expressionに *(dem@1 AND 1) を使う
        expression = f'({siteidx_params[0]} + (npp@1 - {siteidx_params[1]}) * {siteidx_params[2]} - (srad@1 - {siteidx_params[3]}) * 0.01 * {siteidx_params[4]} - (vtex@1 - {siteidx_params[5]}) * 0.01 * {siteidx_params[6]}) * (dem@1 AND 1)'
        calc = QgsRasterCalculator(expression,
                                   output_filepath,
                                   'GTiff',
                                   dem_rlayer.extent(),
                                   dem_rlayer.width(),
                                   dem_rlayer.height(),
                                  (npp_entry, srad_entry, vtex_entry, dem_entry))
        calc.processCalculation()

    return (output_sugi_filepath, output_hinoki_filepath, output_karamatsu_filepath)
