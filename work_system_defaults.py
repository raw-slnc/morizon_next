"""作業システムの既定値を、プラグイン本体と作業フォルダの外に保存する。"""

import os
import re
import shutil
import tempfile
import uuid
import zipfile

from qgis.PyQt.QtCore import QSettings
from qgis.core import QgsApplication

from .forest_zoning_main_dialog_costcsv_editor import (
    costcsv_sidecar_path, read_costcsv, read_costcsv_xlsx,
)


class WorkSystemDefaultsManager:
    GROUP = "/MORIZON/work_system_defaults"
    BUILTIN = "builtin"
    CUSTOM = "custom"
    CSV_NAME = "costcsv.csv"
    EXCEL_NAME = "work_system.xlsx"

    def _root(self):
        return os.path.join(QgsApplication.qgisSettingsDirPath(), "morizon_next", "work_system")

    def _snapshot_id(self):
        settings = QSettings()
        settings.beginGroup(self.GROUP)
        snapshot_id = str(settings.value("snapshot_id", ""))
        settings.endGroup()
        return snapshot_id if re.fullmatch(r"[0-9a-f]{32}", snapshot_id) else ""

    def custom_paths(self):
        """保存済みの (CSV, Excel)。不完全な保存は選択肢に出さない。"""
        snapshot_id = self._snapshot_id()
        if not snapshot_id:
            return None
        directory = os.path.join(self._root(), snapshot_id)
        csv_path = os.path.join(directory, self.CSV_NAME)
        excel_path = os.path.join(directory, self.EXCEL_NAME)
        if os.path.isfile(csv_path) and os.path.isfile(excel_path) and zipfile.is_zipfile(excel_path):
            try:
                read_costcsv(csv_path)
            except (OSError, ValueError):
                return None
            return csv_path, excel_path
        return None

    def selected_source(self):
        settings = QSettings()
        settings.beginGroup(self.GROUP)
        source = settings.value("source", self.BUILTIN)
        settings.endGroup()
        return self.CUSTOM if source == self.CUSTOM and self.custom_paths() else self.BUILTIN

    def select_source(self, source):
        if source not in (self.BUILTIN, self.CUSTOM):
            raise ValueError("作業システムの選択が不正です")
        if source == self.CUSTOM and not self.custom_paths():
            raise ValueError("保存済みの独自作業システムがありません")
        settings = QSettings()
        settings.beginGroup(self.GROUP)
        settings.setValue("source", source)
        settings.endGroup()
        settings.sync()

    def save_custom(self, csv_path, excel_path):
        """検査済みCSVと編集済みExcelを、新しい組として保存してから選択する。"""
        read_costcsv(csv_path)
        if not os.path.isfile(excel_path) or not zipfile.is_zipfile(excel_path):
            raise FileNotFoundError(excel_path)
        read_costcsv_xlsx(excel_path)
        root = self._root()
        os.makedirs(root, exist_ok=True)
        staged = tempfile.mkdtemp(prefix=".saving_", dir=root)
        old_id = self._snapshot_id()
        new_id = uuid.uuid4().hex
        try:
            target_csv = os.path.join(staged, self.CSV_NAME)
            target_excel = os.path.join(staged, self.EXCEL_NAME)
            shutil.copy2(csv_path, target_csv)
            shutil.copy2(excel_path, target_excel)
            sidecar = costcsv_sidecar_path(csv_path)
            if os.path.isfile(sidecar):
                shutil.copy2(sidecar, costcsv_sidecar_path(target_csv))
            read_costcsv(target_csv)
            read_costcsv_xlsx(target_excel)
            os.replace(staged, os.path.join(root, new_id))
            staged = None
            settings = QSettings()
            settings.beginGroup(self.GROUP)
            settings.setValue("snapshot_id", new_id)
            settings.setValue("source", self.CUSTOM)
            settings.endGroup()
            settings.sync()
        finally:
            if staged is not None:
                shutil.rmtree(staged, ignore_errors=True)
        if old_id and old_id != new_id:
            shutil.rmtree(os.path.join(root, old_id), ignore_errors=True)
