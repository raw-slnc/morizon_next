import os
import tempfile

# QGIS-API
from qgis.PyQt.QtCore import QThread, pyqtSignal

from ..dem_loader import (
    GSITileDEMLoader, choose_local_crs_epsg, nominal_resolution, reproject_geotiff, save_as_geotiff,
)


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

            # タイルはEPSG:3857で、そのままでは傾斜・距離・面積が緯度に応じて歪む。
            # いったん保存してから実距離を保つ平面直角座標系へ変換する
            center_lon = (self.lon_min + self.lon_max) / 2
            center_lat = (self.lat_min + self.lat_max) / 2
            epsg = choose_local_crs_epsg(center_lon, center_lat)
            resolution = nominal_resolution(loader.cell_size)

            self.postMessage.emit("平面直角座標系に変換して保存中")
            self.postDetail.emit(f"EPSG:{epsg}・{resolution}m")
            self.addProgress.emit(1)
            output_dir = os.path.dirname(self.output_path) or None
            fd, mercator_path = tempfile.mkstemp(suffix=".tif", dir=output_dir)
            os.close(fd)
            try:
                save_as_geotiff(loader, mercator_path)
                cols, rows = reproject_geotiff(mercator_path, self.output_path, epsg, resolution)
            finally:
                os.remove(mercator_path)

            info = (
                f"{cols}×{rows} px  |  {loader._used_source_label}  |  "
                f"EPSG:{epsg}  |  {resolution} m/px"
            )
            if loader._filled_nodata_count:
                info += f"  |  NoData補間 {loader._filled_nodata_count} px"
            self.processFinished.emit({"path": self.output_path, "info": info})
        except Exception as e:
            self.processFailed.emit(str(e))
