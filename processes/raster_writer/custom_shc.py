"""Optional contour based terrain complexity, corrected by the CS Map's valley blue.

The 10 m analysis grid, 10 m contours and 490 m contour window give the base
value; the parts the CS Map shows blue (valleys, valley heads, concave slopes)
are raised and the rest lowered. The resulting value is an index; its scoring
thresholds are set independently.
"""

import gc
import os
import tempfile

import numpy as np
from osgeo import gdal, ogr
from scipy.ndimage import distance_transform_edt, gaussian_filter, maximum_filter, minimum_filter
from scipy.spatial import cKDTree

from ...constants import (
    CUSTOM_SHC_CSMAP_GAIN_DOWN, CUSTOM_SHC_CSMAP_GAIN_UP, OUTPUT_SHC_CUSTOM, PIXELS_THRESHOLD_RESAMPLING,
)
from ..processing_feedback import set_stage
from . import custom_shc_contours, custom_shc_csmap


NODATA = -9999.0
CELL_M = 10.0
SAMPLE_STEP_M = 10.0
WINDOW_M = 490.0
CONTOUR_INTERVAL_M = 10.0
SMOOTH_SIGMA = 0.6  # 結果の仕上げの平滑化（セル数）。小さいほど細い構造が残る（以前は 1.2）
# True なら、等高線の曲がりを「長さあたり」（曲率に相当）で測り、cos(傾斜)の重みも外す（傾斜との相関は下がる）。
# ただし山頂の小さな閉じた等高線が曲率の大きい所として最上位に出るため、既定は False（面積あたり。山頂は等高線の間隔が広く低く出る）
SLOPE_INDEPENDENT = False
# CS立体図の青の補正：CS立体図で青く見える部分（沢・谷頭・凹んだ斜面）を一律に強め、それ以外を一律に弱める。
# 青く見える部分の判定は QLAE の CS Valley と同じ（custom_shc_csmap.py）。青の濃さは使わず、青い部分かどうかだけを使う。
# 細かい出力格子があれば、その細かい DEM で CS立体図を作る（細かい DEM で見る青にそろえる）
CSMAP_GAIN_UP = CUSTOM_SHC_CSMAP_GAIN_UP  # 青く見える部分を強める割合（constants.py）
CSMAP_GAIN_DOWN = CUSTOM_SHC_CSMAP_GAIN_DOWN  # 青くない部分を弱める割合（constants.py）

# ── 試して使わなかった補正（2026-11-26、CS立体図の青に決めた。コードは削除し、概要だけ残す）──────────────
# ・沢の中心線：流れの計算（MFD＝多方向流 QLAE 由来、または D∞＝Tarboton FOL 由来）で流量が 50 セル以上、かつ
#   周りより 2m 以上凹んだ所を、幅 10m ほどで最大 +80% 強める（凹みの深さと流量の対数で重み）
# ・沢筋の床上げ（D∞のときだけ、既定はオフ）：上流から流れ込む複雑さの平均に比例した値を、沢筋の周りの下限にする
# ・2ha流域の相対補正：2ha の流域ごとの平均を全体の平均と比べ、高い流域を最大 +25%、低い流域を最大 -40%。
#   流域の中は同じ倍率になり、流域の境界で値が段になった
# ・凹みの深さ：周り（σ 30m）の平均より何 m 低いかで、4m 以上凹んだ所を最大 +80%、出っ張った所を最大 -40%
# ・ベースの等高線法の差を縮める（平均からの差を一定の割合に）：補正を強めたときの組み合わせとして試した


class _Context:
    def __init__(self, feedback):
        self.feedback = feedback

    def check(self):
        if self.feedback is not None and self.feedback.isCanceled():
            raise RuntimeError("地形の複雑さ（等高線法）計算を中断しました")

    def report(self, message):
        self.check()
        if self.feedback is not None:
            self.feedback.pushInfo(message)


def _smooth(array, valid, sigma):
    numerator = gaussian_filter(np.where(valid, array, 0.0), sigma, mode="constant")
    denominator = gaussian_filter(valid.astype(np.float64), sigma, mode="constant")
    return np.divide(numerator, denominator, out=np.zeros_like(array), where=denominator > 0)


def _csmap_factor(dem, valid, cell_m):
    """CS立体図の青の補正の倍率：青く見える部分は一律 1+UP、それ以外は一律 1-DOWN（青の濃さは使わない）"""
    blue = custom_shc_csmap.cs_blue_part(np.where(valid, dem, np.nan), cell_m)
    return np.where(blue, 1.0 + CSMAP_GAIN_UP, 1.0 - CSMAP_GAIN_DOWN)


def _sample_line(points):
    xy = np.asarray(points, dtype=np.float64)
    if len(xy) < 2:
        return None
    delta = np.diff(xy, axis=0)
    stations = np.r_[0.0, np.cumsum(np.hypot(delta[:, 0], delta[:, 1]))]
    keep = np.r_[True, np.diff(stations) > 1e-8]
    xy, stations = xy[keep], stations[keep]
    if len(xy) < 2 or stations[-1] <= 0:
        return None
    sampled_stations = np.unique(np.r_[np.arange(0, stations[-1], SAMPLE_STEP_M), stations[-1]])
    sampled = np.column_stack([
        np.interp(sampled_stations, stations, xy[:, 0]),
        np.interp(sampled_stations, stations, xy[:, 1]),
    ])
    return sampled_stations, sampled


def _contour_parts(lines, context):
    parts = []
    by_level = {}
    for index, (wkt, elevation) in enumerate(lines):
        if index % 100 == 0:
            context.check()
        geometry = ogr.CreateGeometryFromWkt(wkt)
        if geometry is None:
            continue
        geometries = ([geometry] if geometry.GetGeometryType() in (ogr.wkbLineString, ogr.wkbLineString25D)
                      else [geometry.GetGeometryRef(i) for i in range(geometry.GetGeometryCount())])
        for line in geometries:
            result = _sample_line([line.GetPoint_2D(i) for i in range(line.GetPointCount())])
            if result is None:
                continue
            stations, xy = result
            level = int(round(float(elevation) / CONTOUR_INTERVAL_M))
            part = {"level": level, "stations": stations, "xy": xy}
            parts.append(part)
            by_level.setdefault(level, []).append(xy)
    trees = {level: cKDTree(np.concatenate(points)) for level, points in by_level.items()}
    for part in parts:
        distances = [trees[level].query(part["xy"])[0]
                     for level in (part["level"] - 1, part["level"] + 1) if level in trees]
        if len(distances) == 2:
            spacing = (distances[0] + distances[1]) / 2.0
        elif distances:
            spacing = distances[0]
        else:
            spacing = np.full(len(part["xy"]), np.nan)
        part["spacing"] = spacing
    return parts


def _contour_score(parts):
    for part in parts:
        xy, stations = part["xy"], part["stations"]
        turning = np.zeros(len(xy))
        if len(xy) > 2:
            a, b = xy[1:-1] - xy[:-2], xy[2:] - xy[1:-1]
            turning[1:-1] = np.abs(np.arctan2(
                a[:, 0] * b[:, 1] - a[:, 1] * b[:, 0], np.sum(a * b, axis=1)))
        width = (part["spacing"][:-1] + part["spacing"][1:]) / 2.0
        area_cumulative = np.r_[0.0, np.cumsum(np.nan_to_num(width * np.diff(stations)))]
        turning_cumulative = np.r_[0.0, np.cumsum(turning)]
        left = np.searchsorted(stations, stations - WINDOW_M / 2, side="left")
        right = np.searchsorted(stations, stations + WINDOW_M / 2, side="right") - 1
        area = area_cumulative[right] - area_cumulative[left]
        total_turn = turning_cumulative[right + 1] - turning_cumulative[left]
        if SLOPE_INDEPENDENT:
            length = stations[right] - stations[left]
            part["score"] = np.divide(total_turn * 1000.0, length,
                                      out=np.zeros(len(xy)), where=length > 0)
        else:
            part["score"] = np.divide(total_turn * 10000.0, area,
                                      out=np.zeros(len(xy)), where=area > 0)


def _rasterize_contour_score(parts, dataset, valid, context):
    raw = np.zeros(valid.shape, dtype=np.float64)
    if not parts:
        return raw
    xy = np.concatenate([part["xy"] for part in parts])
    scores = np.concatenate([part["score"] for part in parts])
    spacing = np.concatenate([part["spacing"] for part in parts])
    tree = cKDTree(xy)
    gt = dataset.GetGeoTransform()
    half_diagonal = np.hypot(gt[1], gt[5]) / 2.0
    cols = valid.shape[1]
    # Query in rows to limit peak memory for broad analysis areas.
    for row_start in range(0, valid.shape[0], 256):
        context.check()
        row_stop = min(row_start + 256, valid.shape[0])
        rr, cc = np.indices((row_stop - row_start, cols))
        rr += row_start
        centers = np.column_stack((gt[0] + (cc.ravel() + .5) * gt[1],
                                   gt[3] + (rr.ravel() + .5) * gt[5]))
        distance, nearest = tree.query(centers)
        within = distance <= spacing[nearest] / 2.0 + half_diagonal
        raw[row_start:row_stop] = np.where(within, scores[nearest], 0.0).reshape(row_stop-row_start, cols)
    raw[~valid] = 0.0
    return raw


def _read_dem(path):
    dataset = gdal.Open(path)
    if dataset is None:
        raise RuntimeError(f"DEMを開けません: {path}")
    band = dataset.GetRasterBand(1)
    dem = band.ReadAsArray().astype(np.float64)
    nodata = band.GetNoDataValue()
    valid = np.isfinite(dem)
    if nodata is not None:
        valid &= dem != nodata
    return dataset, dem, valid


def _fill_invalid(array, valid, fill_value):
    """無効なセル（NoData）に、いちばん近い有効なセルの値を入れる（補間で、縁の値が引きずられないように）"""
    if valid.all() or not valid.any():
        return np.where(valid, array, fill_value)
    index = distance_transform_edt(~valid, return_distances=False, return_indices=True)
    return array[tuple(index)]


def _to_fine_grid(array, valid, coarse, fine, fill_value):
    """10m 格子の配列を、細かい出力格子へ、双一次で補間する（GDAL の補間を使い、メモリを抑える）"""
    filled = _fill_invalid(array, valid, fill_value).astype(np.float32)
    rows, cols = filled.shape
    memory = gdal.GetDriverByName("MEM").Create("", cols, rows, 1, gdal.GDT_Float32)
    memory.SetGeoTransform(coarse.GetGeoTransform())
    memory.SetProjection(coarse.GetProjection())
    memory.GetRasterBand(1).WriteArray(filled)
    fgt = fine.GetGeoTransform()
    bounds = (fgt[0], fgt[3] + fine.RasterYSize * fgt[5], fgt[0] + fine.RasterXSize * fgt[1], fgt[3])
    out = gdal.Warp("", memory, format="MEM", outputBounds=bounds,
                    width=fine.RasterXSize, height=fine.RasterYSize, resampleAlg="bilinear")
    result = out.GetRasterBand(1).ReadAsArray().astype(np.float32)
    memory = out = None
    return result


def _write(path, values, valid, reference):
    rows, cols = values.shape
    dataset = gdal.GetDriverByName("GTiff").Create(
        path, cols, rows, 1, gdal.GDT_Float32,
        ["TILED=YES", "COMPRESS=LZW", "BIGTIFF=IF_NEEDED", "TFW=YES"])
    if dataset is None:
        raise RuntimeError(f"地形の複雑さ（等高線法）を保存できません: {path}")
    dataset.SetGeoTransform(reference.GetGeoTransform())
    dataset.SetProjection(reference.GetProjection())
    dataset.SetMetadata({"METHOD": "contours from the fine DEM; 490m turning/area; stream centre and 2ha basin",
                         "UNITS": "index", "STATUS": "optional Morizon Next method"})
    band = dataset.GetRasterBand(1)
    band.SetNoDataValue(NODATA)
    band.WriteArray(np.where(valid, values, NODATA).astype(np.float32))
    dataset = None


def _calculate(dataset, dem, valid, work_dir, context, fine=None):
    """fine：出力の細かい格子（{"dataset", "dem", "valid"}）。あれば、等高線をその細かい DEM から引き、
    曲がりの評価値を細かい格子に直接割り当てる。戻り値は (値, 値の格子のデータセット, その有効マスク)"""
    gt = dataset.GetGeoTransform()
    cell_m = abs(gt[1])
    if abs(cell_m - CELL_M) > .01 or abs(abs(gt[5]) - CELL_M) > .01:
        raise RuntimeError("地形の複雑さ（等高線法）の内部DEMは10m格子が必要です")
    x0, y0 = gt[0], gt[3]
    x1, y1 = x0 + dataset.RasterXSize * gt[1], y0 + dataset.RasterYSize * gt[5]
    area_wkt = (f"POLYGON (({x0} {y0}, {x1} {y0}, {x1} {y1}, "
                f"{x0} {y1}, {x0} {y0}))")
    if fine is not None:
        # 等高線は、10m に平均化する前の細かい DEM から引く（滑らかさは、等高線の処理がメートルで与える）
        fgt = fine["dataset"].GetGeoTransform()
        fx0, fy0 = fgt[0], fgt[3]
        fx1, fy1 = fx0 + fine["dataset"].RasterXSize * fgt[1], fy0 + fine["dataset"].RasterYSize * fgt[5]
        fine_area_wkt = (f"POLYGON (({fx0} {fy0}, {fx1} {fy0}, {fx1} {fy1}, "
                         f"{fx0} {fy1}, {fx0} {fy0}))")
        lines = custom_shc_contours.compute_contours(
            np.where(fine["valid"], fine["dem"], np.nan), fgt, fine["dataset"].GetProjection(),
            abs(fgt[1]), fine_area_wkt, work_dir, context)
    else:
        prepared = np.where(valid, dem, np.nan)
        lines = custom_shc_contours.compute_contours(
            prepared, gt, dataset.GetProjection(), CELL_M, area_wkt, work_dir, context)
    context.report("等高線の曲がりと間隔を評価中")
    parts = _contour_parts(lines, context)
    _contour_score(parts)
    raw = _rasterize_contour_score(parts, dataset, valid, context)
    if not parts:
        return np.where(valid, 0.0, NODATA), dataset, valid

    terrain = _smooth(dem, valid, 1.5)
    dzdy, dzdx = np.gradient(terrain, CELL_M, CELL_M)
    slope = np.arctan(np.hypot(dzdx, dzdy))
    high = maximum_filter(np.where(valid, terrain, -np.inf), 11)
    low = minimum_filter(np.where(valid, terrain, np.inf), 11)
    relief = np.maximum(high - low, 0.0)
    meaning = np.divide(relief, relief + 10.0, out=np.zeros_like(relief), where=np.isfinite(relief))
    slope_weight = (1.0 if SLOPE_INDEPENDENT else np.cos(slope)) * meaning
    base = _smooth(raw * slope_weight, valid, SMOOTH_SIGMA)

    context.report("CS立体図の青を計算中")
    if fine is None:
        adjusted = base * _csmap_factor(dem, valid, CELL_M)
        adjusted[~valid] = NODATA
        return adjusted, dataset, valid

    # 細かい出力格子：評価値と CS立体図の青は細かい格子で求め、10m で求めた重みは補間して掛ける
    context.report("細かい格子に合わせて仕上げ中")
    fine_dataset, fine_valid = fine["dataset"], fine["valid"]
    raw_fine = _rasterize_contour_score(parts, fine_dataset, fine_valid, context)
    fine_cell = abs(fine_dataset.GetGeoTransform()[1])
    weight_fine = _to_fine_grid(slope_weight, valid, dataset, fine_dataset, 0.0)
    base_fine = _smooth(raw_fine * weight_fine, fine_valid, SMOOTH_SIGMA * CELL_M / fine_cell)
    del raw_fine, weight_fine
    adjusted_fine = base_fine * _csmap_factor(fine["dem"], fine_valid, fine_cell)
    adjusted_fine[~fine_valid] = NODATA
    return adjusted_fine, fine_dataset, fine_valid


def generate(dem_filepath: str, output_dir: str, target_dem_filepath: str = None, feedback=None) -> str:
    """Calculate from the source DEM; align the result with the other elements."""
    context = _Context(feedback)
    os.makedirs(output_dir, exist_ok=True)
    output_path = os.path.join(output_dir, OUTPUT_SHC_CUSTOM["FILE_NAME"] + ".tif")
    with tempfile.TemporaryDirectory(prefix="morizon_custom_shc_") as work_dir:
        # Windows では、GDAL が開いたままのファイルは一時フォルダを消すときに掴まれて消せない（WinError 32）。
        # 途中で失敗した場合も含め、フォルダを消す前に必ず手放す
        source = analysis = target = warped = result = fine_dataset = grid = None
        try:
            source, _, _ = _read_dem(dem_filepath)
            gt = source.GetGeoTransform()
            if abs(abs(gt[1]) - CELL_M) > .01 or abs(abs(gt[5]) - CELL_M) > .01:
                finer = abs(gt[1]) < CELL_M or abs(gt[5]) < CELL_M
                set_stage(feedback, "地形の複雑さ（等高線法）：DEMを10mに平均化" if finer
                          else "地形の複雑さ（等高線法）：DEMを10m格子に補間")
                analysis_path = os.path.join(work_dir, "analysis_10m.tif")
                warped = gdal.Warp(analysis_path, source, xRes=CELL_M, yRes=CELL_M,
                                   targetAlignedPixels=True,
                                   resampleAlg="average" if finer else "bilinear",
                                   dstNodata=NODATA)
                if warped is None:
                    raise RuntimeError("10m DEMへの平均化に失敗しました")
                warped = None
            else:
                analysis_path = dem_filepath
            analysis, dem, valid = _read_dem(analysis_path)
            target = gdal.Open(target_dem_filepath or dem_filepath)
            if target is None:
                raise RuntimeError("出力格子のDEMを開けません")
            # 出力の格子が10mより細かいときは、等高線を細かい DEM から引き、評価値を出力の格子に直接割り当てる
            # （10m に平均化すると、1m の DEM を使っても、結果が10m相当になるため）。大きすぎる範囲は10mのまま
            fine = None
            tgt_gt = target.GetGeoTransform()
            if (abs(tgt_gt[1]) < CELL_M - .01
                    and target.RasterXSize * target.RasterYSize <= PIXELS_THRESHOLD_RESAMPLING):
                fine_path = target_dem_filepath or dem_filepath
                if abs(gt[1] - tgt_gt[1]) > 1e-6 or source.RasterXSize != target.RasterXSize \
                        or source.RasterYSize != target.RasterYSize:
                    # 元の DEM と出力の格子が違うときは、出力の格子に合わせた DEM を作る
                    fine_path = os.path.join(work_dir, "fine_dem.tif")
                    bounds_f = (tgt_gt[0], tgt_gt[3] + target.RasterYSize * tgt_gt[5],
                                tgt_gt[0] + target.RasterXSize * tgt_gt[1], tgt_gt[3])
                    fitted = gdal.Warp(fine_path, source, outputBounds=bounds_f, width=target.RasterXSize,
                                       height=target.RasterYSize, resampleAlg="bilinear", dstNodata=NODATA)
                    if fitted is None:
                        raise RuntimeError("出力の格子に合わせたDEMを作れませんでした")
                    fitted = None  # 閉じて書き出す
                fine_dataset, fine_dem, fine_valid = _read_dem(fine_path)
                fine = {"dataset": fine_dataset, "dem": fine_dem, "valid": fine_valid}
            set_stage(feedback, "地形の複雑さ（等高線法）：等高線・CS立体図の青")
            values, grid, grid_valid = _calculate(analysis, dem, valid, work_dir, context, fine)
            analysis_output = os.path.join(work_dir, "custom_result.tif")
            _write(analysis_output, values, grid_valid, grid)
            same_grid = (target.RasterXSize == grid.RasterXSize
                         and target.RasterYSize == grid.RasterYSize
                         and target.GetGeoTransform() == grid.GetGeoTransform())
            if same_grid:
                result = gdal.Translate(output_path, analysis_output,
                                        creationOptions=["TILED=YES", "COMPRESS=LZW", "TFW=YES"])
            else:
                ref_gt = target.GetGeoTransform()
                bounds = (ref_gt[0], ref_gt[3] + target.RasterYSize * ref_gt[5],
                          ref_gt[0] + target.RasterXSize * ref_gt[1], ref_gt[3])
                # 10m 格子の結果を細かい出力格子へ合わせる。最近傍だと10mのブロックが階段状に出るため、双一次で補間する
                result = gdal.Warp(output_path, analysis_output, outputBounds=bounds,
                                   width=target.RasterXSize, height=target.RasterYSize,
                                   resampleAlg="bilinear", srcNodata=NODATA, dstNodata=NODATA,
                                   creationOptions=["TILED=YES", "COMPRESS=LZW", "TFW=YES"])
            if result is None:
                raise RuntimeError("地形の複雑さ（等高線法）の保存に失敗しました")
            result = None
            context.check()
        finally:
            source = analysis = target = warped = result = fine_dataset = grid = fine = None
            gc.collect()
    return output_path
