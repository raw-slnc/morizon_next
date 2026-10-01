"""
地形の複雑さの前半（DEMの平滑化・平面曲率）を GDAL と numpy だけで計算する。
SAGA（Processing Saga NextGen Provider）が無くても計算でき、原版の設計どおりの値を出す。

原版（QGIS 3.16 + SAGA 2.3）の設定（手引 p.87）:
    ① SAGA GaussianFilter  Standard Deviation=3, Search Radius=12（セル単位、円形）
    ② SAGA Curvature       Plan Curvature (C_PLAN), Zevenbergen & Thorne (1987)
QGIS 3.44 以降の NextGen プロバイダー（SAGA 9）では①のパラメータ名が変わっており
（KERNEL_RADIUS / SIGMA）、プラグインの MODE / RADIUS は使われず、ほぼ平滑化されない。
そのため既定ではこちらで計算する（設定タブの「SAGA ON」で従来どおり SAGA も使える）。

2026-10-01 に検証（サンプルDEM DEM_SAMPLE.tif、原版を QGIS 3.16.10 + SAGA 2.3.2 で実行した Y_11_chikei.tif と比較）:
    ・平面曲率の式は SAGA 2.3 と SAGA 9 で係数が違う。SAGA 9 は2階微分を半分の値（r=D, t=E）で使い、
      原版の SAGA 2.3 は r=2D, t=2E（s=F）で使っていた。原版に合わせて後者で計算する
      （SAGA 9 の式のままだと地形の複雑さの値が原版の約6割になる）
    ・平滑化は σ=3 セル、半径12 セルの円形窓のガウシアン加重平均（原版の MODE=1＝円形に合わせる）
    ・最終の地形の複雑さは、外周・データの無いセルから25セル（計算範囲49セルの半分）より内側で
      原版と差の中央値 4e-8（ほぼ一致）
    ・外周と、DEMのデータの無いセルの周りでは、原版とわずかに違う（欠けの周り25セル以内で差が最大0.02程度）。
      ここは原版のプラグインが何も指定しておらず、SAGA 2.3 の内部の扱いに任せていた部分なので、
      合わせ込みはしない（2026-10-01、ユーザーと判断）。手引の方法論（p.72）では欠けは平滑化の前に
      穴埋めする手順になっている
"""

import math

import numpy as np
from osgeo import gdal

GAUSSIAN_SIGMA = 3.0   # セル
GAUSSIAN_RADIUS = 12   # セル
NODATA = -9999.0


def _shifted(array, offset, axis):
    """array を axis 方向に offset セルずらした配列（はみ出した分は0で埋める）。
    戻り値の位置 i には array の i+offset の値が入る"""
    out = np.zeros_like(array)
    n = array.shape[axis]
    src = [slice(None), slice(None)]
    dst = [slice(None), slice(None)]
    src[axis] = slice(max(0, offset), n + min(0, offset))
    dst[axis] = slice(max(0, -offset), n - max(0, offset))
    out[tuple(dst)] = array[tuple(src)]
    return out


def _circular_gaussian_sum(values, sigma, radius):
    """半径 radius セルの円内について、ガウシアンの重み exp(-(dx²+dy²)/2σ²) を掛けた和をとる。
    重みは exp(-dx²/2σ²)·exp(-dy²/2σ²) に分かれるので、行（dy）ごとに「横方向に ±rx セルの重み付き和」
    （rx は円の幅）を縦にずらして足せばよい。横方向の和は rx を1ずつ広げながら作るので、
    円形でも計算量は縦横分割とほぼ同じ"""
    def weight(d):
        return math.exp(-(d * d) / (2 * sigma * sigma))

    rows_by_width = {}
    for dy in range(-radius, radius + 1):
        rows_by_width.setdefault(math.isqrt(radius * radius - dy * dy), []).append(dy)

    total = np.zeros_like(values)
    horizontal = values.copy()  # 横方向 ±0 セルの和
    for width in range(radius + 1):
        if width > 0:
            horizontal += weight(width) * (_shifted(values, width, 1) + _shifted(values, -width, 1))
        for dy in rows_by_width.get(width, []):
            total += weight(dy) * _shifted(horizontal, dy, 0)
    return total


def gaussian_smooth(z, valid, sigma=GAUSSIAN_SIGMA, radius=GAUSSIAN_RADIUS):
    """円形窓のガウシアン加重平均（原版の MODE=1＝円形）。
    データの無いセルはNaNのまま、平均には周りの有効セルだけを使う（重みを有効セルの分で割り直す）"""
    mask = valid.astype(np.float64)
    total = _circular_gaussian_sum(np.where(valid, z, 0.0), sigma, radius)
    weight_sum = _circular_gaussian_sum(mask, sigma, radius)
    with np.errstate(invalid="ignore", divide="ignore"):
        smoothed = total / weight_sum
    smoothed[~valid] = np.nan
    return smoothed


def plan_curvature(z, cell_size):
    """平面曲率（原版の SAGA 2.3 Curvature, Zevenbergen & Thorne と同じ式。SAGA 9 とは係数が異なる）。
    z は行0が北。周囲3x3が揃わないセル（外周・データの無いセルの隣）は NaN"""
    z1, z2, z3 = z[:-2, :-2], z[:-2, 1:-1], z[:-2, 2:]
    z4, z5, z6 = z[1:-1, :-2], z[1:-1, 1:-1], z[1:-1, 2:]
    z7, z8, z9 = z[2:, :-2], z[2:, 1:-1], z[2:, 2:]
    area = cell_size * cell_size
    r = 2 * ((z4 + z6) / 2 - z5) / area
    t = 2 * ((z2 + z8) / 2 - z5) / area
    s = (-z1 + z3 + z7 - z9) / (4 * area)
    p = (z6 - z4) / (2 * cell_size)
    q = (z2 - z8) / (2 * cell_size)
    p2q2 = p * p + q * q
    with np.errstate(invalid="ignore", divide="ignore"):
        plan = np.where(p2q2 > 0, -(t * p * p + r * q * q - 2 * s * p * q) / np.power(p2q2, 1.5), 0.0)
    out = np.full(z.shape, np.nan)
    out[1:-1, 1:-1] = plan
    return out


def write_plan_curvature(dem_filepath: str, output_filepath: str) -> str:
    """DEMを平滑化して平面曲率を求め、GeoTIFF（Float32、NoData=-9999）で保存する"""
    src = gdal.Open(dem_filepath)
    if src is None:
        raise RuntimeError(f"DEMを開けませんでした: {dem_filepath}")
    band = src.GetRasterBand(1)
    z = band.ReadAsArray().astype(np.float64)
    nodata = band.GetNoDataValue()
    valid = np.isfinite(z)
    if nodata is not None:
        valid &= z != nodata
    gt = src.GetGeoTransform()
    if abs(abs(gt[1]) - abs(gt[5])) > 1e-6 * abs(gt[1]):
        raise RuntimeError("DEMのセルが正方形ではないため、曲率を計算できません")

    curvature = plan_curvature(gaussian_smooth(z, valid), abs(gt[1]))

    out = gdal.GetDriverByName("GTiff").Create(
        output_filepath, src.RasterXSize, src.RasterYSize, 1, gdal.GDT_Float32,
        ["COMPRESS=LZW", "TILED=YES", "BIGTIFF=IF_NEEDED"],
    )
    out.SetGeoTransform(gt)
    out.SetProjection(src.GetProjection())
    out_band = out.GetRasterBand(1)
    out_band.SetNoDataValue(NODATA)
    out_band.WriteArray(np.where(np.isfinite(curvature), curvature, NODATA).astype(np.float32))
    out_band.FlushCache()
    out = None
    src = None
    return output_filepath
