from functools import lru_cache
import json
import xml.etree.ElementTree as ET
import tempfile
import os
import re

from qgis.PyQt.QtCore import *
from qgis.PyQt.QtGui import *
from qgis.PyQt.QtWidgets import *
from qgis.core import *
from qgis.gui import *
import processing

from ..processes import raster_styler
from ..constants import PIXELS_THRESHOLD_RESAMPLING


@lru_cache(maxsize=None)
def get_raster_stats(rlayer: QgsRasterLayer) -> dict:
    """ラスターレイヤーの統計値を取得する

    Args:
        rlayer (QgsRasterLayer)

    Returns:
        dict: {'MAX': float, 'MEAN': float, 'MIN': float, 'STD_DEV': float}
    """

    """
    まずはラスターレイヤーのメタデータを読みに行く
    QgsRasterLayerの初期化時に、メタデータには以下のような文字列が書き込まれる
    STATISTICS_MAXIMUM=255
    STATISTICS_MEAN=206.68737191625
    STATISTICS_MINIMUM=0
    STATISTICS_STDDEV=55.286417545366
    これらはHTMLに埋め込まれているのでパースして取り出す
    """

    metadata_stats = {
        "STATISTICS_MAXIMUM": None,
        "STATISTICS_MINIMUM": None,
        "STATISTICS_MEAN": None,
        "STATISTICS_STDDEV": None,
    }

    try:
        root = ET.fromstring(
            "<root>"
            + rlayer.dataProvider().htmlMetadata().replace("\n", "")
            + "</root>"
        )
    except ET.ParseError as e:
        # xyzタイルはhtmlMetadataが適切なXMLとしてパース出来ないので例外をキャッチ
        print(f"failed to parse htmlMetada of {rlayer.name()}, skipping...")
        root = ET.fromstring("<root></root>")

    for item in root.iter():
        if item.text == "MBTiles":  # GDAL-DriverがMBTilesの場合
            # MBTilesは処理対象外＋計算コストが非常に大きいので、計算せず不正な値を返す（ラスタータイルと同じ値）
            return {"MIN": 1000000, "MAX": -10000, "MEAN": 1000000, "STD_DEV": 1}

        if item.text is None:
            continue

        if "=" not in item.text:
            continue

        prefix, value = item.text.split("=")
        if prefix in metadata_stats.keys() and metadata_stats[prefix] is None:
            metadata_stats[prefix] = float(value)

    # メタデータに統計値が含まれていない場合は計算する
    stats = {
        "MAX": metadata_stats["STATISTICS_MAXIMUM"]
        if metadata_stats["STATISTICS_MAXIMUM"] is not None
        else rlayer.dataProvider().bandStatistics(1).maximumValue,
        "MIN": metadata_stats["STATISTICS_MINIMUM"]
        if metadata_stats["STATISTICS_MINIMUM"] is not None
        else rlayer.dataProvider().bandStatistics(1).minimumValue,
        "MEAN": metadata_stats["STATISTICS_MEAN"]
        if metadata_stats["STATISTICS_MEAN"] is not None
        else rlayer.dataProvider().bandStatistics(1).mean,
        "STD_DEV": metadata_stats["STATISTICS_STDDEV"]
        if metadata_stats["STATISTICS_STDDEV"] is not None
        else rlayer.dataProvider().bandStatistics(1).stdDev,
    }

    return stats


def get_initial_thresholds(rlayer: QgsRasterLayer, classes_count=3) -> list:
    """
    ラスタレイヤに対して、等量区分をしたときの閾値を返す
    classes_count: 区分数
    return -> list: 等量区分する際のしきい値、配列長は区分数-1
    """
    if classes_count < 2:
        raise Exception("classes_count must be larger than 2.")

    rlayer_filepath = rlayer.dataProvider().dataSourceUri()
    output_dir = os.path.dirname(rlayer_filepath)

    # QGIS画面上の地図スタイルを変換しないために、filepathから新たにインスタンスを作成する
    rlayer_from_path = QgsRasterLayer(rlayer_filepath)
    renderer = raster_styler.get_quantile_renderer(
        rlayer_from_path, [[0, 0, 0] for _ in range(classes_count)]
    )
    rlayer_from_path.setRenderer(renderer)

    # 等量区分QMLを書き出す
    with tempfile.NamedTemporaryFile(delete=False, suffix=".qml") as temp_qml:
        rlayer_from_path.saveNamedStyle(temp_qml.name)
        qml_filepath = raster_styler.round_label_precision(
            temp_qml.name, os.path.join(output_dir, "tmp_init_score.qml"), precision=4
        )

    tree = ET.parse(qml_filepath)
    root = tree.getroot()
    items = root.find("pipe/rasterrenderer/rastershader/colorrampshader").findall(
        "item"
    )

    # QMLからしきい値を取り出す
    thresholds = []
    for i in range(classes_count - 1):
        try:
            threshold = float(items[i].attrib["value"])
        except IndexError:
            threshold = 0
        thresholds.append(threshold)

    os.remove(qml_filepath)
    return thresholds


def find(l: list, x) -> int:
    """
    https://note.nkmk.me/python-list-index/
    配列から要素を検索し、存在すればそのインデックスを返す
    存在しなければ-1を返す

    Args:
        l ([type]): 検索対象の配列
        x ([type]): 検索する値

    Returns:
        int: 見つかった最初の要素のインデックス
    """
    return l.index(x) if x in l else -1


def is_morizon_managed_layer(layer: QgsMapLayer, allowed_names=None,
                             allowed_extensions=None) -> bool:
    """
    MORIZON NEXTがプロジェクトフォルダ配下で管理する成果物レイヤーか判定する。
    外部レイヤーを広く走査しないため、タブ内の候補絞り込みと自動設定で使う。
    """
    if layer is None:
        return False
    if allowed_names is not None and layer.name() not in allowed_names:
        return False
    source = _layer_source_path(layer)
    if not source:
        return False
    normalized_source = os.path.normpath(source)
    managed_dir = get_morizon_managed_dir()
    try:
        in_managed_dir = os.path.commonpath(
            [normalized_source, managed_dir]
        ) == managed_dir
    except ValueError:
        in_managed_dir = False
    if not in_managed_dir:
        return False
    if allowed_extensions is None:
        return True
    return os.path.splitext(normalized_source)[1].lower() in {
        ext.lower() for ext in allowed_extensions
    }


def set_morizon_layer_scope(combobox: QgsMapLayerComboBox,
                            allowed_names=None,
                            allowed_extensions=None,
                            keep_current=True):
    """QgsMapLayerComboBoxの候補をMORIZON管理フォルダ配下の成果物に限定する。"""
    project_layers = list(QgsProject.instance().mapLayers().values())
    current_layer = combobox.currentLayer() if keep_current else None
    excepted_layers = [
        layer for layer in project_layers
        if layer is not current_layer
        if not is_morizon_managed_layer(
            layer,
            allowed_names=allowed_names,
            allowed_extensions=allowed_extensions,
        )
    ]
    combobox.setExceptedLayerList(excepted_layers)


def find_morizon_layer_by_name(layer_name: str, allowed_extensions=None):
    """MORIZON管理フォルダ配下から、指定名のレイヤーを返す。"""
    for layer in QgsProject.instance().mapLayers().values():
        if is_morizon_managed_layer(
            layer,
            allowed_names={layer_name},
            allowed_extensions=allowed_extensions,
        ):
            return layer
    return None


def get_morizon_managed_dir() -> str:
    project_home = QgsProject.instance().homePath() or os.path.expanduser("~")
    return os.path.normpath(os.path.join(project_home, "morizon_next"))


def _layer_source_path(layer: QgsMapLayer) -> str:
    source = layer.source()
    if "|" in source:
        source = source.split("|", 1)[0]
    return os.path.abspath(source) if source else ""


def get_tiff_info(tiff_filepath: str) -> dict:
    """
    DEMの各種情報をgdalinfoを用いて取得する

    Args:
        dem_filepath (str): [description]

    Returns:
        dict: {"crs", "resolution", "extent", "nodata_value", "size"}
    """
    gdalinfo_html = processing.run(
        "gdal:gdalinfo",
        {"EXTRA": "-json", "INPUT": tiff_filepath, "OUTPUT": "TEMPORARY_OUTPUT"},
    )["OUTPUT"]

    with open(gdalinfo_html) as f:
        gdalinfo_json = "".join(f.readlines())[5:-6]
        gdalinfo = json.loads(gdalinfo_json)

    crs = QgsCoordinateReferenceSystem.fromWkt(gdalinfo["coordinateSystem"]["wkt"])

    extent = [
        gdalinfo["cornerCoordinates"]["upperLeft"][0],
        gdalinfo["cornerCoordinates"]["lowerRight"][0],
        gdalinfo["cornerCoordinates"]["lowerRight"][1],
        gdalinfo["cornerCoordinates"]["upperLeft"][1],
    ]

    return {
        "crs": crs,
        "resolution": gdalinfo["geoTransform"][1],
        "extent": extent,
        "nodata_value": gdalinfo["bands"][0].get("noDataValue"),
        "size": gdalinfo["size"],
    }


def is_valid_elements_layer(rlayer: QgsRasterLayer) -> bool:
    """
    ラスターレイヤーが有効な要素レイヤーかチェックする
    """
    stats = get_raster_stats(rlayer)
    return stats["MIN"] <= stats["MEAN"] and stats["MEAN"] <= stats["MAX"]


def is_valid_scoring_layer(rlayer: QgsRasterLayer) -> bool:
    """
    ラスターレイヤーが有効なスコアリングレイヤーかチェックする
    """
    stats = get_raster_stats(rlayer)
    is_tile = stats["MIN"] >= stats["MAX"]
    has_negative = stats["MIN"] < 0
    not_integer = stats["MIN"] != int(stats["MIN"]) or stats["MAX"] != int(stats["MAX"])

    is_invalid = is_tile or has_negative or not_integer
    return not is_invalid


def get_ascii_safe_dir(subdir_name: str) -> str:
    """
    ユーザー名・プロジェクト名等の全角文字を経由しない、ドライブ直下の半角安全な
    ディレクトリパスを返す（フォルダが無ければ作成する）。
    SAGA/GRASSの一時フォルダや、プラグイン自身が管理するデータの保存先など、
    「ユーザーの環境設定は変えず、内部で吸収する」ために使う。
    """
    drive = os.path.splitdrive(tempfile.gettempdir())[0]
    if drive:
        safe_dir = os.path.join(drive + os.sep, subdir_name)
    else:
        safe_dir = os.path.join("/tmp", subdir_name)
    os.makedirs(safe_dir, exist_ok=True)
    return safe_dir


def get_ascii_safe_alias(real_path: str) -> str:
    """
    real_pathの実体はそのまま（ユーザーが指定した場所、全角パスでも構わない）にしつつ、
    ASCII必須のバリデーション・SAGA/GRASS等に渡すための半角安全な別名パスを用意して返す。
    real_pathが既に半角のみならそのまま返す（別名は作らない）。

    実装はハードリンク（同一ファイルを指す別パス、容量を消費しない）を優先し、
    ドライブをまたぐ等でハードリンクが作れない場合はコピーにフォールバックする。
    実体はあくまでreal_path側にあるまま：ユーザーの保存場所（プロジェクトフォルダ等）は
    一切変更しない。

    別名ファイル名にはreal_pathのハッシュを含める。複数プロジェクトで同じベース名
    （例：dem_fetched.tif）のファイルを扱っても、別名パス同士が衝突して他プロジェクトの
    ものを上書きしないようにするため。
    """
    if re.search("[^\x01-\x7E]", real_path) is None:
        return real_path

    real_path = os.path.abspath(real_path)
    real_drive = os.path.splitdrive(real_path)[0]
    if real_drive:
        alias_dir = os.path.join(real_drive + os.sep, "morizon_next_tmp")
    else:
        alias_dir = "/tmp/morizon_next_tmp"
    os.makedirs(alias_dir, exist_ok=True)

    import hashlib
    path_hash = hashlib.sha1(real_path.encode("utf-8")).hexdigest()[:12]  # nosec B324
    real_dir = os.path.dirname(real_path)
    name, ext = os.path.splitext(os.path.basename(real_path))
    alias_path = os.path.join(alias_dir, f"{path_hash}{ext}")

    def _link_or_copy(src, dst):
        if os.path.exists(dst):
            os.remove(dst)
        try:
            os.link(src, dst)
        except OSError:
            # ドライブをまたぐ等でハードリンクが作れない場合はコピーする
            import shutil
            shutil.copy2(src, dst)

    # シェープファイル等、同じベース名で複数の付随ファイル(.shx/.dbf/.prj/.cpg等)を
    # 持つ形式は、本体だけをエイリアス化するとGDAL/OGRが読めなくなる。
    # 同じディレクトリ内で同じベース名を持つファイルは全て一緒にエイリアス化する
    for sibling_name in os.listdir(real_dir):
        sibling_base, sibling_ext = os.path.splitext(sibling_name)
        if sibling_base != name:
            continue
        sibling_src = os.path.join(real_dir, sibling_name)
        sibling_dst = os.path.join(alias_dir, f"{path_hash}{sibling_ext}")
        _link_or_copy(sibling_src, sibling_dst)

    return alias_path


class AsciiSafeProcessingTmpdir:
    """
    SAGA/GRASSはTMP/TEMP環境変数のパスに全角文字（日本語ユーザー名等）が含まれると
    エラーを起こす。ユーザーのPC環境（ユーザー名・フォルダ名）を変えさせるのではなく、
    SAGA/GRASS呼び出しの間だけTMP/TEMPを半角安全なパスへ一時的に差し替え、
    呼び出し後に元へ戻すためのユーティリティ。

    使い方:
        tmpdir = AsciiSafeProcessingTmpdir()
        tmpdir.enter()
        try:
            ... SAGA/GRASSを呼ぶprocessing.run()等 ...
        finally:
            tmpdir.restore()
    """

    def __init__(self):
        self._original_tmp = None
        self._original_temp = None
        self._entered = False

    def enter(self):
        if self._entered:
            return
        self._original_tmp = os.environ.get("TMP")
        self._original_temp = os.environ.get("TEMP")

        safe_dir = get_ascii_safe_dir("morizon_next_tmp")

        os.environ["TMP"] = safe_dir
        os.environ["TEMP"] = safe_dir
        self._entered = True

    def restore(self):
        if not self._entered:
            return
        if self._original_tmp is not None:
            os.environ["TMP"] = self._original_tmp
        else:
            os.environ.pop("TMP", None)
        if self._original_temp is not None:
            os.environ["TEMP"] = self._original_temp
        else:
            os.environ.pop("TEMP", None)
        self._entered = False


def is_resampling_needed(dem_info: dict) -> bool:
    """
    リサンプリングが必要なDEMかどうか判定する
    1: 5mDEMなら常に10mDEMにリサンプリング
    2: 1mDEMなら、画素数が所定の値よりも大きい場合に10mDEMにリサンプリング

    Args:
        dem_info (dict): ./utils.get_tiff_info()で取得できる辞書

    Returns:
        bool: 必要ならTrue
    """
    dem_resolution = dem_info.get("resolution", 10)
    if round(dem_resolution) == 10:
        return False
    elif round(dem_resolution) == 5:
        return True
    else:
        dem_size = dem_info.get("size", [0, 0])
        pixels_count = dem_size[0] * dem_size[1]
        return pixels_count > PIXELS_THRESHOLD_RESAMPLING
