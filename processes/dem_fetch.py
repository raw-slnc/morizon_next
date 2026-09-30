# QGIS-API
from qgis.PyQt.QtCore import QThread, pyqtSignal

from ..dem_loader import GSITileDEMLoader, save_as_geotiff


class DemFetchThread(QThread):
    processStarted = pyqtSignal(int)
    addProgress = pyqtSignal(int)
    postMessage = pyqtSignal(str)
    postDetail = pyqtSignal(str)
    processFinished = pyqtSignal(dict)  # {"path": str, "info": str}
    setAbortable = pyqtSignal(bool)
    processFailed = pyqtSignal(str)

    def __init__(self, lon_min: float, lat_min: float, lon_max: float, lat_max: float,
                 output_path: str, sources=None):
        super().__init__()
        self.lon_min = lon_min
        self.lat_min = lat_min
        self.lon_max = lon_max
        self.lat_max = lat_max
        self.output_path = output_path
        self.sources = sources

        self.abort_flag = False

    def set_abort_flag(self, flag=True):
        self.abort_flag = flag

    def run(self):
        try:
            self.setAbortable.emit(True)
            self.postMessage.emit("DEMタイルを取得中…")
            self.postDetail.emit("")

            seen_totals = set()

            def on_tile_progress(tiles_done, tiles_total, bytes_done):
                if tiles_total not in seen_totals:
                    # 新しい解像度での取得開始（フォールバック含む）：上限を+1（保存分）で設定
                    seen_totals.add(tiles_total)
                    self.processStarted.emit(tiles_total + 1)
                self.addProgress.emit(1)
                mb_done = bytes_done / (1024 * 1024)
                self.postDetail.emit(f"{tiles_done}/{tiles_total}枚・約{mb_done:.1f}MB取得済み")

            loader = GSITileDEMLoader()
            loader.fetch_for_extent(
                self.lon_min, self.lat_min, self.lon_max, self.lat_max,
                sources=self.sources,
                cancel_cb=lambda: self.abort_flag,
                progress_cb=on_tile_progress,
            )

            if loader._cancelled:
                self.processFailed.emit("処理を中断しました。")
                return

            if loader.data is None:
                errors = "\n".join(loader.last_errors[-5:])
                self.processFailed.emit(
                    f"DEMの取得に失敗しました。\n{errors}"
                )
                return

            info = loader.info_text()
            self.postMessage.emit("GeoTIFFに保存中")
            self.postDetail.emit(info)
            self.addProgress.emit(1)
            save_as_geotiff(loader, self.output_path)

            self.processFinished.emit({"path": self.output_path, "info": info})
        except Exception as e:
            self.processFailed.emit(str(e))
