# This file is part of MORIZON NEXT.
# Copyright (C) 2026 Hideharu Masai
# Licensed under the GNU General Public License v3. See LICENSE and NOTICE.

import os
import time

# QGIS-API
from qgis.PyQt.QtCore import QThread, pyqtSignal

from .. import zoningkit_fetcher


class SiteIndexFetchThread(QThread):
    processStarted = pyqtSignal(int)
    addProgress = pyqtSignal(int)
    setProgress = pyqtSignal(int)
    postMessage = pyqtSignal(str)
    postDetail = pyqtSignal(str)
    processFinished = pyqtSignal(dict)  # {"NPP": path, "SRAD": path, "VTEX": path}
    setAbortable = pyqtSignal(bool)
    processFailed = pyqtSignal(str)

    def __init__(self, zone: int, cache_base_dir: str, output_dir: str,
                 lon_min: float, lat_min: float, lon_max: float, lat_max: float, dem_path: str = ""):
        """dem_path: 取得済みDEM。指定すると、その陸地が地位指数データの範囲内かを確かめる"""
        super().__init__()
        self.zone = zone
        self.cache_base_dir = cache_base_dir
        self.output_dir = output_dir
        self.lon_min = lon_min
        self.lat_min = lat_min
        self.lon_max = lon_max
        self.lat_max = lat_max
        self.dem_path = dem_path
        # 地位指数データの整備範囲外で止めたとき True（呼び出し側が取得済みのDEMを解放する）
        self.out_of_range = False

        self.abort_flag = False

    def set_abort_flag(self, flag=True):
        self.abort_flag = flag

    def run(self):
        try:
            self.setAbortable.emit(True)
            self.postMessage.emit(f"座標系{self.zone}系の地位指数データを確認中")
            self.postDetail.emit("")

            # bytes_total(KB単位)が判明した時点でバーの上限を設定する。
            # 判明前(中央ディレクトリ読み込み中)は上限未確定として扱う
            state = {"max_set": False}

            def on_fetch_progress(bytes_done, bytes_total):
                kb_done = bytes_done // 1024
                kb_total = bytes_total // 1024
                if not state["max_set"] and kb_total > 0:
                    self.processStarted.emit(kb_total + 1)  # +1は切り出し工程分
                    state["max_set"] = True
                if state["max_set"]:
                    self.setProgress.emit(kb_done)
                mb_done = bytes_done / (1024 * 1024)
                mb_total = bytes_total / (1024 * 1024)
                if bytes_total > 0:
                    self.postDetail.emit(f"約{mb_done:.1f}/{mb_total:.1f}MB取得済み")
                else:
                    self.postDetail.emit(f"座標系{self.zone}系のデータを確認中…")

            zone_cache = zoningkit_fetcher.ensure_zone_cache(
                self.zone, self.cache_base_dir,
                progress_cb=on_fetch_progress,
                cancel_cb=lambda: self.abort_flag,
            )

            if not state["max_set"]:
                # 全てキャッシュ済みで実際のダウンロードが発生しなかった場合
                self.processStarted.emit(1)
                self.postDetail.emit(f"座標系{self.zone}系のキャッシュを使用します")

            if self.abort_flag:
                self.processFailed.emit("処理を中断しました。")
                return

            if not zone_cache:
                self.processFailed.emit(
                    f"座標系{self.zone}系の地位指数データが見つかりませんでした。"
                )
                return

            self.postMessage.emit("解析範囲に合わせて切り出し中")
            self.addProgress.emit(1)
            clipped = zoningkit_fetcher.clip_siteindex_to_extent(
                zone_cache, self.output_dir,
                self.lon_min, self.lat_min, self.lon_max, self.lat_max,
            )

            # 地位指数データの外（離島など）なら、ここで止める（後のスコアリング・ゾーニングに進めないため）
            if self.dem_path and os.path.exists(self.dem_path):
                if not zoningkit_fetcher.siteindex_has_data(self.dem_path, clipped.values()):
                    # 判定のために切り出した（中身の無い）ファイルは残さない
                    for path in clipped.values():
                        for target in (path, path + ".aux.xml"):
                            if os.path.exists(target):
                                try:
                                    os.remove(target)
                                except OSError:
                                    # QGISで開いているなど（Windowsでは消せない）。中止は続ける
                                    pass
                    # 知らせは呼び出し側が、DEMを解放してから出す
                    self.out_of_range = True
                    self.processFailed.emit(zoningkit_fetcher.SITEINDEX_OUT_OF_RANGE_TITLE)
                    return

            self.processFinished.emit(clipped)
        except InterruptedError:
            self.processFailed.emit("処理を中断しました。")
        except Exception as e:
            self.processFailed.emit(str(e))


class DemRangeCheckDisplayThread(QThread):
    """地位指数データの整備範囲外と分かったとき、中止の知らせの前に見せる進捗バー（表示だけで、何もしない）。
    判定は一瞬で終わるが、すぐに中止の窓を出すと何が起きたのか分かりにくいため、照合している様子を見せる"""

    processStarted = pyqtSignal(int)
    addProgress = pyqtSignal(int)
    postMessage = pyqtSignal(str)
    postDetail = pyqtSignal(str)
    processFinished = pyqtSignal(dict)
    setAbortable = pyqtSignal(bool)
    processFailed = pyqtSignal(str)

    SECONDS = 5
    STEPS = 50

    def set_abort_flag(self, flag=True):
        pass

    def run(self):
        self.setAbortable.emit(False)
        self.processStarted.emit(self.STEPS)
        self.postMessage.emit("取得したDEMの範囲を照合しています")
        for _ in range(self.STEPS):
            time.sleep(self.SECONDS / self.STEPS)
            self.addProgress.emit(1)
        self.processFinished.emit({})
