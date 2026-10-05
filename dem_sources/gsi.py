# This file is part of MORIZON NEXT.
# Copyright (C) 2026 Hideharu Masai
# Licensed under the GNU General Public License v3. See LICENSE and NOTICE.

"""
国土地理院 標高タイル（DEM1A/5A/10B）と AWS Terrarium（全球）。
タイルの取得・組み立ては dem_loader.GSITileDEMLoader が行い、ここでは
Webメルカトル（EPSG:3857）で組み立てたDEMを平面直角座標系へ変換して保存する。
"""

import os
import tempfile

from ..dem_loader import (
    GSITileDEMLoader, choose_local_crs_epsg, nominal_resolution, reproject_geotiff, save_as_geotiff,
)
from .base import Cancelled, DemSource, format_info


class GsiSource(DemSource):
    key = "gsi"
    label = "国土地理院 DEM1A/5A/10B"
    description = (
        "出典：国土地理院（地理院タイル DEM1A/DEM5A/DEM10B）。利用の際は出典の明示が必要です。\n"
        "・DEM1A 1m：航空レーザ測量。測量実施エリアのみ（伊豆半島・山間部等）\n"
        "・DEM5A 5m：標準解像度、全国で利用可能\n"
        "・DEM10B 10m：広域解析向け、全国で利用可能\n"
        "取得前に、表示範囲の全体を同じ解像度でカバーできるかを確認し、解像度を1つにそろえます。\n"
        "正式な利用規約は国土地理院の該当ページでご確認ください。"
    )

    def __init__(self):
        # 取得に使うタイル方式。DEMブラウザで解像度を決めたあと呼び出し側が1つに絞る
        self.tile_sources = GSITileDEMLoader.TILE_SOURCES

    def fetch(self, extent, output_path, cancel_cb, reporter):
        lon_min, lat_min, lon_max, lat_max = extent
        reporter.message("DEMタイルを取得中")
        reporter.detail("")

        seen_totals = set()

        def on_tile_progress(tiles_done, tiles_total, bytes_done):
            if tiles_total not in seen_totals:
                # 新しい解像度での取得開始（フォールバック含む）：上限を+1（保存分）で設定
                seen_totals.add(tiles_total)
                reporter.start(tiles_total + 1)
            reporter.step()
            reporter.detail(f"{tiles_done}/{tiles_total}枚・約{bytes_done / (1024 * 1024):.1f}MB取得済み")

        loader = GSITileDEMLoader()
        loader.fetch_for_extent(
            lon_min, lat_min, lon_max, lat_max,
            sources=self.tile_sources,
            cancel_cb=cancel_cb,
            progress_cb=on_tile_progress,
        )
        if loader._cancelled:
            raise Cancelled()
        if loader.data is None:
            raise RuntimeError("DEMの取得に失敗しました。\n" + "\n".join(loader.last_errors[-5:]))

        # タイルはEPSG:3857で、そのままでは傾斜・距離・面積が緯度に応じて歪む。
        # いったん保存してから実距離を保つ平面直角座標系へ変換する
        epsg = choose_local_crs_epsg((lon_min + lon_max) / 2, (lat_min + lat_max) / 2)
        resolution = nominal_resolution(loader.cell_size)

        reporter.message("平面直角座標系に変換して保存中")
        reporter.detail(f"座標系 EPSG:{epsg}・{resolution}m として保存")
        reporter.step()
        fd, mercator_path = tempfile.mkstemp(suffix=".tif", dir=os.path.dirname(output_path) or None)
        os.close(fd)
        try:
            save_as_geotiff(loader, mercator_path)
            cols, rows = reproject_geotiff(mercator_path, output_path, epsg, resolution)
        finally:
            os.remove(mercator_path)

        extra = f"NoData補間 {loader._filled_nodata_count} px" if loader._filled_nodata_count else ""
        return {
            "path": output_path,
            "info": format_info(cols, rows, loader._used_source_label, epsg, resolution, extra),
        }


class TerrariumSource(GsiSource):
    key = "terrarium"
    label = "AWS Terrarium（全球、登録不要）"
    description = (
        "出典：AWS Terrain Tiles (Mapzen Terrarium)。全球カバー、無料・登録不要。\n"
        "国土地理院データが取得できない場合のフォールバック用途を想定。\n"
        "正式な利用条件はAWS Terrain Tilesの配布元でご確認ください。"
    )

    def __init__(self):
        self.tile_sources = GSITileDEMLoader.TERRARIUM_SOURCES
