# This file is part of MORIZON NEXT.
# Copyright (C) 2026 Hideharu Masai
# Licensed under the GNU General Public License v3. See LICENSE and NOTICE.

import os
import time

# QGIS-API
from qgis.PyQt.QtCore import QThread, pyqtSignal

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

    def __init__(self, source, extent, output_path: str):
        """extent: WGS84経緯度の (lon_min, lat_min, lon_max, lat_max)"""
        super().__init__()
        self.source = source
        self.extent = extent
        self.output_path = output_path

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
            remaining = LAST_STEP_MIN_SECONDS - (time.monotonic() - reporter.last_message_time)
            if remaining > 0:
                time.sleep(remaining)
            self.processFinished.emit(result)
        except Cancelled:
            self.processFailed.emit("処理を中断しました。")
        except Exception as e:
            self.processFailed.emit(str(e))


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
