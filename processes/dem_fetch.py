# This file is part of MORIZON NEXT.
# Copyright (C) 2026 Hideharu Masai
# Licensed under the GNU General Public License v3. See LICENSE and NOTICE.

import os
import time

# QGIS-API
from qgis.PyQt.QtCore import QThread, pyqtSignal

from .. import dem_gaps
from ..dem_gaps import mask_path_for
from ..dem_loader import GSITileDEMLoader
from ..dem_sources.base import Cancelled, FetchReporter

# 最後の工程（保存）の表示を見せる最短の時間（秒）。保存はほとんどの範囲で一瞬で終わり、
# そのままでは読めないうちに次の知らせへ切り替わるため
LAST_STEP_MIN_SECONDS = 0.8


class _SignalReporter(FetchReporter):
    """取得元(dem_sources)からの進捗通知をスレッドのシグナルへ中継する"""

    def __init__(self, thread):
        self._thread = thread
        self.last_message_time = time.monotonic()

    def start(self, total):
        self._thread.processStarted.emit(total)

    def step(self, n=1):
        self._thread.addProgress.emit(n)

    def message(self, text):
        self.last_message_time = time.monotonic()
        self._thread.postMessage.emit(text)

    def detail(self, text):
        self._thread.postDetail.emit(text)


class DemFetchThread(QThread):
    """DEMブラウザで選んだ取得元(dem_sources.base.DemSource)でDEMを取得するスレッド。
    取得方法そのものは各取得元のモジュールが持ち、ここでは中断と進捗・結果の受け渡しだけを行う。"""

    processStarted = pyqtSignal(int)
    addProgress = pyqtSignal(int)
    postMessage = pyqtSignal(str)
    postDetail = pyqtSignal(str)
    processFinished = pyqtSignal(dict)  # {"path": str, "info": str}
    setAbortable = pyqtSignal(bool)
    processFailed = pyqtSignal(str)

    def __init__(self, source, extent, output_path: str, shape=None):
        """extent: WGS84経緯度の (lon_min, lat_min, lon_max, lat_max)
        shape: DEMブラウザで円（表示範囲により楕円）を選んだとき {"label": 形の名前, "ring": [(経度, 緯度), ...]}。
        取得は extent の四角で行い、保存後に形の外側を解析対象外（NoData）にする。四角なら None"""
        super().__init__()
        self.source = source
        self.extent = extent
        self.output_path = output_path
        self.shape = shape

        self.abort_flag = False

    def set_abort_flag(self, flag=True):
        self.abort_flag = flag

    def run(self):
        try:
            self.setAbortable.emit(True)
            # 前回取得したDEMのマスク（国土地理院DEMのみ作る）を、今回のDEMのものと取り違えないよう消しておく
            stale_mask_path = mask_path_for(self.output_path)
            if os.path.exists(stale_mask_path):
                try:
                    os.remove(stale_mask_path)
                except OSError:
                    # QGISで開いているなど（Windowsでは消せない）。取得は続け、新しいマスクを書けなければ知らせる
                    pass
            reporter = _SignalReporter(self)
            result = self.source.fetch(
                self.extent, self.output_path,
                cancel_cb=lambda: self.abort_flag,
                reporter=reporter,
            )
            self._exclude_water(result, reporter)
            if self.shape is not None:
                self._exclude_outside_shape(result, reporter)
            remaining = LAST_STEP_MIN_SECONDS - (time.monotonic() - reporter.last_message_time)
            if remaining > 0:
                time.sleep(remaining)
            self.processFinished.emit(result)
        except Cancelled:
            self.processFailed.emit("処理を中断しました。")
        except Exception as e:
            self.processFailed.emit(str(e))

    def _exclude_water(self, result, reporter):
        """取得元を問わず、DEMの水面（地理院ベクトルタイルの水域）を解析対象外にし、結果の知らせに書き足す。
        中断されたら、水面を外していないDEMを使わせないよう、保存したDEMとマスクを消して Cancelled を送出する"""
        current = {"message": None}

        def on_stage(message, done, total):
            if message != current["message"]:
                current["message"] = message
                reporter.start(total)
                reporter.message(message)
            reporter.step()
            reporter.detail(f"{done}/{total}枚")

        path = result["path"]
        water = dem_gaps.exclude_water(path, cancel_cb=lambda: self.abort_flag, progress=on_stage)
        if water is None:
            for stale in (path, path + ".aux.xml", mask_path_for(path)):
                try:
                    if os.path.exists(stale):
                        os.remove(stale)
                except OSError:
                    pass
            raise Cancelled()

        warnings = [result["warning"]] if result.get("warning") else []
        if not water["available"]:
            warnings.append(
                "水域データ（地理院ベクトルタイル）を読み込めないため（GDALにMVTの読み込み機能がありません）、"
                "水面にも値がある取得元では、水面が地形として解析されます。"
            )
        if water["water_px"]:
            ratio = water["water_px"] / max(water["total_px"], 1)
            result["info"] += f"  |  水面を対象外 {'0.1%未満' if ratio < 0.001 else f'{ratio * 100:.1f}%'}"
            message = "海・水面は解析の対象外（NoData）にしました。"
            if not any(message in text for text in warnings):
                warnings.append(message)
        if water["available"] and not water["complete"]:
            warnings.append("水域データ（地理院ベクトルタイル）を一部取得できなかったため、水面の一部が解析に含まれることがあります。")
        if water["mask_write_failed"]:
            warnings.append(
                "どこを対象外にしたかを示すファイル（dem_fetched_mask.tif）を書き込めませんでした。"
                "QGISで開いている場合は、レイヤーを削除してから取得し直してください。"
            )
        elif water["mask_path"] and water["water_px"] and not any(water["mask_path"] in text for text in warnings):
            warnings.append(f"どこを補ったか・対象外にしたかは次のファイルで確認できます：\n{water['mask_path']}")
        if water["mask_path"]:
            result["mask_path"] = water["mask_path"]
        result["warning"] = "\n".join(warnings)

    def _exclude_outside_shape(self, result, reporter):
        """DEMブラウザで選んだ円（表示範囲により楕円）の外側を解析対象外にし、結果の知らせに書き足す"""
        label = self.shape["label"]
        reporter.start(1)
        reporter.message(f"{label}の外側を解析の対象外にしています")
        outside = dem_gaps.exclude_outside_shape(result["path"], self.shape["ring"])
        reporter.step()
        ratio = outside["outside_px"] / max(outside["total_px"], 1)
        result["info"] += f"  |  {label}の外を対象外 {ratio * 100:.1f}%"
        warnings = [result["warning"]] if result.get("warning") else []
        warnings.append(f"範囲の形を「{label}」にしたため、{label}の外側は解析の対象外（NoData）にしました。")
        if outside["mask_write_failed"]:
            warnings.append(
                "どこを対象外にしたかを示すファイル（dem_fetched_mask.tif）を書き込めませんでした。"
                "QGISで開いている場合は、レイヤーを削除してから取得し直してください。"
            )
        elif outside["mask_path"]:
            result["mask_path"] = outside["mask_path"]
        result["warning"] = "\n".join(warnings)


class GsiCoverageCheckThread(QThread):
    """国土地理院DEMの解像度候補を確認するスレッド。"""

    processStarted = pyqtSignal(int)
    addProgress = pyqtSignal(int)
    postMessage = pyqtSignal(str)
    postDetail = pyqtSignal(str)
    processFinished = pyqtSignal(list)
    setAbortable = pyqtSignal(bool)
    processFailed = pyqtSignal(str)

    def __init__(self, extent, sources):
        """extent: WGS84経緯度の (lon_min, lat_min, lon_max, lat_max)"""
        super().__init__()
        self.extent = extent
        self.sources = sources
        self.abort_flag = False

    def set_abort_flag(self, flag=True):
        self.abort_flag = flag

    def run(self):
        try:
            self.setAbortable.emit(True)
            lon_min, lat_min, lon_max, lat_max = self.extent
            total = 0
            for source in self.sources:
                _, zoom, _, _ = source
                x0, y0, x1, y1 = GSITileDEMLoader._tile_range(
                    lon_min, lat_min, lon_max, lat_max, zoom
                )
                total += len(GSITileDEMLoader._coverage_sample_tiles(x0, y0, x1, y1))
            self.processStarted.emit(max(total, 1))
            self.postMessage.emit("国土地理院DEMの解像度を確認中")

            def on_checked(label, tx, ty):
                self.postDetail.emit(f"{label}: タイル x={tx}, y={ty}")
                self.addProgress.emit(1)

            result = GSITileDEMLoader.check_sources_coverage(
                lon_min, lat_min, lon_max, lat_max,
                self.sources,
                cancel_cb=lambda: self.abort_flag,
                progress_cb=on_checked,
            )
            if self.abort_flag:
                self.processFailed.emit("処理を中断しました。")
                return
            self.processFinished.emit(result)
        except Exception as e:
            self.processFailed.emit(str(e))
