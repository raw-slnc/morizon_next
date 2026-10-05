# This file is part of MORIZON NEXT.
# Copyright (C) 2026 Hideharu Masai
# Licensed under the GNU General Public License v3. See LICENSE and NOTICE.

"""
「地形の複雑さ」を SAGA で計算する（設定タブの SAGA ON）ための前提を確かめる。

SAGA を使うには、QGIS のプラグイン Processing Saga NextGen Provider と、SAGA 本体（saga_cmd）の両方が要る。
プロバイダーは SAGA 本体の有無に関係なくアルゴリズムの一覧を説明ファイルから作るため、
処理ツールの一覧にアルゴリズムがあっても本体があるとは限らない。そこで本体は、
プロバイダー（processing_saga_nextgen 1.3.0 の SagaUtils.sagaPath / getInstalledVersion）と
同じ順番で saga_cmd を探し、実際に版を問い合わせて確かめる。
"""

import os
import platform
import shutil
import subprocess

from qgis.core import Qgis, QgsApplication, QgsMessageLog

PLUGIN_NAME = "processing_saga_nextgen"
PROVIDER_ID = "sagang"
# プロバイダーが求める SAGA の最低限の版（processing_saga_nextgen の SagaUtils.REQUIRED_VERSION）
REQUIRED_SAGA_VERSION = "9.2.0"


def _parse_version(text):
    return tuple(int(part) for part in text.rstrip(".").split(".") if part.isdigit())


def _default_saga_folder():
    """プロバイダーの findSagaFolder と同じ規則で、OSごとの既定の場所を探す"""
    system = platform.system()
    if system in ("Darwin", "FreeBSD"):
        candidates = [os.path.join(QgsApplication.prefixPath(), "bin"), "/usr/local/bin"]
        executable = "saga_cmd"
    elif system == "Windows":
        candidates = [os.path.join(os.path.dirname(QgsApplication.prefixPath()), "saga")]
        if "OSGEO4W_ROOT" in os.environ:
            candidates.append(os.path.join(os.environ["OSGEO4W_ROOT"], "apps", "saga"))
        executable = "saga_cmd.exe"
    else:
        return None
    for folder in candidates:
        if os.path.exists(os.path.join(folder, executable)):
            return folder
    return None


def find_saga_cmd():
    """saga_cmd の場所。プロセシングの設定（SAGA_FOLDER）→ OSごとの既定の場所 → PATH の順に探す。無ければ None"""
    executable = "saga_cmd.exe" if platform.system() == "Windows" else "saga_cmd"
    folder = None
    try:
        from processing.core.ProcessingConfig import ProcessingConfig
        configured = ProcessingConfig.getSetting("SAGA_FOLDER")
        if configured and os.path.isdir(configured):
            folder = configured
    except Exception as e:
        # SAGA の設定項目が無い環境（プロバイダ未導入など）。既定の場所と PATH で探す
        QgsMessageLog.logMessage(
            f"SAGA の設定を読めないため、既定の場所で探します（{e}）", "Morizon Next", Qgis.MessageLevel.Info
        )
    folder = folder or _default_saga_folder()
    if folder:
        path = os.path.join(folder, executable)
        return path if os.path.isfile(path) else None
    return shutil.which(executable)


def saga_version(saga_cmd):
    """saga_cmd に版を問い合わせる（"SAGA Version: 9.8.0" の行を読む）。読めなければ None"""
    kwargs = {}
    if platform.system() == "Windows":
        kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    # Why Bandit B603 (subprocess call) is suppressed here: the only way to get the SAGA version is to ask
    # SAGA itself. The executable is saga_cmd found via the Processing setting, the default install location
    # or PATH, and the only argument is the fixed "-v". No user input is passed and no shell is used.
    try:
        result = subprocess.run(  # nosec B603
            [saga_cmd, "-v"], stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL, timeout=20, **kwargs,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    for line in result.stdout.decode("utf-8", errors="replace").splitlines():
        if line.startswith("SAGA Version:"):
            return line[len("SAGA Version:"):].strip().split(" ")[0]
    return None


def check_saga():
    """SAGA で計算できるかを確かめる。
    戻り値は {"ok": bool, "problem": 足りないものの説明（ok のときは ""）,
    "plugin_version": str|None, "saga_version": str|None, "saga_cmd": str|None}"""
    import qgis.utils

    result = {"ok": False, "problem": "", "plugin_version": None, "saga_version": None, "saga_cmd": None}
    qgis.utils.updateAvailablePlugins()
    if PLUGIN_NAME not in qgis.utils.available_plugins:
        result["problem"] = (
            "QGIS のプラグイン「Processing Saga NextGen Provider」が入っていません。\n"
            "プラグイン → プラグインの管理とインストール から入れてください。"
        )
        return result
    result["plugin_version"] = qgis.utils.pluginMetadata(PLUGIN_NAME, "version")
    if PLUGIN_NAME not in qgis.utils.active_plugins \
            or QgsApplication.processingRegistry().providerById(PROVIDER_ID) is None:
        result["problem"] = (
            "プラグイン「Processing Saga NextGen Provider」が無効になっています。\n"
            "プラグイン → プラグインの管理とインストール →「インストール済み」で有効にしてください。"
        )
        return result

    saga_cmd = find_saga_cmd()
    result["saga_cmd"] = saga_cmd
    version = saga_version(saga_cmd) if saga_cmd else None
    if version is None:
        result["problem"] = (
            "SAGA 本体（saga_cmd）が見つかりません。\n"
            "SAGA をインストールしてください。インストール済みの場合は、"
            "設定 → オプション → プロセシング → プロバイダ の SAGA の設定で、SAGA のフォルダを指定してください。"
        )
        return result
    result["saga_version"] = version
    if _parse_version(version) < _parse_version(REQUIRED_SAGA_VERSION):
        result["problem"] = (
            f"SAGA 本体の版が古いため使えません（検出した版：{version}、"
            f"プラグイン「Processing Saga NextGen Provider」が求める版：{REQUIRED_SAGA_VERSION} 以上）。"
        )
        return result
    result["ok"] = True
    return result
