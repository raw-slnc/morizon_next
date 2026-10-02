# This file is part of MORIZON NEXT.
# Copyright (C) 2026 Hideharu Masai
# Licensed under the GNU General Public License v3. See LICENSE and NOTICE.

import os
import time

# QGIS-API
from qgis.PyQt.QtCore import QThread, pyqtSignal

from .. import morizon_data


class DataImportThread(QThread):
    """保存データ（ZIP・フォルダ）を管理フォルダへ取り込むスレッド。
    管理フォルダを空にしてから（共有キャッシュは残す）中身をコピー・展開する。
    レイヤーの取り外し・作り直しはQGISの画面側の処理なので、呼び出し側がメインスレッドで行う。

    中断された場合は、途中までコピーしたファイルを消して管理フォルダを空の状態に戻す。
    結果は processFinished({"status": "done" | "cancelled"}) で返す。"""

    processStarted = pyqtSignal(int)
    addProgress = pyqtSignal(int)
    postMessage = pyqtSignal(str)
    postDetail = pyqtSignal(str)
    processFinished = pyqtSignal(dict)
    setAbortable = pyqtSignal(bool)
    processFailed = pyqtSignal(str)

    def __init__(self, source: str, managed_dir: str):
        super().__init__()
        self.source = source
        self.managed_dir = managed_dir
        self.abort_flag = False

    def set_abort_flag(self, flag=True):
        self.abort_flag = flag

    def run(self):
        try:
            self.setAbortable.emit(False)
            self.postMessage.emit("以前のデータを削除中")
            self.postDetail.emit("")
            failed = morizon_data.clear_managed_dir(self.managed_dir)
            if failed:
                self.processFailed.emit(
                    "次のものを削除できませんでした（使用中の可能性があります）。\n" + "\n".join(failed)
                )
                return

            self.setAbortable.emit(True)
            self.postMessage.emit("データをコピー中")
            self.processStarted.emit(100)
            reported = {"percent": 0, "time": 0.0}

            def on_progress(done, total, name):
                percent = int(done * 100 / total) if total else 100
                if percent > reported["percent"]:
                    self.addProgress.emit(percent - reported["percent"])
                    reported["percent"] = percent
                now = time.monotonic()
                if now - reported["time"] >= 0.15 or done == total:
                    reported["time"] = now
                    self.postDetail.emit(f"{name}  {done / 1e6:.0f}/{total / 1e6:.0f}MB")

            try:
                morizon_data.import_source(
                    self.source, self.managed_dir,
                    progress_cb=on_progress, cancel_cb=lambda: self.abort_flag,
                )
            except morizon_data.ImportCancelled:
                self.setAbortable.emit(False)
                self.postMessage.emit("途中まで読み込んだデータを削除中")
                self.postDetail.emit("")
                morizon_data.clear_managed_dir(self.managed_dir)
                self.processFinished.emit({"status": "cancelled"})
                return
            self.processFinished.emit({"status": "done"})
        except Exception as e:
            self.processFailed.emit(str(e))


class FileCopyThread(QThread):
    """ファイルを決まった場所へコピーするスレッド（入力欄の「…」で選んだファイルを作業フォルダへ取り込むときに使う）。
    tasks は (元のパス, コピー先) の配列。中断されたら、書きかけのコピー先を消す"""

    processStarted = pyqtSignal(int)
    addProgress = pyqtSignal(int)
    postMessage = pyqtSignal(str)
    postDetail = pyqtSignal(str)
    processFinished = pyqtSignal(dict)
    setAbortable = pyqtSignal(bool)
    processFailed = pyqtSignal(str)

    def __init__(self, tasks):
        super().__init__()
        self.tasks = tasks
        self.abort_flag = False

    def set_abort_flag(self, flag=True):
        self.abort_flag = flag

    def run(self):
        tasks = []
        try:
            tasks = [(src, dest, os.path.getsize(src)) for src, dest in self.tasks]
            self.setAbortable.emit(True)
            self.postMessage.emit("作業フォルダにファイルを取り込み中")
            self.processStarted.emit(100)
            reported = {"percent": 0}

            def on_progress(done, total, name):
                percent = int(done * 100 / total) if total else 100
                if percent > reported["percent"]:
                    self.addProgress.emit(percent - reported["percent"])
                    reported["percent"] = percent
                self.postDetail.emit(f"{name}  {done / 1e6:.0f}/{total / 1e6:.0f}MB")

            morizon_data._copy_tasks(
                tasks, lambda path: open(path, "rb"), on_progress, lambda: self.abort_flag
            )
            self.processFinished.emit({"status": "done"})
        except morizon_data.ImportCancelled:
            for _, dest, _ in tasks:
                if os.path.exists(dest):
                    os.remove(dest)
            self.processFinished.emit({"status": "cancelled"})
        except Exception as e:
            self.processFailed.emit(str(e))
