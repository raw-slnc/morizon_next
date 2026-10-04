# This file is part of MORIZON NEXT.
# Copyright (C) 2026 Hideharu Masai
# Licensed under the GNU General Public License v3. See LICENSE and NOTICE.

import os

# QGIS-API
from qgis.PyQt.QtCore import QThread, pyqtSignal

from .. import fgd_fetcher
from ..utils import get_tiff_info


class BuildingRoadFetchThread(QThread):
    processStarted = pyqtSignal(int)
    addProgress = pyqtSignal(int)
    postMessage = pyqtSignal(str)
    postDetail = pyqtSignal(str)
    processFinished = pyqtSignal(dict)  # {"building": path|None, "road": path|None}
    setAbortable = pyqtSignal(bool)
    processFailed = pyqtSignal(str)

    def __init__(self, session, mesh_codes: list, cache_dir: str, output_dir: str,
                 lon_min: float, lat_min: float, lon_max: float, lat_max: float,
                 dem_filepath: str = None, clip_to_extent=True):
        super().__init__()
        self.session = session
        self.mesh_codes = mesh_codes
        self.cache_dir = cache_dir
        self.output_dir = output_dir
        self.lon_min = lon_min
        self.lat_min = lat_min
        self.lon_max = lon_max
        self.lat_max = lat_max
        self.dem_filepath = dem_filepath
        self.clip_to_extent = clip_to_extent
        self._dem_crs = None
        self._dem_crs_resolved = False

        self.abort_flag = False

    def set_abort_flag(self, flag=True):
        self.abort_flag = flag

    def _get_dem_crs(self):
        """DEMのCRSを返す(WGS84度単位のまま保存すると、DEMの解像度(メートル)が
        度単位の範囲に誤って適用され0x0ピクセルのラスター生成に失敗するバグを踏んだため、
        取得したベクタデータはDEMのCRSに合わせて保存する)。取得できない場合はEPSG:4326"""
        if not self._dem_crs_resolved:
            self._dem_crs_resolved = True
            self._dem_crs = "EPSG:4326"
            if self.dem_filepath:
                try:
                    crs = get_tiff_info(self.dem_filepath)["crs"]
                    self._dem_crs = crs.authid() or crs.toWkt()
                except Exception:
                    pass
        return self._dem_crs

    def _fetch_and_merge(self, type_code: str, label: str, output_path: str,
                         include_layer_names=None, allow_empty_output=False):
        def on_progress(done, total, file_name):
            if done == 1:
                self.processStarted.emit(total + 1)
            self.addProgress.emit(1)
            self.postDetail.emit(f"{label} {done}/{total}件取得済み（{file_name}）")

        zip_paths = fgd_fetcher.fetch_meshes(
            self.session, type_code, self.mesh_codes, self.cache_dir,
            progress_cb=on_progress, cancel_cb=lambda: self.abort_flag,
        )
        if self.abort_flag:
            raise InterruptedError()
        if not zip_paths:
            if allow_empty_output:
                fgd_fetcher.create_empty_line_shapefile(
                    output_path, self._get_dem_crs(), os.path.splitext(os.path.basename(output_path))[0]
                )
                return output_path, True
            return None

        self.postDetail.emit(f"{label} を範囲でまとめています…")
        ok = fgd_fetcher.merge_layers(
            zip_paths, output_path,
            self.lon_min, self.lat_min, self.lon_max, self.lat_max,
            include_layer_names=include_layer_names,
            driver_format="ESRI Shapefile",
            dst_crs=self._get_dem_crs(),
            clip_to_extent=self.clip_to_extent,
        )
        self.addProgress.emit(1)
        if ok:
            return (output_path, False) if allow_empty_output else output_path
        if allow_empty_output:
            fgd_fetcher.create_empty_line_shapefile(
                output_path, self._get_dem_crs(), os.path.splitext(os.path.basename(output_path))[0]
            )
            return output_path, True
        return None

    def run(self):
        try:
            self.setAbortable.emit(True)
            result = {}

            self.postMessage.emit("道路縁データを取得中…")
            road_path = os.path.join(self.output_dir, "ROAD", "road_edge.shp")
            road_result = self._fetch_and_merge(
                fgd_fetcher.TYPE_CODE_ROAD_EDGE, "道路縁", road_path,
                include_layer_names=fgd_fetcher.ROAD_LAYERS,
                allow_empty_output=True,
            )
            if isinstance(road_result, tuple):
                result["road"], result["road_empty"] = road_result
            else:
                result["road"] = road_result
                result["road_empty"] = False
            if self.abort_flag:
                self.processFailed.emit("処理を中断しました。")
                return

            self.postMessage.emit("建物ポリゴンデータを取得中…")
            building_path = os.path.join(self.output_dir, "TATEMONO", "building.shp")
            result["building"] = self._fetch_and_merge(
                fgd_fetcher.TYPE_CODE_BUILDING, "建物", building_path,
                include_layer_names=fgd_fetcher.BUILDING_LAYERS,
            )
            if self.abort_flag:
                self.processFailed.emit("処理を中断しました。")
                return

            self.processFinished.emit(result)
        except InterruptedError:
            self.processFailed.emit("処理を中断しました。")
        except Exception as e:
            self.processFailed.emit(str(e))
