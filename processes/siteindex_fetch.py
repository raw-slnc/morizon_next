# This file is part of MORIZON NEXT.
# Copyright (C) 2026 Hideharu Masai
# Licensed under the GNU General Public License v3. See LICENSE and NOTICE.

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
                 lon_min: float, lat_min: float, lon_max: float, lat_max: float):
        super().__init__()
        self.zone = zone
        self.cache_base_dir = cache_base_dir
        self.output_dir = output_dir
        self.lon_min = lon_min
        self.lat_min = lat_min
        self.lon_max = lon_max
        self.lat_max = lat_max

        self.abort_flag = False

    def set_abort_flag(self, flag=True):
        self.abort_flag = flag

    def run(self):
        try:
            self.setAbortable.emit(True)
            self.postMessage.emit(f"座標系{self.zone}系の地位指数データを確認中…")
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
                    self.postDetail.emit(
                        f"座標系{self.zone}系\n"
                        f"約{mb_done:.1f}/{mb_total:.1f}MB取得済み\n"
                        f"（初回のみ、以降はキャッシュを再利用します）"
                    )
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

            self.postMessage.emit("解析範囲に合わせて切り出し中…")
            self.addProgress.emit(1)
            clipped = zoningkit_fetcher.clip_siteindex_to_extent(
                zone_cache, self.output_dir,
                self.lon_min, self.lat_min, self.lon_max, self.lat_max,
            )

            self.processFinished.emit(clipped)
        except InterruptedError:
            self.processFailed.emit("処理を中断しました。")
        except Exception as e:
            self.processFailed.emit(str(e))
