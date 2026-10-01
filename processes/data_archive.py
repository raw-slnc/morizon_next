# This file is part of MORIZON NEXT.
# Copyright (C) 2026 Hideharu Masai
# Licensed under the GNU General Public License v3. See LICENSE and NOTICE.

import os
import time
import zipfile

# QGIS-API
from qgis.PyQt.QtCore import QThread, pyqtSignal

from .. import morizon_data
from ..constants import DATA_INFO_FILE_NAME

_CHUNK = 1024 * 1024


class _Cancelled(Exception):
    pass


class DataArchiveThread(QThread):
    """保存ファイル出力のZIPを書き出すスレッド。
    (ZIP内のパス, ファイル)を1MBずつ圧縮して書き、最後に中身を検証してから所定の名前に置き換える。
    中断・失敗した場合は書きかけのZIPを消す（管理フォルダのクリアは呼び出し側が成功時だけ行う）。
    結果は processFinished({"status": "done" | "cancelled"}) で返す。"""

    processStarted = pyqtSignal(int)
    addProgress = pyqtSignal(int)
    postMessage = pyqtSignal(str)
    postDetail = pyqtSignal(str)
    processFinished = pyqtSignal(dict)
    setAbortable = pyqtSignal(bool)
    processFailed = pyqtSignal(str)

    def __init__(self, entries: list, archive_path: str):
        super().__init__()
        self.entries = entries
        self.archive_path = archive_path
        self.abort_flag = False

    def set_abort_flag(self, flag=True):
        self.abort_flag = flag

    def run(self):
        temp_path = self.archive_path + ".part"
        try:
            self.setAbortable.emit(True)
            self.postMessage.emit("ZIPに保存中")
            self.processStarted.emit(100)
            self._write(temp_path)

            self.setAbortable.emit(False)
            self.postMessage.emit("保存したZIPを確認中")
            self.postDetail.emit("")
            with zipfile.ZipFile(temp_path) as zf:
                broken = zf.testzip()
            if broken is not None:
                raise RuntimeError(f"ZIPの検証に失敗しました: {broken}")
            os.replace(temp_path, self.archive_path)
            self.processFinished.emit({"status": "done"})
        except _Cancelled:
            self.processFinished.emit({"status": "cancelled"})
        except Exception as e:
            self.processFailed.emit(str(e))
        finally:
            if os.path.exists(temp_path):
                os.remove(temp_path)

    def _write(self, temp_path):
        total = sum(os.path.getsize(path) for _, path in self.entries) or 1
        done = 0
        percent = 0
        last_detail = 0.0
        with zipfile.ZipFile(temp_path, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.writestr(DATA_INFO_FILE_NAME, morizon_data.info_text().encode("utf-8-sig"))
            for arcname, path in self.entries:
                info = zipfile.ZipInfo.from_file(path, arcname)
                info.compress_type = zipfile.ZIP_DEFLATED
                # 2GBを超えるDEMでも書けるよう、ZIP64を許可しておく
                with open(path, "rb") as src, zf.open(info, "w", force_zip64=True) as dst:
                    while True:
                        if self.abort_flag:
                            raise _Cancelled()
                        chunk = src.read(_CHUNK)
                        if not chunk:
                            break
                        dst.write(chunk)
                        done += len(chunk)
                        new_percent = min(int(done * 100 / total), 100)
                        if new_percent > percent:
                            self.addProgress.emit(new_percent - percent)
                            percent = new_percent
                        now = time.monotonic()
                        if now - last_detail >= 0.15:
                            last_detail = now
                            self.postDetail.emit(
                                f"{os.path.basename(path)}  {done / 1e6:.0f}/{total / 1e6:.0f}MB"
                            )
