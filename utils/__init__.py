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


def is_tmpdir_valid():
    """
    システムtempディレクトリーに全角文字がある場合はSAGA/GRASSエラーになるので、不正だと判断する
    """
    return (
        re.search("[^\x01-\x7E]", os.environ["TMP"]) is None
        and re.search("[^\x01-\x7E]", os.environ["TEMP"]) is None
    )


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
