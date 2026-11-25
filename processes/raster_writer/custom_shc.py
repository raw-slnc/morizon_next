"""Optional contour based terrain complexity, with gully and basin context.

The 10 m analysis grid, 10 m contours, 490 m contour window, MFD channel
evidence and 2 ha signed basin correction reproduce the accepted prototype.
The resulting value is an index; its scoring thresholds are set independently.
"""

import gc
import os
import tempfile

import numpy as np
from osgeo import gdal, ogr
from scipy.ndimage import distance_transform_edt, gaussian_filter, maximum_filter, minimum_filter
from scipy.spatial import cKDTree

from ...constants import OUTPUT_SHC_CUSTOM, PIXELS_THRESHOLD_RESAMPLING
from ..processing_feedback import set_stage
from . import custom_shc_catchments, custom_shc_contours, custom_shc_dinf, custom_shc_flow


NODATA = -9999.0
CELL_M = 10.0
SAMPLE_STEP_M = 10.0
WINDOW_M = 490.0
CONTOUR_INTERVAL_M = 10.0
SMOOTH_SIGMA = 0.6  # 結果の仕上げの平滑化（セル数）。小さいほど細い構造が残る（以前は 1.2）
# True なら、等高線の曲がりを「長さあたり」（曲率に相当）で測り、cos(傾斜)の重みも外す（傾斜との相関は下がる）。
# ただし山頂の小さな閉じた等高線が曲率の大きい所として最上位に出るため、既定は False（面積あたり。山頂は等高線の間隔が広く低く出る）
SLOPE_INDEPENDENT = False
# 沢の中心線（D∞の流量が大きく、周囲より凹んだ所）だけを細く薄く強める。沢の中心は、平面上の曲がりに加えて
# 断面方向の曲がりも強いため。幅は1セル分ほどに絞り、沢の周りまで一緒に濃くしない。0 なら補正しない
# 沢の中心線・2ha流域の補正に使う流れの計算の既定（実際は設定タブの選択を generate に渡す）。"mfd"（多方向流。QLAE 由来）か "dinf"（D∞。FOL 由来、より細い線）
FLOW_MODEL = "mfd"
CHANNEL_GAIN = 0.80
CHANNEL_FLOW_MIN = 50  # 沢の中心線とみなす流量の下限（セル数）
CHANNEL_FLOW_FULL = 1000  # この流量以上で、補正を最大にする
CHANNEL_SIGMA_M = 5.0
CHANNEL_CUTOFF_M = 10.0
CHANNEL_DEPTH_FULL_M = 6.0  # 周囲より凹んでいる深さがこれ以上で、補正を最大にする
# 流れが集まる沢筋と、その出口（平らな所）の床上げ。平らな所は等高線がほとんど無く、掛け算の補正では値が付かないので、
# 「上流から流れ込む複雑さの平均」に比例した値を、下限として足す。D∞の流量が大きい沢筋の周りだけに効かせ、
# 流量が大きすぎる所（大きな川の氾濫原）では効かなくする。0 なら使わない（D∞のときだけ）
PRESSURE_RATIO = 0.0
PRESSURE_SIGMA_M = 7.5
PRESSURE_CUTOFF_M = 15.0
PRESSURE_RIVER_START = 20000  # 流量（セル）がこれを超えると、川とみなして弱める
PRESSURE_RIVER_END = 100000  # これ以上で 0
BASIN_GAIN_UP = 0.25  # 2ha流域の平均が全体より高いとき、最大 +25%
BASIN_GAIN_DOWN = 0.40  # 全体より低いとき、最大 -40%（低い流域をより弱める）


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


def _calculate(dataset, dem, valid, work_dir, context, flow_model=FLOW_MODEL, fine=None):
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
    surface_weight = 1.0 / np.maximum(np.cos(slope), .1)
    high = maximum_filter(np.where(valid, terrain, -np.inf), 11)
    low = minimum_filter(np.where(valid, terrain, np.inf), 11)
    relief = np.maximum(high - low, 0.0)
    meaning = np.divide(relief, relief + 10.0, out=np.zeros_like(relief), where=np.isfinite(relief))
    slope_weight = (1.0 if SLOPE_INDEPENDENT else np.cos(slope)) * meaning
    base = _smooth(raw * slope_weight, valid, SMOOTH_SIGMA)

    terrain_nan = np.where(valid, terrain, np.nan)
    if flow_model == "dinf":
        context.report("D∞の流れの集まりを計算中")
        flow, proportions = custom_shc_dinf.flow_and_proportions(terrain_nan, CELL_M, context)
    else:
        context.report("MFDの流れの集まりを計算中")
        flow = custom_shc_flow.flow_accumulation(terrain_nan, 5, context)
        proportions = None
    evidence = np.zeros_like(base)
    if CHANNEL_GAIN > 0:
        surrounding = _smooth(terrain, valid, 9.0)
        channel = valid & (flow >= CHANNEL_FLOW_MIN) & ((surrounding - terrain) >= 2.0) & (relief >= 10.0)
        if np.any(channel):
            distance = distance_transform_edt(~channel) * CELL_M
            evidence = np.exp(-0.5 * (distance / CHANNEL_SIGMA_M) ** 2)
            evidence[distance > CHANNEL_CUTOFF_M] = 0.0
            # 断面方向の曲がりの強さの目安として、周囲より凹んだ深さで重みを付ける
            depth = np.clip((surrounding - terrain) / CHANNEL_DEPTH_FULL_M, 0.3, 1.0)
            # 流れが多く集まる沢ほど強く（流れが集まる所に災害がかかる）。流量の対数で 0.3〜1 の重み
            gather = np.clip(np.log10(np.maximum(flow, 1.0) / CHANNEL_FLOW_MIN)
                             / np.log10(CHANNEL_FLOW_FULL / CHANNEL_FLOW_MIN), 0.3, 1.0)
            evidence *= depth * gather
    existing = base * (1.0 + CHANNEL_GAIN * evidence)
    floor_value = None
    if PRESSURE_RATIO > 0 and flow_model == "dinf":
        context.report("上流の複雑さが流れ込む沢筋の床上げを計算中")
        upstream_sum = custom_shc_dinf.weighted_accumulation(terrain_nan, proportions, base, context)
        upstream_mean = np.divide(upstream_sum, flow, out=np.zeros_like(base), where=flow > 0)
        stream = valid & (flow >= CHANNEL_FLOW_MIN)
        if np.any(stream):
            distance = distance_transform_edt(~stream) * CELL_M
            near = np.exp(-0.5 * (distance / PRESSURE_SIGMA_M) ** 2)
            near[distance > PRESSURE_CUTOFF_M] = 0.0
            gather = np.clip(np.log10(np.maximum(flow, 1.0) / CHANNEL_FLOW_MIN)
                             / np.log10(CHANNEL_FLOW_FULL / CHANNEL_FLOW_MIN), 0.3, 1.0)
            river = np.clip(1.0 - np.log10(np.maximum(flow, 1.0) / PRESSURE_RIVER_START)
                            / np.log10(PRESSURE_RIVER_END / PRESSURE_RIVER_START), 0.0, 1.0)
            river = np.where(flow <= PRESSURE_RIVER_START, 1.0, river)
            floor_value = PRESSURE_RATIO * upstream_mean * near * gather * river
            existing = np.maximum(existing, floor_value)

    context.report("2ha流域の相対補正を計算中")
    if flow_model == "dinf":
        labels = custom_shc_dinf.basin_labels(terrain_nan, flow, proportions, 200, context)
    else:
        labels = custom_shc_catchments.basin_labels(terrain_nan, 200, 5, context)
    basin_factor = np.ones_like(existing)
    assigned = valid & (labels > 0)
    if np.any(assigned):
        all_mean = np.average(existing[valid], weights=surface_weight[valid])
        basin_area = np.bincount(labels[assigned].ravel(), weights=surface_weight[assigned].ravel())
        basin_sum = np.bincount(labels[assigned].ravel(),
                                weights=(existing[assigned] * surface_weight[assigned]).ravel())
        basin_mean = np.divide(basin_sum, basin_area,
                               out=np.zeros_like(basin_sum), where=basin_area > 0)
        signed = np.divide(basin_mean - all_mean, basin_mean + all_mean,
                           out=np.zeros_like(basin_mean), where=(basin_mean + all_mean) > 0)
        basin_signed = signed[labels[assigned]]
        basin_factor[assigned] = 1.0 + np.where(basin_signed >= 0, BASIN_GAIN_UP, BASIN_GAIN_DOWN) * basin_signed
    if fine is None:
        adjusted = existing * basin_factor
        adjusted[~valid] = NODATA
        return adjusted, dataset, valid

    # 細かい出力格子：評価値は細かい格子に直接割り当て、10m で求めた重み・沢の補正・流域の補正は補間して掛ける
    context.report("細かい格子に合わせて仕上げ中")
    fine_dataset, fine_valid = fine["dataset"], fine["valid"]
    raw_fine = _rasterize_contour_score(parts, fine_dataset, fine_valid, context)
    fine_cell = abs(fine_dataset.GetGeoTransform()[1])
    weight_fine = _to_fine_grid(slope_weight, valid, dataset, fine_dataset, 0.0)
    base_fine = _smooth(raw_fine * weight_fine, fine_valid, SMOOTH_SIGMA * CELL_M / fine_cell)
    del raw_fine, weight_fine
    existing_fine = base_fine * (1.0 + CHANNEL_GAIN * _to_fine_grid(evidence, valid, dataset, fine_dataset, 0.0))
    del base_fine
    if floor_value is not None:
        existing_fine = np.maximum(existing_fine, _to_fine_grid(floor_value, valid, dataset, fine_dataset, 0.0))
    adjusted_fine = existing_fine * _to_fine_grid(basin_factor, valid, dataset, fine_dataset, 1.0)
    adjusted_fine[~fine_valid] = NODATA
    return adjusted_fine, fine_dataset, fine_valid


def generate(dem_filepath: str, output_dir: str, target_dem_filepath: str = None, feedback=None,
             flow_model: str = None) -> str:
    """Calculate from the source DEM; align the result with the other elements."""
    flow_model = flow_model if flow_model in ("mfd", "dinf") else FLOW_MODEL
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
                    fitted = None
                fine_dataset, fine_dem, fine_valid = _read_dem(fine_path)
                fine = {"dataset": fine_dataset, "dem": fine_dem, "valid": fine_valid}
            set_stage(feedback, "地形の複雑さ（等高線法）：等高線・沢筋・流域")
            values, grid, grid_valid = _calculate(analysis, dem, valid, work_dir, context, flow_model, fine)
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
