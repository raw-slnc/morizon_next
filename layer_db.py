# This file is part of MORIZON NEXT.
# Copyright (C) 2026 Hideharu Masai
# Licensed under the GNU General Public License v3. See LICENSE and NOTICE.

"""
描画用の DB（GPKG）。ゾーン統計量・道路・建物のデータの正本で、レイヤーはここから描画する。

置き場所は <qgz のフォルダ>/morizon_next/LAYER/<qgz 名>/ の aggregate.gpkg・road.gpkg・building.gpkg
（Morizon Next の中だけで使うので、利用者のデータである作業フォルダには置かない）。
各 GPKG の中の表（レイヤー名は aggregate・road・building）を入れ替える・空にすることはあっても、
.gpkg のファイル自体は置き換えない・消さない（DB として扱う）。

作業フォルダ（プロジェクト内・外部）には、原版との互換のため、DB から書き出した shp を置く（DB → shp の順）。
保存データには shp と一緒に DB の写しの GPKG も入れ、読み込むときは GPKG を優先する。
ただし shp だけが他のソフトで編集されていることがあるため、DB から shp を書き出したときの shp の指紋
（.shp と .dbf の SHA-256）を GPKG の中に記録しておき、今の shp と食い違えば shp から取り込む。
shp から取り込むときは文字コードを判定する（.cpg があればそれに従い、無ければ UTF-8 か CP932 か）。

ファイルの場所はすべて呼び出し側が渡す（処理スレッドから使うため、ここではプロジェクトを参照しない）。
"""

import hashlib
import os
import sqlite3
from contextlib import closing

from osgeo import ogr
from qgis.core import QgsCoordinateTransformContext, QgsVectorFileWriter, QgsVectorLayer

KIND_AGGREGATE = "aggregate"
KIND_ROAD = "road"
KIND_BUILDING = "building"
KINDS = (KIND_AGGREGATE, KIND_ROAD, KIND_BUILDING)

# DB から shp を書き出したときの shp の指紋を記録する表（GPKG の中の、Morizon Next だけが使う表）
_META_TABLE = "morizon_next_source"
# 元のデータに行番号ではない「fid」の属性があるときに使う、GPKG の行番号の列の名前
_FID_COLUMN = "morizon_fid"
_SHP_PARTS = (".shp", ".shx", ".dbf", ".prj", ".cpg", ".qpj", ".qix", ".sbn", ".sbx")


def db_file(db_dir: str, kind: str) -> str:
    """DB のファイル。db_dir が空（プロジェクトが未保存）なら空文字"""
    return os.path.join(db_dir, f"{kind}.gpkg") if db_dir else ""


def layer_uri(gpkg_path: str, kind: str) -> str:
    return f"{gpkg_path}|layername={kind}"


def has_layer(gpkg_path: str, kind: str) -> bool:
    if not gpkg_path or not os.path.isfile(gpkg_path):
        return False
    ds = ogr.Open(gpkg_path)
    if ds is None:
        return False
    found = ds.GetLayerByName(kind) is not None
    ds = None
    return found


def store(gpkg_path: str, kind: str, source_layer: QgsVectorLayer):
    """source_layer の中身で、DB の表を入れ替える（DB のファイルは置き換えない）"""
    if not gpkg_path:
        raise RuntimeError("描画用のデータの置き場所が決まっていません（QGISプロジェクトを保存してください）")
    os.makedirs(os.path.dirname(gpkg_path), exist_ok=True)
    options = QgsVectorFileWriter.SaveVectorOptions()
    options.driverName = "GPKG"
    options.layerName = kind
    options.fileEncoding = "UTF-8"
    options.actionOnExistingFile = (
        QgsVectorFileWriter.ActionOnExistingFile.CreateOrOverwriteLayer if os.path.isfile(gpkg_path)
        else QgsVectorFileWriter.ActionOnExistingFile.CreateOrOverwriteFile
    )
    # GPKG は行番号の列に既定で「fid」を使う。行番号ではない「fid」という属性（基盤地図情報の地物の識別子など）が
    # あると名前がぶつかって書き込めないため、そのときは行番号の列を別の名前にし、元の「fid」は属性のまま残す
    primary_keys = set(source_layer.dataProvider().pkAttributeIndexes())
    fields = source_layer.fields()
    if any(fields.at(i).name().lower() == "fid" and i not in primary_keys for i in range(fields.count())):
        options.layerOptions = [
            option for option in QgsVectorFileWriter.defaultLayerOptions("GPKG")
            if not option.upper().startswith("FID=")
        ] + [f"FID={_FID_COLUMN}"]
    error, message, *_ = QgsVectorFileWriter.writeAsVectorFormatV3(
        source_layer, gpkg_path, QgsCoordinateTransformContext(), options
    )
    if error != QgsVectorFileWriter.WriterError.NoError:
        raise RuntimeError(f"{kind} のデータを書き込めませんでした: {message}")
    # 中身が変わったので、前に書き出した shp の記録は無効
    _set_fingerprint(gpkg_path, kind, None)


def clear(gpkg_path: str, kind: str):
    """DB の表を空にする（表を取り除く。DB のファイルは消さない）"""
    if not os.path.isfile(gpkg_path):
        return
    ds = ogr.Open(gpkg_path, 1)
    if ds is not None:
        for index in reversed(range(ds.GetLayerCount())):
            if ds.GetLayer(index).GetName() == kind:
                ds.DeleteLayer(index)
        ds = None
    _set_fingerprint(gpkg_path, kind, None)


def clear_all(db_dir: str):
    for kind in KINDS:
        clear(db_file(db_dir, kind), kind)


def remove_shp(shp_path: str):
    """shp とその付随ファイルを消す"""
    stem = os.path.splitext(shp_path)[0]
    for ext in _SHP_PARTS:
        path = stem + ext
        if os.path.exists(path):
            os.remove(path)


def export_shp(gpkg_path: str, kind: str, shp_path: str) -> str:
    """DB の表から、作業フォルダへ互換用の shp を書き出し、その shp の指紋を DB に記録する"""
    source = QgsVectorLayer(layer_uri(gpkg_path, kind), kind, "ogr")
    if not source.isValid():
        raise RuntimeError(f"{kind} のデータを読み込めませんでした: {gpkg_path}")
    os.makedirs(os.path.dirname(shp_path), exist_ok=True)
    remove_shp(shp_path)
    options = QgsVectorFileWriter.SaveVectorOptions()
    options.driverName = "ESRI Shapefile"
    options.fileEncoding = "UTF-8"
    # GPKG の行番号の列（fid）は属性ではないので書き出さない。属性名は shp の決まり（10バイトまで）に
    # 合わせるが、文字の途中で切って壊れた名前にしないよう、文字の区切りで切り詰めて重なりも避ける
    fields = source.fields()
    primary_keys = set(source.dataProvider().pkAttributeIndexes())
    indexes = [i for i in range(fields.count()) if i not in primary_keys]
    options.attributes = indexes
    # 書き出す名前は、選んだ属性だけでなく全部の属性の並びで渡す（選んだ属性の並びで渡すと名前と中身がずれる）
    export_names = [fields.at(i).name() for i in range(fields.count())]
    for index, name in zip(indexes, shp_field_names([fields.at(i).name() for i in indexes])):
        export_names[index] = name
    options.attributesExportNames = export_names
    error, message, *_ = QgsVectorFileWriter.writeAsVectorFormatV3(
        source, shp_path, QgsCoordinateTransformContext(), options
    )
    del source
    if error != QgsVectorFileWriter.WriterError.NoError:
        raise RuntimeError(f"{os.path.basename(shp_path)} を書き出せませんでした: {message}")
    _set_fingerprint(gpkg_path, kind, shp_fingerprint(shp_path))
    return shp_path


def shp_field_names(names: list) -> list:
    """shp の属性名（UTF-8 で10バイトまで）。文字の区切りで切り詰め、重なれば末尾に番号を付ける"""
    def fit(text, limit):
        while len(text.encode("utf-8")) > limit:
            text = text[:-1]
        return text

    result = []
    used = set()
    for name in names:
        candidate = fit(name, 10)
        number = 1
        while candidate.lower() in used:
            suffix = f"_{number}"
            candidate = fit(name, 10 - len(suffix)) + suffix
            number += 1
        used.add(candidate.lower())
        result.append(candidate)
    return result


def export_copy(gpkg_path: str, kind: str, dest_path: str, shp_path: str) -> bool:
    """保存データ用に、DB の表を別の GPKG に写す（shp の指紋の記録も写す）。
    DB が shp と食い違っている（その shp を DB から書き出したのではない）ときは写さない"""
    if not has_layer(gpkg_path, kind):
        return False
    recorded = get_fingerprint(gpkg_path, kind)
    if recorded is None or recorded != shp_fingerprint(shp_path):
        return False
    source = QgsVectorLayer(layer_uri(gpkg_path, kind), kind, "ogr")
    if not source.isValid():
        return False
    if os.path.exists(dest_path):
        os.remove(dest_path)
    store(dest_path, kind, source)
    del source
    _set_fingerprint(dest_path, kind, recorded)
    return True


def import_saved(gpkg_path: str, kind: str, shp_path: str) -> str:
    """保存データ（作業フォルダ）に DB を合わせる。次の順に、今の shp と食い違っていないものを使う。
    1. DB 自身（記録した指紋が今の shp と一致する＝shp が編集されていない）→ そのまま使う（正本の内容を残す）
    2. shp の隣の同じ名前の GPKG（保存データ）→ GPKG から取り込む
    3. どちらも食い違う（shp が他のソフトで編集された・GPKG が無い）→ shp から取り込む
    shp が無ければ DB の表を空にする。使った元（"db"・"gpkg"・"shp"・"none"）を返す"""
    if not gpkg_path:
        return "none"
    if not shp_path or not os.path.isfile(shp_path):
        clear(gpkg_path, kind)
        return "none"
    current = shp_fingerprint(shp_path)
    if has_layer(gpkg_path, kind) and get_fingerprint(gpkg_path, kind) == current:
        return "db"
    saved_gpkg = os.path.splitext(shp_path)[0] + ".gpkg"
    if has_layer(saved_gpkg, kind) and get_fingerprint(saved_gpkg, kind) == current:
        source = QgsVectorLayer(layer_uri(saved_gpkg, kind), kind, "ogr")
        if source.isValid():
            store(gpkg_path, kind, source)
            del source
            _set_fingerprint(gpkg_path, kind, current)
            return "gpkg"
    source = QgsVectorLayer(shp_path, kind, "ogr")
    if not source.isValid():
        raise RuntimeError(f"{os.path.basename(shp_path)} を読み込めませんでした")
    encoding = detect_shp_encoding(shp_path)
    if encoding:
        source.setProviderEncoding(encoding)
    store(gpkg_path, kind, source)
    del source
    _set_fingerprint(gpkg_path, kind, current)
    return "shp"


def detect_shp_encoding(shp_path: str):
    """shp の属性の文字コード。.cpg があれば None（読み込み側がそれに従う）。
    無ければ、属性名と値が UTF-8 として読めれば "UTF-8"、読めなければ "CP932"（Shift_JIS）"""
    stem = os.path.splitext(shp_path)[0]
    if any(os.path.isfile(stem + ext) for ext in (".cpg", ".CPG")):
        return None
    dbf_path = next((stem + ext for ext in (".dbf", ".DBF") if os.path.isfile(stem + ext)), None)
    if dbf_path is None:
        return None
    with open(dbf_path, "rb") as f:
        data = f.read()
    # 判定に使うのは属性名と値だけ（ヘッダーの数値の部分は文字ではないので混ぜない）
    header_length = int.from_bytes(data[8:10], "little")
    names = [
        data[offset:offset + 11].split(b"\0", 1)[0]
        for offset in range(32, max(header_length - 1, 32), 32)
    ]
    text = b"".join(names) + data[header_length:].rstrip(b"\x1a")
    try:
        text.decode("utf-8")
        return "UTF-8"
    except UnicodeDecodeError:
        return "CP932"


def shp_fingerprint(shp_path: str):
    """shp の中身（.shp と .dbf）の SHA-256。shp が無ければ None"""
    if not shp_path or not os.path.isfile(shp_path):
        return None
    digest = hashlib.sha256()
    stem = os.path.splitext(shp_path)[0]
    for ext in (".shp", ".dbf"):
        path = stem + ext
        if os.path.isfile(path):
            with open(path, "rb") as f:
                for chunk in iter(lambda: f.read(1024 * 1024), b""):
                    digest.update(chunk)
    return digest.hexdigest()


def get_fingerprint(gpkg_path: str, kind: str):
    if not os.path.isfile(gpkg_path):
        return None
    with closing(sqlite3.connect(gpkg_path)) as conn:
        exists = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (_META_TABLE,)
        ).fetchone()
        if not exists:
            return None
        row = conn.execute(f"SELECT shp_sha256 FROM {_META_TABLE} WHERE layer=?", (kind,)).fetchone()
    return row[0] if row else None


def _set_fingerprint(gpkg_path: str, kind: str, value):
    if not os.path.isfile(gpkg_path):
        return
    with closing(sqlite3.connect(gpkg_path)) as conn, conn:
        conn.execute(f"CREATE TABLE IF NOT EXISTS {_META_TABLE} (layer TEXT PRIMARY KEY, shp_sha256 TEXT)")
        if value is None:
            conn.execute(f"DELETE FROM {_META_TABLE} WHERE layer=?", (kind,))
        else:
            conn.execute(f"INSERT OR REPLACE INTO {_META_TABLE} (layer, shp_sha256) VALUES (?, ?)", (kind, value))
