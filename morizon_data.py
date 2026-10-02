# This file is part of MORIZON NEXT.
# Copyright (C) 2026 Hideharu Masai
# Licensed under the GNU General Public License v3. See LICENSE and NOTICE.

"""
MORIZONデータ（ZoningKit形式のフォルダ・保存ZIP）の構成を扱う部品。
画面（QGIS/Qt）には依存せず、フォルダの判定・入力ファイルの検索・取り込み・説明ファイルの作成だけを行う。

構成は原版の『ZoningKit_○○』（手引 図3-13）と同じ:
    <最上位>/
    ├─ DATA/        DEM/ SiteIndex/{NPP,SRAD,VTEX}/ ROAD/ TATEMONO/ SAGYO-SYSTEM_CSV/
    ├─ YOUSO/       要素計算の出力
    ├─ ZONING/      スコアリングとゾーニングの出力
    └─ AGGREGATE/   ゾーン統計量の出力（MORIZON NEXTで追加）
原版の手引では DATA フォルダそのものを指定する運用のため、DATA だけのフォルダ・ZIPも受け付ける。
"""

import configparser
import os
import posixpath
import shutil
import zipfile
from datetime import datetime

from .constants import (
    DIR_AGGREGATE,
    DIR_DATA,
    DIR_SHARED,
    DIR_YOUSO,
    DIR_ZONING,
    INPUT_BUILDING,
    INPUT_COSTCSV,
    INPUT_DEM,
    INPUT_NETWORK,
    INPUT_NPP,
    INPUT_SRAD,
    INPUT_VTEX,
)

# 入力データ（キーは要素計算の入力辞書と同じ）
INPUT_DEFS = {
    "dem": INPUT_DEM,
    "npp": INPUT_NPP,
    "srad": INPUT_SRAD,
    "vtex": INPUT_VTEX,
    "building": INPUT_BUILDING,
    "network": INPUT_NETWORK,
    "costcsv": INPUT_COSTCSV,
}

# DATA フォルダだけが渡されたと判断する目印（DATA直下のサブフォルダ名）
_DATA_MARKERS = {defn["PATH"][0] for defn in INPUT_DEFS.values()}

# 最上位から取り込む（コピー・展開する）フォルダ。KEIKAKUZU 等ほかのフォルダもそのまま持ち込む
_SKIP_TOP_LEVEL = {DIR_SHARED, "__MACOSX"}


# ── 入力ファイルの検索 ───────────────────────────────────────────────

def find_input_files(data_dir: str, input_def: dict) -> list:
    """DATA フォルダ内の所定サブフォルダから、拡張子が一致するファイルを名前順で返す。
    拡張子は最後の1つだけで比べる（DEM.tif.aux.xml や SHOHAN_M.shp.xml を本体と取り違えないため）。"""
    directory = os.path.join(data_dir, *input_def["PATH"])
    if not os.path.isdir(directory):
        return []
    ext = "." + input_def["EXT"].lower()
    return [
        os.path.join(directory, name)
        for name in sorted(os.listdir(directory))
        if os.path.splitext(name)[1].lower() == ext and os.path.isfile(os.path.join(directory, name))
    ]


def find_inputs(data_dir: str) -> dict:
    """{入力キー: [候補ファイル, ...]}（見つからない入力は空リスト）"""
    return {key: find_input_files(data_dir, defn) for key, defn in INPUT_DEFS.items()}


# ── フォルダ構成の判定 ───────────────────────────────────────────────

def _is_data_dir(path: str) -> bool:
    return os.path.isdir(path) and any(
        os.path.isdir(os.path.join(path, marker)) for marker in _DATA_MARKERS
    )


def resolve_root(selected_dir: str):
    """選ばれたフォルダから (最上位, 種類) を返す。種類は
    "kit"  … DATA/ を持つ最上位（ZoningKit・MORIZON NEXTの保存データ）
    "data" … DATA フォルダそのもの（出力フォルダは無い）
    判定できなければ (None, None)。DATA を選んでも親が最上位なら親を最上位とする。"""
    selected_dir = os.path.normpath(selected_dir)
    if os.path.isdir(os.path.join(selected_dir, DIR_DATA)):
        return selected_dir, "kit"
    if _is_data_dir(selected_dir):
        parent = os.path.dirname(selected_dir)
        if os.path.basename(selected_dir) == DIR_DATA and any(
            os.path.isdir(os.path.join(parent, name)) for name in (DIR_YOUSO, DIR_ZONING)
        ):
            return parent, "kit"
        return selected_dir, "data"
    return None, None


def resolve_workspace(selected_dir: str):
    """選ばれたフォルダから (作業フォルダ, DATAフォルダ) を返す。読める構成でなければ (None, None)。
    最上位（DATA/ を持つ）ならそのまま。DATA を選んだ場合はその親を作業フォルダにする（出力の YOUSO/ 等は DATA の隣にできる）。
    DATA という名前でない入力フォルダなら、そのフォルダ自身を作業フォルダにする"""
    root, kind = resolve_root(selected_dir)
    if root is None:
        return None, None
    if kind == "kit":
        return root, os.path.join(root, DIR_DATA)
    workspace_root = os.path.dirname(root) if os.path.basename(root) == DIR_DATA else root
    return workspace_root, root


def has_outputs(workspace_root: str) -> bool:
    """作業フォルダに出力（YOUSO/・ZONING/・AGGREGATE/ のファイル）があるか"""
    return any(
        any(files for _, _, files in os.walk(os.path.join(workspace_root, name)))
        for name in (DIR_YOUSO, DIR_ZONING, DIR_AGGREGATE)
    )


def resolve_data_dir(selected_dir: str):
    """選ばれたフォルダ（最上位でも DATA でもよい）から DATA フォルダを返す。無ければ None。"""
    root, kind = resolve_root(selected_dir)
    if root is None:
        return None
    return os.path.join(root, DIR_DATA) if kind == "kit" else root


def _zip_root(names):
    """ZIP内の名前一覧から (最上位の接頭辞, 種類) を返す。1段フォルダに包まれたZIPにも対応する。"""
    dirs = set()
    for name in names:
        parts = name.split("/")[:-1]
        for i in range(1, len(parts) + 1):
            dirs.add("/".join(parts[:i]) + "/")

    def shallowest(candidates):
        return min(candidates, key=lambda p: (p.count("/"), p)) if candidates else None

    kit = shallowest([d[: -len(DIR_DATA) - 1] for d in dirs if posixpath.basename(d[:-1]) == DIR_DATA])
    if kit is not None:
        return kit, "kit"
    data = shallowest([
        d[: -len(posixpath.basename(d[:-1])) - 1]
        for d in dirs if posixpath.basename(d[:-1]) in _DATA_MARKERS
    ])
    if data is not None:
        return data, "data"
    return None, None


def _safe_member(name: str) -> bool:
    """ZIP内の名前がフォルダ外へ書き出されないか（絶対パス・..・ドライブ指定を拒否）"""
    if name.startswith("/") or "\\" in name or ":" in name.split("/")[0]:
        return False
    return ".." not in name.split("/")


# ── 取り込み ─────────────────────────────────────────────────────────

def clear_managed_dir(managed_dir: str) -> list:
    """管理フォルダ内の共有キャッシュ以外を削除し、削除できなかったものを返す"""
    failed = []
    if not os.path.isdir(managed_dir):
        return failed
    for name in os.listdir(managed_dir):
        if name == DIR_SHARED:
            continue
        path = os.path.join(managed_dir, name)
        try:
            if os.path.isdir(path) and not os.path.islink(path):
                shutil.rmtree(path)
            else:
                os.remove(path)
        except OSError as e:
            failed.append(f"{name}: {e}")
    return failed


def inspect_source(source: str):
    """取り込み元（ZIPまたはフォルダ）の種類を返す。読めなければ ValueError。
    戻り値は "kit" または "data"。"""
    if os.path.isfile(source):
        with zipfile.ZipFile(source) as zf:
            _, kind = _zip_root([_member_name(info) for info in zf.infolist()])
    else:
        _, kind = resolve_root(source)
    if kind is None:
        raise ValueError(
            "MORIZONのデータとして読める構成ではありません。\n"
            "DATA フォルダ（DEM・SiteIndex などを含むフォルダ）か、それを含むフォルダ・ZIPを選んでください。"
        )
    return kind


class ImportCancelled(Exception):
    """取り込みが中断されたときに import_source から送出される"""


_COPY_CHUNK = 1024 * 1024


def import_source(source: str, managed_dir: str, progress_cb=None, cancel_cb=None):
    """ZIPまたはフォルダの中身を管理フォルダへ取り込む（事前に clear_managed_dir しておくこと）。
    最上位の構成ならそのまま、DATA だけなら管理フォルダの DATA/ に入れる。

    1MBごとに cancel_cb() を確認し、真なら ImportCancelled を送出する（途中までのファイルは残る）。
    progress_cb(済んだバイト数, 合計バイト数, ファイル名) で進み具合を通知する。
    ワーカースレッドから呼ぶことを想定し、QGIS/Qt には触れない。"""
    os.makedirs(managed_dir, exist_ok=True)
    if os.path.isfile(source):
        with zipfile.ZipFile(source) as zf:
            tasks = _plan_zip(zf, managed_dir)
            _copy_tasks(tasks, lambda info: zf.open(info), progress_cb, cancel_cb)
    else:
        tasks = _plan_dir(source, managed_dir)
        _copy_tasks(tasks, lambda path: open(path, "rb"), progress_cb, cancel_cb)


def _member_name(info):
    """ZIP内の名前。UTF-8の印が無い名前はcp437として読まれているため、
    日本語Windowsで作られたZIP（配布されているZoningKit等）を想定してcp932で読み直す"""
    if info.flag_bits & 0x800:
        return info.filename
    try:
        return info.filename.encode("cp437").decode("cp932")
    except (UnicodeEncodeError, UnicodeDecodeError):
        return info.filename


def _plan_zip(zf, managed_dir):
    """取り込むエントリを (ZipInfo, 書き出し先, バイト数) の配列で返す"""
    prefix, kind = _zip_root([_member_name(info) for info in zf.infolist()])
    if kind is None:
        raise ValueError("MORIZONのデータとして読める構成ではありません。")
    dest_base = managed_dir if kind == "kit" else os.path.join(managed_dir, DIR_DATA)
    tasks = []
    for info in zf.infolist():
        name = _member_name(info)
        if info.is_dir() or not name.startswith(prefix):
            continue
        rel = name[len(prefix):]
        if not rel or not _safe_member(rel) or rel.split("/")[0] in _SKIP_TOP_LEVEL:
            continue
        tasks.append((info, os.path.join(dest_base, *rel.split("/")), info.file_size))
    return tasks


def _plan_dir(source_dir, managed_dir):
    """コピーするファイルを (元のパス, コピー先, バイト数) の配列で返す"""
    root, kind = resolve_root(source_dir)
    if kind is None:
        raise ValueError("MORIZONのデータとして読める構成ではありません。")
    if kind == "data":
        pairs = [(root, os.path.join(managed_dir, DIR_DATA))]
    else:
        pairs = [
            (os.path.join(root, name), os.path.join(managed_dir, name))
            for name in sorted(os.listdir(root)) if name not in _SKIP_TOP_LEVEL
        ]
    tasks = []
    for src, dest in pairs:
        if os.path.isfile(src):
            tasks.append((src, dest, os.path.getsize(src)))
            continue
        for dirpath, _, filenames in os.walk(src):
            for filename in sorted(filenames):
                path = os.path.join(dirpath, filename)
                tasks.append((path, os.path.join(dest, os.path.relpath(path, src)), os.path.getsize(path)))
    return tasks


def _copy_tasks(tasks, open_source, progress_cb, cancel_cb):
    total = sum(size for _, _, size in tasks)
    done = 0
    for source_ref, dest, _ in tasks:
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        name = os.path.basename(dest)
        with open_source(source_ref) as src, open(dest, "wb") as dst:
            while True:
                if cancel_cb and cancel_cb():
                    raise ImportCancelled()
                chunk = src.read(_COPY_CHUNK)
                if not chunk:
                    break
                dst.write(chunk)
                done += len(chunk)
                if progress_cb:
                    progress_cb(done, total, name)
        if progress_cb:
            progress_cb(done, total, name)


# ── 保存データの説明ファイル ─────────────────────────────────────────

def _plugin_version():
    parser = configparser.ConfigParser(interpolation=None)
    try:
        parser.read(os.path.join(os.path.dirname(__file__), "metadata.txt"), encoding="utf-8")
        return parser.get("general", "version")
    except (configparser.Error, OSError):
        return "不明"


def info_text() -> str:
    return (
        "MORIZON NEXT 保存データ\n"
        "\n"
        "このフォルダ（ZIP）は、QGIS プラグイン MORIZON NEXT で保存した森林ゾーニングのデータです。\n"
        f"保存日時：{datetime.now().strftime('%Y-%m-%d %H:%M')}\n"
        f"プラグインのバージョン：{_plugin_version()}\n"
        "\n"
        "■ 構成（原版「もりぞん」の ZoningKit と同じ構成です）\n"
        f"{DIR_DATA}/        入力データ\n"
        "  DEM/                  DEM（標高）\n"
        "  SiteIndex/NPP/ SRAD/ VTEX/  地位データ\n"
        "  ROAD/                 道路（既設路網ライン）\n"
        "  TATEMONO/             建物（保全対象）\n"
        "  SAGYO-SYSTEM_CSV/     作業システム\n"
        f"{DIR_YOUSO}/       要素計算の出力\n"
        f"{DIR_ZONING}/      収益性・災害リスク・ゾーニング図の出力\n"
        f"{DIR_AGGREGATE}/   ゾーン統計量の出力（MORIZON NEXT で追加したフォルダです。原版にはありません）\n"
        "\n"
        "■ 読み直すには\n"
        "MORIZON NEXT の要素計算タブ「保存ファイルを読み込む」から、この ZIP またはフォルダを選んでください。\n"
        "原版「もりぞん」では、DATA フォルダを指定すると入力データを読み込めます。\n"
    )
