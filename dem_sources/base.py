"""
DEM取得元（データソース）の共通インターフェースと共通処理。

DEMブラウザ（forest_zoning_dem_browser_dialog.py）と取得スレッド（processes/dem_fetch.py）は
ここで定義した DemSource のメソッドだけを使い、個々のソースのタイル体系・配布形式・索引には
触れない。ソースごとの事情は各モジュール（gsi.py / virtual_shizuoka.py / nagano_*.py）に閉じ込める。

範囲はすべて WGS84 経緯度のタプル (lon_min, lat_min, lon_max, lat_max) で受け渡す。
QGIS API には依存せず、座標変換は GDAL/OSR で行う（ワーカースレッドから安全に呼べるように）。
"""

import math
import os

from osgeo import gdal, osr

gdal.UseExceptions()

NODATA = -9999.0

# 取得後、表示範囲のうちこの割合以上に標高値が無ければ取得失敗とする。
# 残りの小さな欠け（水面・計測漏れ）は周囲から補間する
MIN_VALID_FRACTION = 0.99
FILL_MAX_SEARCH_PX = 100


class Cancelled(Exception):
    """cancel_cb が中断を示したときに送出する。"""


class FetchReporter:
    """取得の進捗通知先。処理スレッドがシグナルに中継する（既定は何もしない）。"""

    def start(self, total: int):
        """進捗バーの総ステップ数を設定する"""

    def step(self, n: int = 1):
        """進捗を n ステップ進める"""

    def message(self, text: str):
        """現在の工程名"""

    def detail(self, text: str):
        """工程内の詳細（何枚目・何MB等）"""


class DemSource:
    """DEM取得元の基底クラス。ソースを追加するときはこれを継承し、
    dem_sources/__init__.py の all_sources() に並べる。"""

    key = ""
    label = ""
    # ブラウザに表示する説明（出典・特徴・利用条件の確認先）
    description = ""

    def coverage_problem(self, extent):
        """表示範囲がこのソースの対象地域に収まっていれば None、
        収まっていなければ理由の文字列を返す。通信せずに判定すること
        （ダイアログを開いた時点で全ソースに対して呼ばれる）。"""
        return None

    def estimate(self, extent):
        """取得量の目安（タイル数など）。表示しない場合は空文字列。"""
        return ""

    def fetch(self, extent, output_path, cancel_cb, reporter):
        """表示範囲のDEMを取得し、output_path に GeoTIFF で保存する。
        戻り値は {"path": 保存先, "info": 完了ダイアログに出す概要}。
        中断時は Cancelled、失敗時は理由を含む例外を送出する。"""
        raise NotImplementedError


# ── 共通処理 ─────────────────────────────────────────────────────────


def check_cancel(cancel_cb):
    if cancel_cb and cancel_cb():
        raise Cancelled()


def extent_in_epsg(extent, epsg):
    """WGS84経緯度の範囲を指定の投影座標系へ変換した外接矩形 (xmin, ymin, xmax, ymax) を返す。"""
    src = osr.SpatialReference()
    src.ImportFromEPSG(4326)
    dst = osr.SpatialReference()
    dst.ImportFromEPSG(epsg)
    for srs in (src, dst):
        srs.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
    transform = osr.CoordinateTransformation(src, dst)

    lon_min, lat_min, lon_max, lat_max = extent
    steps = 16
    xs, ys = [], []
    for i in range(steps + 1):
        t = i / steps
        lon = lon_min + (lon_max - lon_min) * t
        lat = lat_min + (lat_max - lat_min) * t
        for px, py in ((lon, lat_min), (lon, lat_max), (lon_min, lat), (lon_max, lat)):
            x, y = transform.TransformPoint(px, py)[:2]
            xs.append(x)
            ys.append(y)
    return min(xs), min(ys), max(xs), max(ys)


def extent_within(extent, bbox):
    """extent が bbox（どちらも経緯度）の内側に収まっているか"""
    return (extent[0] >= bbox[0] and extent[1] >= bbox[1]
            and extent[2] <= bbox[2] and extent[3] <= bbox[3])


def sample_points(bbox, n=8):
    """bbox 内に格子状の点を (n+1)^2 個並べる（範囲が索引で覆われているかの判定用）"""
    xmin, ymin, xmax, ymax = bbox
    return [
        (xmin + (xmax - xmin) * i / n, ymin + (ymax - ymin) * j / n)
        for i in range(n + 1) for j in range(n + 1)
    ]


def build_dem_from_tiles(tile_paths, bbox, epsg, output_path, resolution):
    """タイルGeoTIFF群を結合し、bbox（epsg座標）で切り出して resolution[m] で保存する。

    表示範囲の MIN_VALID_FRACTION 以上に標高値が無い場合は、範囲の一部がデータの
    整備範囲外とみなして RuntimeError を送出する（穴の空いたDEMで解析させない）。
    (列数, 行数, 補間したセル数) を返す。"""
    import numpy as np

    xmin = math.floor(bbox[0] / resolution) * resolution
    ymin = math.floor(bbox[1] / resolution) * resolution
    xmax = math.ceil(bbox[2] / resolution) * resolution
    ymax = math.ceil(bbox[3] / resolution) * resolution

    vrt_path = output_path + ".tiles.vrt"
    try:
        vrt = gdal.BuildVRT(vrt_path, tile_paths, srcNodata=NODATA, VRTNodata=NODATA)
        if vrt is None:
            raise RuntimeError("タイルを結合できませんでした")
        vrt = None
        ds = gdal.Warp(
            output_path, vrt_path,
            dstSRS=f"EPSG:{epsg}",
            outputBounds=(xmin, ymin, xmax, ymax),
            xRes=resolution, yRes=resolution,
            resampleAlg="average",
            srcNodata=NODATA, dstNodata=NODATA,
            creationOptions=["COMPRESS=LZW", "TILED=YES", "BIGTIFF=IF_NEEDED"],
        )
        if ds is None:
            raise RuntimeError("DEMの切り出しに失敗しました")
        ds = None
    finally:
        if os.path.exists(vrt_path):
            os.remove(vrt_path)

    ds = gdal.Open(output_path, gdal.GA_Update)
    band = ds.GetRasterBand(1)
    data = band.ReadAsArray()
    missing = ~np.isfinite(data) | (data == NODATA)
    valid_fraction = 1.0 - float(missing.mean())
    if valid_fraction < MIN_VALID_FRACTION:
        ds = None
        os.remove(output_path)
        raise RuntimeError(
            f"表示範囲の約{(1 - valid_fraction) * 100:.0f}%にこのデータの標高値がありません"
            "（整備範囲の外か、未公開の区域です）。\n"
            "表示範囲を狭めるか、国土地理院のDEMを選んでください。"
        )

    filled = int(missing.sum())
    if filled:
        gdal.FillNodata(band, None, FILL_MAX_SEARCH_PX, 0)
    band.FlushCache()
    size = (ds.RasterXSize, ds.RasterYSize)
    ds = None
    return size[0], size[1], filled


def format_info(cols, rows, label, epsg, resolution, extra=""):
    info = f"{cols}×{rows} px  |  {label}  |  EPSG:{epsg}  |  {resolution} m/px"
    if extra:
        info += f"  |  {extra}"
    return info
