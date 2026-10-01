# This file is part of MORIZON NEXT, modified from the original MORIZON (v2.1).
# Modified by Hideharu Masai since 2026-09-29. See the Git history for the changes and their dates.
# Licensed under the GNU General Public License v3. See LICENSE and NOTICE.

import os
import shutil
import tempfile

import processing
from qgis.core import (
    QgsApplication,
    QgsCoordinateReferenceSystem,
    QgsCoordinateTransform,
    QgsProject,
    QgsRectangle,
)

try:
    from osgeo import gdal
    gdal.UseExceptions()
    HAS_GDAL = True
except ImportError:
    HAS_GDAL = False

from ...utils import (
    get_tiff_info
)


def resolve_algorithm_id(*candidate_ids: str) -> str:
    """
    候補のうち、実際に登録されているアルゴリズムIDを返す
    (プロバイダーIDはQGISのバージョンや構成で異なる: grass7/grass, saga/sagang)
    どれも見つからない場合は先頭の候補を返す
    """
    registry = QgsApplication.processingRegistry()
    for algorithm_id in candidate_ids:
        if registry.algorithmById(algorithm_id) is not None:
            return algorithm_id
    return candidate_ids[0]


def resampling(tiff_filepath: str,
               target_resolution: int,
               output_filepath=None,
               resampling_alg_name="cubicspline") -> str:
    """
    TIFFを指定のZ解像度へリサンプリングする、EXTENTは変更されない
    """
    return processing.run("gdal:translate", {
        "EXTRA": f"-tr {target_resolution} {target_resolution} -r {resampling_alg_name}",
        "INPUT": tiff_filepath,
        "OUTPUT": output_filepath if output_filepath is not None else "TEMPORARY_OUTPUT"
    })["OUTPUT"]


def adjust_extent_and_resolution(basis_tiff_filepath: str,
                                 target_tiff_filepath: str,
                                 output_filepath=None,
                                 resampling_alg_name="cubicspline") -> str:
    """
    任意のラスターを、基準ラスターと同じ領域・解像度に調整して出力する
    """
    basis_deminfo = get_tiff_info(basis_tiff_filepath)
    resampling_alg_dict = {
        "nearest": 0,
        "bilinear": 1,
        "cubicspline": 3
    }
    resampling_alg = resampling_alg_dict.get(resampling_alg_name, 3)

    output = processing.run("gdal:warpreproject", {
        "TARGET_CRS": basis_deminfo["crs"],
        "TARGET_RESOLUTION": basis_deminfo["resolution"],
        "TARGET_EXTENT": f'{basis_deminfo["extent"][0]},{basis_deminfo["extent"][1]},{basis_deminfo["extent"][2]},{basis_deminfo["extent"][3]}',
        "TARGET_EXTENT_CRS": basis_deminfo["crs"],
        "RESAMPLING": resampling_alg,
        "INPUT": target_tiff_filepath,
        "OUTPUT": output_filepath if output_filepath is not None else "TEMPORARY_OUTPUT",
    })["OUTPUT"]
    return output


def replace_with_adjusted_extent_and_resolution(basis_tiff_filepath: str,
                                                target_tiff_filepath: str,
                                                resampling_alg_name="nearest") -> str:
    """
    既存の出力ラスターを、基準ラスターと同じ領域・解像度に揃えて置き換える
    """
    target_dir = os.path.dirname(target_tiff_filepath) or None
    temp_dir = tempfile.mkdtemp(dir=target_dir)
    temp_filepath = os.path.join(temp_dir, "adjusted.tif")
    try:
        adjusted_filepath = adjust_extent_and_resolution(
            basis_tiff_filepath,
            target_tiff_filepath,
            output_filepath=temp_filepath,
            resampling_alg_name=resampling_alg_name,
        )
        os.replace(adjusted_filepath, target_tiff_filepath)
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)
    return target_tiff_filepath


def replace_with_clipped_wgs84_extent(target_tiff_filepath: str,
                                      extent_wgs84) -> str:
    """
    ラスターをWGS84指定範囲でピクセル単位に切り出して置き換える。
    解析途中ではなく、最終成果物を表示・後段入力の範囲へ揃える用途。
    """
    if extent_wgs84 is None:
        return target_tiff_filepath
    if not HAS_GDAL:
        raise RuntimeError("GDAL is not available")

    lon_min, lat_min, lon_max, lat_max = extent_wgs84
    target_info = get_tiff_info(target_tiff_filepath)
    target_crs = target_info["crs"]
    wgs84_crs = QgsCoordinateReferenceSystem("EPSG:4326")
    transform = QgsCoordinateTransform(wgs84_crs, target_crs, QgsProject.instance())
    target_rect = transform.transformBoundingBox(
        QgsRectangle(lon_min, lat_min, lon_max, lat_max)
    )

    target_dir = os.path.dirname(target_tiff_filepath) or None
    temp_dir = tempfile.mkdtemp(dir=target_dir)
    temp_filepath = os.path.join(temp_dir, "clipped.tif")
    try:
        ds = gdal.Translate(
            temp_filepath,
            target_tiff_filepath,
            projWin=[
                target_rect.xMinimum(),
                target_rect.yMaximum(),
                target_rect.xMaximum(),
                target_rect.yMinimum(),
            ],
            creationOptions=["COMPRESS=LZW", "TILED=YES", "BIGTIFF=IF_NEEDED"],
        )
        if ds is None:
            raise RuntimeError("最終範囲でのラスター切り出しに失敗しました")
        ds = None
        os.replace(temp_filepath, target_tiff_filepath)
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)
    return target_tiff_filepath
