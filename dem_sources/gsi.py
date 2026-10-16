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

from .. import dem_gaps
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
        "・DEM5A 5m：航空レーザ測量。広く整備されているが、湖・川などの水面は値がない\n"
        "・DEM10B 10m：広域解析向け、全国で利用可能\n"
        "取得前に、表示範囲の全体を同じ解像度でカバーできるかを確認し、解像度を1つにそろえます。\n"
        "海と水面（地理院ベクトルタイルの水域で判定）は解析対象外（NoData）にし、"
        "選んだ解像度が無い陸地は粗い解像度（5A→10B）で補います。\n"
        "正式な利用規約は国土地理院の該当ページでご確認ください。"
    )

    def __init__(self):
        # 取得に使うタイル方式。DEMブラウザで解像度を決めたあと呼び出し側が1つに絞る
        self.tile_sources = GSITileDEMLoader.TILE_SOURCES
        self.allow_partial_tiles = True
        # 欠けを海・水面・陸の欠けに分ける（dem_gaps）。Terrarium は国内の水域データが使えないので行わない
        self.classify_gaps = True

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

        current_stage = {"message": None}

        def on_stage(message, done, total):
            if message != current_stage["message"]:
                current_stage["message"] = message
                reporter.start(total)
                reporter.message(message)
            reporter.step()
            reporter.detail(f"{done}/{total}枚")

        loader = GSITileDEMLoader()
        loader.fetch_for_extent(
            lon_min, lat_min, lon_max, lat_max,
            sources=self.tile_sources,
            cancel_cb=cancel_cb,
            progress_cb=on_tile_progress,
            allow_partial_tiles=self.allow_partial_tiles,
            classify_gaps=self.classify_gaps,
            stage_cb=on_stage,
        )
        if loader._cancelled:
            raise Cancelled()
        if loader.data is None:
            raise RuntimeError("DEMの取得に失敗しました。\n" + "\n".join(loader.last_errors[-5:]))

        # タイルはEPSG:3857で、そのままでは傾斜・距離・面積が緯度に応じて歪む。
        # いったん保存してから実距離を保つ平面直角座標系へ変換する
        epsg = choose_local_crs_epsg((lon_min + lon_max) / 2, (lat_min + lat_max) / 2)
        resolution = nominal_resolution(loader.cell_size)

        if current_stage["message"] is not None:
            reporter.start(1)
        reporter.message("平面直角座標系に変換して保存中")
        reporter.detail(f"座標系 EPSG:{epsg}・{resolution}m として保存")
        reporter.step()
        mask_path = None
        mask_write_failed = False
        fd, mercator_path = tempfile.mkstemp(suffix=".tif", dir=os.path.dirname(output_path) or None)
        os.close(fd)
        try:
            save_as_geotiff(loader, mercator_path)
            cols, rows = reproject_geotiff(mercator_path, output_path, epsg, resolution)
            if loader.gap_mask is not None:
                # 欠けの分類も、DEMと同じグリッドで隣に保存する（どこを補ったか・解析対象外かを示す）
                try:
                    dem_gaps.save_mask_geotiff(loader.gap_mask, loader.gt, loader.crs_wkt, mercator_path)
                    mask_path = dem_gaps.mask_path_for(output_path)
                    reproject_geotiff(mercator_path, mask_path, epsg, resolution,
                                      resample_alg="near", nodata=dem_gaps.MASK_NODATA)
                except Exception:
                    # 前回のマスクをQGISで開いているなど（Windowsでは上書きできない）。DEMはそのまま使う
                    mask_path = None
                    mask_write_failed = True
        finally:
            try:
                os.remove(mercator_path)
            except OSError:
                pass

        if loader.gap_mask is None:
            return self._result_without_mask(loader, output_path, cols, rows, epsg, resolution)

        # 内訳は保存したマスクから数える。書けなかったときは取得時の分類（座標変換前）から数える
        summary = dem_gaps.mask_summary(mask_path) if mask_path else dem_gaps.summarize_mask(loader.gap_mask)
        extra_parts = []
        for key, name in (("sea", "海"), ("water", "水面"),
                          ("filled_5a", "5Aで補完"), ("filled_10b", "10Bで補完")):
            if summary[key] > 0:
                extra_parts.append(f"{name} {_percent(summary[key])}")
        if summary["interpolated_px"]:
            extra_parts.append(f"補間 {summary['interpolated_px']} px")
        extra = " / ".join(extra_parts)

        warnings = []
        if summary["sea"] > 0 or summary["water"] > 0:
            warnings.append("海・水面は解析の対象外（NoData）にしました。")
        if summary["land_fill_ratio"] >= dem_gaps.LAND_FILL_WARN_RATIO:
            warnings.append(
                f"陸地の{_percent(summary['land_fill_ratio'])}は{loader._used_source_label}が無いため、"
                "粗い解像度の値で補いました。補った部分は傾斜などがなだらかに出ます。"
                "より粗い解像度で取り直すことをおすすめします。"
            )
        if not loader.gap_notes.get("water_data", True):
            warnings.append(
                "水域データ（地理院ベクトルタイル）を一部取得できなかったため、"
                "海は「10Bにも値が無い部分」として判定しました。"
            )
        if loader.gap_notes.get("fallback_interpolated"):
            warnings.append("補う値を取得できなかった陸の欠けは、周囲の値から補間しました。")
        if mask_write_failed:
            warnings.append(
                "どこを補ったか・対象外にしたかを示すファイル（dem_fetched_mask.tif）を書き込めませんでした。"
                "QGISで開いている場合は、レイヤーを削除してから取得し直してください。"
            )
        elif summary["sea"] > 0 or summary["water"] > 0 or summary["filled_5a"] > 0 or summary["filled_10b"] > 0:
            warnings.append(f"どこを補ったか・対象外にしたかは次のファイルで確認できます：\n{mask_path}")
        return {
            "path": output_path,
            "mask_path": mask_path,
            "info": format_info(cols, rows, loader._used_source_label, epsg, resolution, extra),
            "warning": "\n".join(warnings),
        }

    @staticmethod
    def _result_without_mask(loader, output_path, cols, rows, epsg, resolution):
        extra_parts = []
        if loader._filled_nodata_count:
            extra_parts.append(f"NoData補間 {loader._filled_nodata_count} px")
        if loader._missing_tiles_count:
            extra_parts.append(
                f"未取得タイル {loader._missing_tiles_count}/{loader._missing_tiles_total}枚"
            )
        extra = " / ".join(extra_parts)
        warning = ""
        if loader._missing_tiles_count:
            warning = (
                "一部のDEMタイルを取得できませんでした。"
                "海域などDEM未提供部分を含む可能性があります。"
            )
        return {
            "path": output_path,
            "info": format_info(cols, rows, loader._used_source_label, epsg, resolution, extra),
            "warning": warning,
        }


def _percent(ratio):
    if ratio < 0.001:
        return "0.1%未満"
    return f"{ratio * 100:.1f}%"


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
        self.allow_partial_tiles = False
        self.classify_gaps = False
