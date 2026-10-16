# This file is part of MORIZON NEXT, modified from the original MORIZON (v2.1).
# Modified by Hideharu Masai since 2026-09-29. See the Git history for the changes and their dates.
# Licensed under the GNU General Public License v3. See LICENSE and NOTICE.

import os
import shutil
import tempfile

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

from ...utils import get_ascii_safe_dir, get_tiff_info, run_processing


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
               resampling_alg_name="cubicspline",
               feedback=None) -> str:
    """
    TIFFを指定のZ解像度へリサンプリングする、EXTENTは変更されない
    """
    return run_processing("gdal:translate", {
        "EXTRA": f"-tr {target_resolution} {target_resolution} -r {resampling_alg_name}",
        "INPUT": tiff_filepath,
        "OUTPUT": output_filepath if output_filepath is not None else "TEMPORARY_OUTPUT"
    }, feedback=feedback)["OUTPUT"]


def adjust_extent_and_resolution(basis_tiff_filepath: str,
                                 target_tiff_filepath: str,
                                 output_filepath=None,
                                 resampling_alg_name="cubicspline",
                                 feedback=None) -> str:
    """
    任意のラスターを、基準ラスターと同じ領域・解像度に調整して出力する
    """
    basis_deminfo = get_tiff_info(basis_tiff_filepath, feedback=feedback)
    resampling_alg_dict = {
        "nearest": 0,
        "bilinear": 1,
        "cubicspline": 3
    }
    resampling_alg = resampling_alg_dict.get(resampling_alg_name, 3)

    output = run_processing("gdal:warpreproject", {
        "TARGET_CRS": basis_deminfo["crs"],
        "TARGET_RESOLUTION": basis_deminfo["resolution"],
        "TARGET_EXTENT": ",".join(str(value) for value in basis_deminfo["extent"][:4]),
        "TARGET_EXTENT_CRS": basis_deminfo["crs"],
        "RESAMPLING": resampling_alg,
        "INPUT": target_tiff_filepath,
        "OUTPUT": output_filepath if output_filepath is not None else "TEMPORARY_OUTPUT",
    }, feedback=feedback)["OUTPUT"]
    return output


def replace_with_adjusted_extent_and_resolution(basis_tiff_filepath: str,
                                                target_tiff_filepath: str,
                                                resampling_alg_name="nearest",
                                                feedback=None) -> str:
    """
    既存の出力ラスターを、基準ラスターと同じ領域・解像度に揃えて置き換える
    """
    # 作業用のファイルは半角の一時フォルダに作る（出力先が全角文字を含んでも、外部の処理に渡さないため）
    temp_dir = tempfile.mkdtemp(dir=get_ascii_safe_dir("morizon_next_tmp"))
    temp_filepath = os.path.join(temp_dir, "adjusted.tif")
    try:
        adjusted_filepath = adjust_extent_and_resolution(
            basis_tiff_filepath,
            target_tiff_filepath,
            output_filepath=temp_filepath,
            resampling_alg_name=resampling_alg_name,
            feedback=feedback,
        )
        if os.path.exists(target_tiff_filepath):
            os.remove(target_tiff_filepath)
        shutil.move(adjusted_filepath, target_tiff_filepath)
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)
    return target_tiff_filepath


def replace_with_clipped_wgs84_extent(target_tiff_filepath: str,
                                      extent_wgs84,
                                      feedback=None) -> str:
    """
    ラスターをWGS84指定範囲でピクセル単位に切り出して置き換える。
    解析途中ではなく、最終成果物を表示・後段入力の範囲へ揃える用途。
    """
    if extent_wgs84 is None:
        return target_tiff_filepath
    if not HAS_GDAL:
        raise RuntimeError("GDAL is not available")

    lon_min, lat_min, lon_max, lat_max = extent_wgs84
    target_info = get_tiff_info(target_tiff_filepath, feedback=feedback)
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
        if feedback is not None:
            # gdal.Translate() は Processing を通らないので、工程名だけ明示して無表示を避ける。
            feedback.pushInfo("最終出力を指定範囲で切り出しています")
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


# 計算用に埋めたDEMで作った成果物へ、元のDEMのNoDataを戻すときの値（成果物にNoData値が無い場合）
OUTPUT_NODATA = -3.40282347e+38


def fill_dem_nodata_for_processing(dem_filepath: str, output_filepath: str, feedback=None) -> str:
    """
    DEMにNoData（海・水面など）があれば、周囲の値で埋めたコピーを output_filepath に作り、そのパスを返す。
    無ければ dem_filepath をそのまま返す。
    傾斜・曲率・集水などの計算はNoDataの縁で値が崩れたり途切れたりするため、計算には埋めたDEMを使い、
    最終成果物では restore_dem_nodata() で元のNoDataの場所をNoDataに戻す
    """
    import numpy as np

    if not HAS_GDAL:
        raise RuntimeError("GDAL is not available")
    src = gdal.Open(dem_filepath)
    band = src.GetRasterBand(1)
    nodata = band.GetNoDataValue()
    if nodata is None:
        return dem_filepath
    data = band.ReadAsArray()
    invalid = np.isnan(data) if np.isnan(nodata) else data == nodata
    if not invalid.any():
        return dem_filepath
    del data, invalid

    if feedback is not None:
        feedback.pushInfo("DEMのデータなし（海・水面など）を計算用に周囲の値で埋めています")
    ds = gdal.Translate(output_filepath, src,
                        creationOptions=["COMPRESS=LZW", "TILED=YES", "BIGTIFF=IF_NEEDED"])
    src = None
    if ds is None:
        raise RuntimeError("計算用のDEMを作成できませんでした")
    gdal.FillNodata(ds.GetRasterBand(1), None, 1000, 0)
    ds.FlushCache()
    ds = None
    return output_filepath


def restore_dem_nodata(target_tiff_filepath: str, dem_filepath: str, feedback=None) -> str:
    """
    元のDEMがNoDataの画素（海・水面など）を、成果物でもNoDataにする。
    fill_dem_nodata_for_processing() で埋めたDEMから計算した成果物に使う
    """
    import numpy as np

    if not HAS_GDAL:
        raise RuntimeError("GDAL is not available")
    target = gdal.Open(target_tiff_filepath, gdal.GA_Update)
    band = target.GetRasterBand(1)
    nodata = band.GetNoDataValue()
    if nodata is None:
        if band.DataType not in (gdal.GDT_Float32, gdal.GDT_Float64):
            # 整数の成果物で空いている値が分からないときは手を付けない
            target = None
            return target_tiff_filepath
        nodata = OUTPUT_NODATA
        band.SetNoDataValue(nodata)

    dem = gdal.Open(dem_filepath)
    dem_nodata = dem.GetRasterBand(1).GetNoDataValue()
    dem = None
    if dem_nodata is None:
        target = None
        return target_tiff_filepath
    gt = target.GetGeoTransform()
    cols, rows = target.RasterXSize, target.RasterYSize
    x_min, y_max = gt[0], gt[3]
    x_max, y_min = gt[0] + cols * gt[1], gt[3] + rows * gt[5]
    if feedback is not None:
        feedback.pushInfo("DEMのデータなし（海・水面など）を成果物でもデータなしにしています")
    dem_on_grid = gdal.Warp(
        "", dem_filepath, format="MEM",
        dstSRS=target.GetProjection(),
        outputBounds=(x_min, min(y_min, y_max), x_max, max(y_min, y_max)),
        width=cols, height=rows,
        resampleAlg="near",
        dstNodata=dem_nodata,
    )
    dem_values = dem_on_grid.GetRasterBand(1).ReadAsArray()
    dem_on_grid = None
    invalid = np.isnan(dem_values) if np.isnan(dem_nodata) else dem_values == dem_nodata
    if invalid.any():
        data = band.ReadAsArray()
        data[invalid] = nodata
        band.WriteArray(data)
        band.FlushCache()
    target = None
    return target_tiff_filepath
