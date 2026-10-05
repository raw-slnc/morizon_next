# This file is part of MORIZON NEXT.
# Copyright (C) 2026 Hideharu Masai
# Licensed under the GNU General Public License v3. See LICENSE and NOTICE.

"""
長野県 林務部 0.5mメッシュDEM（2013〜2014年度 航空レーザ測量）
（forestry_operations_lite の nagano_rinmu.py より移植。FOL本体とは実行時の依存を持たない）

配布元: https://www.geospatial.jp/ckan/dataset/nagano-dem
    県全域を12の地域ZIPに分けて配布（1タイル=1000m×750mのGeoTIFF、EPSG:6676）。
    砂防課DEM（nagano_sabo.py）とは計測年度・タイル体系とも別物。

タイルコード "{系2桁}{シート2文字}{行1桁}{列1桁}{外側Z}{内側Z}"（例 "08ID5721"）:
    シート（4000m×3000m）の位置は全県共通の式では求まらないため、公式の索引図
    （全体索引図_図郭.shp × 全体索引図_図郭名.shp、1073シート）から作った
    data/nagano_rinmu_sheet_index.json（{シート: [xmin, ymin, xmax, ymax]}）を引く。
    シート内の位置（末尾2桁）は2階層のZ曲線（1=左上/2=右上/3=左下/4=右下、
    外側2000m×1500m→内側1000m×750m）。2026-09-23、佐久地域の6タイルで一致を確認済み。

タイルがどの地域ZIPに入っているかの索引は無いため、各ZIPの central directory を
1回ずつ読み、タイルの在りかを探す（remote_zip.RemoteZipCatalog）。
"""

import json
import os
import shutil
import tempfile

from .base import (
    DemSource, build_dem_from_tiles, check_cancel, extent_in_epsg, format_info, sample_points,
)
from .remote_zip import RemoteZipCatalog

EPSG = 6676
RESOLUTION = 1  # 0.5m配布。解析側の解像度判定（1/5/10m）に合わせて1mで保存する
TILE_MB = 12

_SHEET_INDEX_PATH = os.path.join(os.path.dirname(__file__), "data", "nagano_rinmu_sheet_index.json")
_sheet_index_cache = None

_QUAD = {1: (0, 0), 2: (1, 0), 3: (0, 1), 4: (1, 1)}  # 番号 → (列, 行)

# 2026-09-23、CKAN nagano-dem のリソース一覧から取得（固定URL、Range対応確認済み）
_BASE_URL = ("https://gsic-opendata.s3.ap-northeast-1.amazonaws.com/local-gov/nagano/"
             "forestry-research-center/dem/nagano-dem/download/nagano/dem/")
ZIP_URLS = [_BASE_URL + name for name in (
    "H24-27(kamiina).zip",
    "H24-28(shimoina).zip",
    "H24-29(kiso).zip",
    "H24-30(saku).zip",
    "H24-31(jyousyou).zip",
    "H24-32(suwa).zip",
    "H24-33(matsumoto).zip",
    "H24-34(kitaazumi1).zip",
    "H24-34(kitaazumi2).zip",
    "H24-35(nagano).zip",
    "H24-36(hokusin1).zip",
    "H26-30(hokusin2).zip",
)]


def _load_sheet_index():
    global _sheet_index_cache
    if _sheet_index_cache is None:
        with open(_SHEET_INDEX_PATH, encoding="utf-8") as f:
            _sheet_index_cache = json.load(f)
    return _sheet_index_cache


def _quad_number(col, row):
    return next(k for k, v in _QUAD.items() if v == (col, row))


def tiles_for_extent(xmin, ymin, xmax, ymax):
    """EPSG:6676 の範囲に重なるタイルコードのリスト"""
    codes = []
    for sheet, (sxmin, symin, sxmax, symax) in _load_sheet_index().items():
        if sxmax < xmin or sxmin > xmax or symax < ymin or symin > ymax:
            continue
        col_lo = max(int((xmin - sxmin) // 1000), 0)
        col_hi = min(int((xmax - sxmin) // 1000), 3)
        row_lo = max(int((symax - ymax) // 750), 0)
        row_hi = min(int((symax - ymin) // 750), 3)
        for row in range(row_lo, row_hi + 1):
            for col in range(col_lo, col_hi + 1):
                outer_col, inner_col = divmod(col, 2)
                outer_row, inner_row = divmod(row, 2)
                codes.append(
                    f"{sheet}{_quad_number(outer_col, outer_row)}{_quad_number(inner_col, inner_row)}"
                )
    return codes


def _point_in_sheets(x, y):
    return any(
        xmin <= x <= xmax and ymin <= y <= ymax
        for xmin, ymin, xmax, ymax in _load_sheet_index().values()
    )


class NaganoRinmuSource(DemSource):
    key = "nagano_rinmu"
    label = "長野県 林務部 0.5m DEM（2013〜2014年度計測）"
    description = (
        "出典：長野県林務部「航空レーザ測量成果 0.5mメッシュDEM」（2013〜2014年度計測、"
        "G空間情報センター）。\n"
        "・長野県内のみ。表示範囲の全体が索引図の区画に入るときだけ選べます\n"
        "・地域ごとのZIPから必要なタイルだけを取り出します（1タイル 1km×0.75km、約12MB）。"
        "タイルの在りかの索引が無いため、最初に各ZIPの目録を確認します\n"
        "・計測から10年以上たっているため、その後の林道開設や崩壊は反映されていない場合があります\n"
        "・0.5mで配布されていますが、1mに変換して保存します\n"
        "利用条件は配布ページでご確認ください：https://www.geospatial.jp/ckan/dataset/nagano-dem"
    )

    def coverage_problem(self, extent):
        bbox = extent_in_epsg(extent, EPSG)
        if not all(_point_in_sheets(x, y) for x, y in sample_points(bbox)):
            return "表示範囲の一部が長野県の索引図の外です"
        return None

    def estimate(self, extent):
        n = len(tiles_for_extent(*extent_in_epsg(extent, EPSG)))
        return f"表示範囲のタイル：{n}枚（約{n * TILE_MB}MBをダウンロード）"

    def fetch(self, extent, output_path, cancel_cb, reporter):
        bbox = extent_in_epsg(extent, EPSG)
        codes = tiles_for_extent(*bbox)
        reporter.start(len(codes) + 1)

        work_dir = tempfile.mkdtemp(prefix=".nagano_rinmu_", dir=os.path.dirname(output_path))
        catalog = RemoteZipCatalog(cancel_cb=cancel_cb)
        # 隣り合うタイルは同じ地域ZIPに入っていることが多いので、直前に見つかったZIPから探す
        zip_order = list(ZIP_URLS)
        try:
            tile_paths = []
            for i, code in enumerate(codes):
                check_cancel(cancel_cb)
                reporter.message(f"タイルを取得中（{i + 1}/{len(codes)}枚目）")
                for url in zip_order:
                    check_cancel(cancel_cb)
                    label = os.path.basename(url)
                    reporter.detail(f"{code}：{label}を確認中")
                    name = catalog.find(url, f"{code}_2013.tif") or catalog.find(url, f"{code}_2014.tif")
                    if name is None:
                        continue

                    def on_progress(done, total, _code=code, _label=label):
                        reporter.detail(f"{_code}：{_label}から {done / 1e6:.1f}/{total / 1e6:.1f}MB")

                    tile_path = os.path.join(work_dir, f"{code}.tif")
                    with open(tile_path, "wb") as fh:
                        fh.write(catalog.read(url, name, progress_cb=on_progress))
                    tile_paths.append(tile_path)
                    zip_order.remove(url)
                    zip_order.insert(0, url)
                    break
                reporter.step()

            if not tile_paths:
                raise RuntimeError("表示範囲のタイルを取得できませんでした。")

            check_cancel(cancel_cb)
            reporter.message("タイルを結合して保存中")
            reporter.detail(f"座標系 EPSG:{EPSG}・{RESOLUTION}m として保存")
            cols, rows, filled = build_dem_from_tiles(tile_paths, bbox, EPSG, output_path, RESOLUTION)
            reporter.step()
        finally:
            catalog.close()
            shutil.rmtree(work_dir, ignore_errors=True)

        extra = f"{len(tile_paths)}タイル"
        if filled:
            extra += f"  |  NoData補間 {filled} px"
        return {"path": output_path, "info": format_info(cols, rows, self.label, EPSG, RESOLUTION, extra)}
