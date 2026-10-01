"""
長野県 建設部砂防課 0.5mメッシュDEM（令和3〜7年度 航空レーザ測量）
（forestry_operations_lite の nagano_sabo.py より移植。FOL本体とは実行時の依存を持たない）

配布元: https://www.geospatial.jp/ckan/dataset/r3-4-50cmdem
    市町村単位のZIP（1タイル=3000m(東西)×2500m(南北)のGeoTIFF、EPSG:6676）。
    ZIP全体は落とさず、必要なタイルだけを HTTP Range で取り出す（remote_zip.py）。

タイルコード "{行英字}{行数字}-{列英字}{列数字}"（例 "D8-d3"）:
    行インデックス = (行英字 A=0..)×10 + 行数字、列も同様（列英字は小文字）
    北端 = 115000 - 行インデックス×2500、西端 = -108000 + 列インデックス×3000
    2026-09-23、佐久穂町・木曽町・松本市の実タイルで完全一致を確認済み。

タイル→市町村→ZIP URL の対応は data/nagano_sabo_dem_index.json（全89ZIPの central directory を
実際に読んで作った索引、1706タイル）。データセット側が更新された場合は索引の作り直しが必要。
索引に無いタイルは「整備されていない」とみなす。
"""

import json
import os
import shutil
import tempfile

from .base import (
    DemSource, build_dem_from_tiles, check_cancel, extent_in_epsg, format_info, NODATA,
)
from .remote_zip import RemoteZipCatalog

EPSG = 6676
RESOLUTION = 1  # 0.5m配布。解析側の解像度判定（1/5/10m）に合わせて1mで保存する
ROW_STEP = 2500
COL_STEP = 3000
BASE_NORTHING = 115000
BASE_EASTING = -108000
TILE_MB = 120

_INDEX_PATH = os.path.join(os.path.dirname(__file__), "data", "nagano_sabo_dem_index.json")
_index_cache = None


def _load_index():
    global _index_cache
    if _index_cache is None:
        with open(_INDEX_PATH, encoding="utf-8") as f:
            _index_cache = json.load(f)
    return _index_cache


def _row_code(row_idx):
    return f"{chr(ord('A') + row_idx // 10)}{row_idx % 10}"


def _col_code(col_idx):
    return f"{chr(ord('a') + col_idx // 10)}{col_idx % 10}"


def tiles_for_extent(xmin, ymin, xmax, ymax):
    """EPSG:6676 の範囲に重なるタイルコードのリスト"""
    row_lo = int((BASE_NORTHING - ymax) // ROW_STEP)
    row_hi = int((BASE_NORTHING - ymin) // ROW_STEP)
    col_lo = int((xmin - BASE_EASTING) // COL_STEP)
    col_hi = int((xmax - BASE_EASTING) // COL_STEP)
    return [
        f"{_row_code(r)}-{_col_code(c)}"
        for r in range(max(row_lo, 0), min(row_hi, 259) + 1)
        for c in range(max(col_lo, 0), min(col_hi, 259) + 1)
    ]


def _merge_tile_parts(part_paths, out_path):
    """市町村境界のタイルは複数の市町村ZIPに分かれて入っているため、
    有効値のある部分を重ね合わせて1枚にする。"""
    if len(part_paths) == 1:
        os.replace(part_paths[0], out_path)
        return

    import numpy as np
    from osgeo import gdal

    base = gdal.Open(part_paths[0])
    gt, proj = base.GetGeoTransform(), base.GetProjection()
    cols, rows = base.RasterXSize, base.RasterYSize
    band = base.GetRasterBand(1)
    nodata = band.GetNoDataValue()
    if nodata is None:
        nodata = NODATA
    merged = band.ReadAsArray().astype(np.float32)
    base = None

    def valid(arr):
        return np.isfinite(arr) & (arr != nodata)

    merged_valid = valid(merged)
    for path in part_paths[1:]:
        ds = gdal.Open(path)
        if ds.RasterXSize != cols or ds.RasterYSize != rows:
            ds = None
            continue
        arr = ds.GetRasterBand(1).ReadAsArray().astype(np.float32)
        ds = None
        fill = ~merged_valid & valid(arr)
        merged[fill] = arr[fill]
        merged_valid |= fill
    merged[~merged_valid] = nodata

    ds = gdal.GetDriverByName("GTiff").Create(
        out_path, cols, rows, 1, gdal.GDT_Float32, options=["COMPRESS=LZW", "TILED=YES"]
    )
    ds.SetGeoTransform(gt)
    ds.SetProjection(proj)
    out_band = ds.GetRasterBand(1)
    out_band.SetNoDataValue(nodata)
    out_band.WriteArray(merged)
    ds = None
    for path in part_paths:
        os.remove(path)


class NaganoSaboSource(DemSource):
    key = "nagano_sabo"
    label = "長野県 砂防課 0.5m DEM（令和3〜7年度計測）"
    description = (
        "出典：長野県建設部砂防課「航空レーザ測量成果 0.5mメッシュDEM」（令和3〜7年度計測、"
        "G空間情報センター）。\n"
        "・長野県内の計測済み区域のみ。表示範囲の全体が計測済み区域に入るときだけ選べます\n"
        "・市町村ごとのZIPから必要なタイルだけを取り出します（1タイル 3km×2.5km、約120MB）\n"
        "・0.5mで配布されていますが、1mに変換して保存します\n"
        "利用条件は配布ページでご確認ください：https://www.geospatial.jp/ckan/dataset/r3-4-50cmdem"
    )

    def _codes(self, extent):
        return tiles_for_extent(*extent_in_epsg(extent, EPSG))

    def coverage_problem(self, extent):
        tile_to_cities = _load_index()["tile_to_cities"]
        missing = [code for code in self._codes(extent) if code not in tile_to_cities]
        if missing:
            return "表示範囲の一部が計測済み区域の外です"
        return None

    def estimate(self, extent):
        n = len(self._codes(extent))
        return f"表示範囲のタイル：{n}枚（約{n * TILE_MB}MBをダウンロード）"

    def fetch(self, extent, output_path, cancel_cb, reporter):
        bbox = extent_in_epsg(extent, EPSG)
        codes = tiles_for_extent(*bbox)
        index = _load_index()
        reporter.start(len(codes) + 1)

        work_dir = tempfile.mkdtemp(prefix=".nagano_sabo_", dir=os.path.dirname(output_path))
        catalog = RemoteZipCatalog(cancel_cb=cancel_cb)
        try:
            tile_paths = []
            for i, code in enumerate(codes):
                check_cancel(cancel_cb)
                reporter.message(f"タイルを取得中（{i + 1}/{len(codes)}枚目）")
                parts = []
                for city in index["tile_to_cities"].get(code, []):
                    for url in index["city_to_urls"].get(city, []):
                        check_cancel(cancel_cb)
                        reporter.detail(f"{code}：{city}のZIPを確認中")
                        name = catalog.find(url, f"{code}.tif")
                        if name is None:
                            continue

                        def on_progress(done, total, _code=code, _city=city):
                            reporter.detail(
                                f"{_code}：{_city}から {done / 1e6:.0f}/{total / 1e6:.0f}MB"
                            )

                        part_path = os.path.join(work_dir, f"{code}.part{len(parts)}.tif")
                        with open(part_path, "wb") as fh:
                            fh.write(catalog.read(url, name, progress_cb=on_progress))
                        parts.append(part_path)
                if parts:
                    tile_path = os.path.join(work_dir, f"{code}.tif")
                    _merge_tile_parts(parts, tile_path)
                    tile_paths.append(tile_path)
                reporter.step()

            if not tile_paths:
                raise RuntimeError("表示範囲のタイルを取得できませんでした。")

            check_cancel(cancel_cb)
            reporter.message("タイルを結合して保存中")
            reporter.detail(f"EPSG:{EPSG}・{RESOLUTION}m")
            cols, rows, filled = build_dem_from_tiles(tile_paths, bbox, EPSG, output_path, RESOLUTION)
            reporter.step()
        finally:
            catalog.close()
            shutil.rmtree(work_dir, ignore_errors=True)

        extra = f"{len(tile_paths)}タイル"
        if filled:
            extra += f"  |  NoData補間 {filled} px"
        return {"path": output_path, "info": format_info(cols, rows, self.label, EPSG, RESOLUTION, extra)}
