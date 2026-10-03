# This file is part of MORIZON NEXT, modified from the original MORIZON (v2.1).
# Modified by Hideharu Masai since 2026-09-29. See the Git history for the changes and their dates.
# Licensed under the GNU General Public License v3. See LICENSE and NOTICE.

from qgis.PyQt.QtCore import QMetaType
from qgis.core import QgsField, QgsRasterLayer, QgsRectangle, QgsVectorLayer
import processing

# ゾーニング図の範囲に重なるポリゴンを取り出すときの余裕（ゾーニング図のセル数）。
# 重なりの判定なので縁にかかるポリゴンは余裕が無くても拾えるが、座標系の変換による丸めで
# 取りこぼさないよう数セル分だけ広げる
EXTENT_MARGIN_CELLS = 3


def extract_overlapping(polygon_layer, zoning_layer: QgsRasterLayer, context, feedback):
    """ポリゴンのうち、ゾーニング図の範囲（＋余裕）に重なるものだけを取り出す。
    県全域の森林計画図などを選んでも、解析範囲の外の数万件を照合しないようにするため"""
    extent = QgsRectangle(zoning_layer.extent())
    margin = EXTENT_MARGIN_CELLS * max(
        zoning_layer.rasterUnitsPerPixelX(), zoning_layer.rasterUnitsPerPixelY()
    )
    extent.grow(margin)
    authid = zoning_layer.crs().authid()
    extent_text = (
        f"{extent.xMinimum()},{extent.xMaximum()},{extent.yMinimum()},{extent.yMaximum()}"
        + (f" [{authid}]" if authid else "")
    )
    return processing.run(
        "native:extractbyextent",
        {"INPUT": polygon_layer, "EXTENT": extent_text, "CLIP": False, "OUTPUT": "memory:"},
        context=context, feedback=feedback,
    )["OUTPUT"]


def to_raster_crs(polygon_layer, zoning_layer: QgsRasterLayer, context, feedback):
    """ポリゴンをゾーニング図の座標系に変換する（同じなら何もしない）。
    ゾーン統計と区分ごとのセル数の計算を同じ座標系で行うため。座標系が違うままだと
    両者の扱いがそろわず、割合が異常な値になる（原版と同じ作り）"""
    if polygon_layer.crs() == zoning_layer.crs():
        return polygon_layer
    return processing.run(
        "native:reprojectlayer",
        {"INPUT": polygon_layer, "TARGET_CRS": zoning_layer.crs(), "OUTPUT": "memory:"},
        context=context, feedback=feedback,
    )["OUTPUT"]


def zonal_histogram(zoning_layer: QgsRasterLayer, polygon_layer, output_path: str, context, feedback):
    """区分1〜4それぞれの出現数（count_1〜count_4）を加えて output_path に保存する"""
    processing.run(
        "qgis:zonalhistogram",
        {
            "INPUT_RASTER": zoning_layer,
            "INPUT_VECTOR": polygon_layer,
            "OUTPUT": output_path,
            "RASTER_BAND": 1,
            "COLUMN_PREFIX": "count_",
        },
        context=context, feedback=feedback,
    )
    return output_path


def add_statistics(output_path: str, feedback=None):
    """区分1〜4のセル数（count_1〜count_4）から、統計（_count・_mean・_min・_max・_majority）と
    区分ごとの割合を加え、ゾーニング図のセルを含まないポリゴンは取り除く。

    原版は統計を QGIS のゾーン統計（qgis:zonalstatisticsfb）で別に求め、割合を「区分のセル数 ÷ _count」で
    計算していた。ゾーニング図の縁にかかるポリゴンでは、ゾーン統計が _count をほぼ0と返すことがあり
    （区分のセル数とは数え方が食い違う）、割合が異常な値になる。ゾーニング図の値は区分1〜4だけなので、
    統計もすべて区分のセル数から求めて一貫させる。1件ずつ更新せず、まとめて書き込む"""
    vlayer = QgsVectorLayer(output_path, "aggregate")
    provider = vlayer.dataProvider()
    provider.addAttributes(
        [
            QgsField(name="_count", type=QMetaType.Type.Int, len=10),
            QgsField(name="_mean", type=QMetaType.Type.Double, len=10, prec=4),
            QgsField(name="_min", type=QMetaType.Type.Int, len=2),
            QgsField(name="_max", type=QMetaType.Type.Int, len=2),
            QgsField(name="_majority", type=QMetaType.Type.Int, len=2),
            QgsField(name="ratio_1", type=QMetaType.Type.Double, len=6, prec=3),
            QgsField(name="ratio_2", type=QMetaType.Type.Double, len=6, prec=3),
            QgsField(name="ratio_3", type=QMetaType.Type.Double, len=6, prec=3),
            QgsField(name="ratio_4", type=QMetaType.Type.Double, len=6, prec=3),
            # セル数の合計なので整数（原版は小数3桁・全体6桁で、1000セル以上が書き込めなかった）
            QgsField(name="count_1_4", type=QMetaType.Type.Int, len=10),
            QgsField(name="ratio_1_4", type=QMetaType.Type.Double, len=6, prec=3),
        ]
    )
    vlayer.updateFields()
    fields = vlayer.fields()
    index = {name: fields.indexOf(name) for name in
             ("_count", "_mean", "_min", "_max", "_majority",
              "ratio_1", "ratio_2", "ratio_3", "ratio_4", "count_1_4", "ratio_1_4")}

    def number(feature, name):
        value = feature[name] if fields.indexOf(name) >= 0 else None
        try:
            return float(value) if value is not None else 0.0
        except (TypeError, ValueError):
            return 0.0

    changes = {}
    empty_ids = []
    total = max(vlayer.featureCount(), 1)
    for i, feature in enumerate(vlayer.getFeatures()):
        if feedback is not None:
            if feedback.isCanceled():
                break
            feedback.setProgress(100.0 * i / total)
        counts = {k: number(feature, f"count_{k}") for k in (1, 2, 3, 4)}
        cell_count = sum(counts.values())
        if cell_count <= 0:
            empty_ids.append(feature.id())
            continue
        present = [k for k in (1, 2, 3, 4) if counts[k] > 0]
        changes[feature.id()] = {
            index["_count"]: int(cell_count),
            index["_mean"]: sum(k * counts[k] for k in counts) / cell_count,
            index["_min"]: min(present),
            index["_max"]: max(present),
            # 同数のときは小さい区分を採る
            index["_majority"]: max(present, key=lambda k: (counts[k], -k)),
            index["ratio_1"]: counts[1] / cell_count,
            index["ratio_2"]: counts[2] / cell_count,
            index["ratio_3"]: counts[3] / cell_count,
            index["ratio_4"]: counts[4] / cell_count,
            index["count_1_4"]: int(counts[1] + counts[4]),
            index["ratio_1_4"]: (counts[1] + counts[4]) / cell_count,
        }
    provider.changeAttributeValues(changes)
    if empty_ids:
        provider.deleteFeatures(empty_ids)
    return len(changes), len(empty_ids)
