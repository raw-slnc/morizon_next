# This file is part of MORIZON NEXT, modified from the original MORIZON (v2.1).
# Modified by Hideharu Masai since 2026-09-29. See the Git history for the changes and their dates.
# Licensed under the GNU General Public License v3. See LICENSE and NOTICE.

import os

from qgis.core import QgsRasterLayer
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
             output_dir: str,
             feedback=None) -> list:
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
                                                         resampling_alg_name="nearest",
                                                         feedback=feedback)
    adjusted_srad_filepath = adjust_extent_and_resolution(basis_dem_filepath,
                                                          srad_filepath,
                                                          resampling_alg_name="nearest",
                                                          feedback=feedback)
    adjusted_vtex_filepath = adjust_extent_and_resolution(basis_dem_filepath,
                                                          vtex_filepath,
                                                          resampling_alg_name="nearest",
                                                          feedback=feedback)

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
        if feedback is not None:
            # QgsRasterCalculator は ProcessingFeedback を受け取れないため工程を手動通知する。
            feedback.pushInfo(f"地位指数を計算しています: {os.path.basename(output_filepath)}")
        # 地位指数 = 定数 + (NPP - NPP1) * NPP2 - (SRAD - SRAD1) * 0.01 * SRAD2 - (VTEX - VTEX1) * 0.01 * VTEX2
        # DEMのNo-DATAの部分は結果でもNo-DATAにするために、expressionに *(dem@1 AND 1) を使う
        expression = (
            f'({siteidx_params[0]} + (npp@1 - {siteidx_params[1]}) * {siteidx_params[2]}'
            f' - (srad@1 - {siteidx_params[3]}) * 0.01 * {siteidx_params[4]}'
            f' - (vtex@1 - {siteidx_params[5]}) * 0.01 * {siteidx_params[6]}) * (dem@1 AND 1)'
        )
        # 計算に失敗したとき前回の出力が残って使われないよう、先に削除しておく
        if os.path.exists(output_filepath):
            os.remove(output_filepath)
        calc = QgsRasterCalculator(expression,
                                   output_filepath,
                                   'GTiff',
                                   dem_rlayer.extent(),
                                   dem_rlayer.width(),
                                   dem_rlayer.height(),
                                   (npp_entry, srad_entry, vtex_entry, dem_entry))
        result = calc.processCalculation()
        if not _is_raster_calculator_success(result):
            raise RuntimeError(
                f"地位指数ラスターの計算に失敗しました: {result}\n{calc.lastError()}"
            )
        _assert_raster_ready(output_filepath, "地位指数ラスター")

    return (output_sugi_filepath, output_hinoki_filepath, output_karamatsu_filepath)


def _is_raster_calculator_success(result) -> bool:
    success = getattr(getattr(QgsRasterCalculator, "Result", QgsRasterCalculator), "Success", 0)
    return result == success or str(result) in ("0", "Result.Success", "Success")


def _assert_raster_ready(filepath: str, label: str):
    if not filepath or not os.path.exists(filepath):
        raise RuntimeError(f"{label}を作成できませんでした: {filepath}")
    if os.path.getsize(filepath) == 0:
        raise RuntimeError(f"{label}が空です: {filepath}")
