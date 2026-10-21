# This file is part of MORIZON NEXT, modified from the original MORIZON (v2.1).
# Modified by Hideharu Masai since 2026-09-29. See the Git history for the changes and their dates.
# Licensed under the GNU General Public License v3. See LICENSE and NOTICE.

from qgis.PyQt.QtCore import QRectF
from qgis.PyQt.QtGui import QColor, QFont
from qgis.core import (
    Qgis,
    QgsCoordinateTransform,
    QgsGeometry,
    QgsLayoutItem,
    QgsLayoutItemLegend,
    QgsLayoutItemMap,
    QgsLayoutItemScaleBar,
    QgsLayoutPoint,
    QgsLayoutSize,
    QgsPointXY,
    QgsPrintLayout,
    QgsMapLayerStyle,
    QgsProject,
    QgsReadWriteContext,
    QgsRectangle,
    QgsUnitTypes,
)
from qgis.PyQt.QtXml import QDomDocument
from qgis.utils import iface
import os


def generate(target_name, background_layers, target_layer, extent_layers=None):
    """extent_layers：地図の範囲を決めるレイヤー（ゾーニング図とゾーン統計量）。地図の中心は各レイヤーの範囲の中心の
    真ん中、縮尺はその中心のまま全部のレイヤーが収まる大きさにする。2つのレイアウトで縮尺と中心（＝背景の位置）がそろう。
    無ければ target_layer の範囲"""
    # レイアウトファイルの読み込み
    project = QgsProject.instance()
    composition = QgsPrintLayout(project)
    document = QDomDocument()
    plugin_dir = os.path.dirname(__file__)

    if target_name == "zoning":
        template_file = os.path.join(plugin_dir, "template/zoning_template.qpt")
    else:
        template_file = os.path.join(plugin_dir, "template/aggregate_template.qpt")
    with open(template_file) as f:
        template_content = f.read()

    document.setContent(template_content)
    composition.loadFromTemplate(document, QgsReadWriteContext())
    template_name = composition.name()
    manager = project.layoutManager()
    project.layoutManager().addLayout(composition)
    layout = manager.layoutByName(template_name)

    map = QgsLayoutItemMap(layout)
    map.setRect(QRectF(10, 10, 10, 10))
    layers = [target_layer] + [
        record["layer"] for record in background_layers if record.get("layer") is not None
    ]
    map.setLayers(layers)
    map.setKeepLayerSet(True)
    layer_style_overrides = _layer_style_overrides(background_layers)
    if layer_style_overrides:
        map.setLayerStyleOverrides(layer_style_overrides)
        map.setKeepLayerStyles(True)
    map.setFrameEnabled(True)

    # マップの表示の調整（ゾーニング図・ゾーン統計量で同じ位置。右の凡例がページに収まるよう、左の余白は上と同じ 10mm）
    map.attemptMove(QgsLayoutPoint(10, 10, QgsUnitTypes.LayoutUnit.LayoutMillimeters))
    map.attemptResize(QgsLayoutSize(310, 270, QgsUnitTypes.LayoutUnit.LayoutMillimeters))
    map.zoomToExtent(_centered_extent(extent_layers or [target_layer]))
    layout.addLayoutItem(map)

    # 方位記号アイテムの方向を地図の回転と同期
    azimuth_icon = layout.itemById("方位記号")
    azimuth_icon.setLinkedMap(map)

    # スケールバーの追加
    scalebar = QgsLayoutItemScaleBar(layout)
    scalebar.setFont(QFont("Arial", 14))
    scalebar.setStyle("Single Box")
    scalebar.setFillColor(QColor("Black"))
    scalebar.setUnits(QgsUnitTypes.DistanceUnit.DistanceKilometers)
    scalebar.setUnitLabel("km")
    scalebar.setLinkedMap(map)
    scalebar.setSegmentSizeMode(Qgis.ScaleBarSegmentSizeMode.FitWidth)
    scalebar.setNumberOfSegmentsLeft(0)
    scalebar.setNumberOfSegments(2)
    scalebar.setMinimumBarWidth(15)
    scalebar.setMaximumBarWidth(120)
    scalebar.update()
    scalebar.setReferencePoint(QgsLayoutItem.ReferencePoint.Middle)
    layout.addLayoutItem(scalebar)
    scalebar.attemptMove(QgsLayoutPoint(210, 287, QgsUnitTypes.LayoutUnit.LayoutMillimeters))

    # 凡例の追加
    legend = QgsLayoutItemLegend(layout)
    legend.setAutoUpdateModel(False)
    group = legend.model().rootGroup()
    group.clear()
    group.addLayer(
        QgsProject.instance().layerTreeRoot().findLayer(target_layer.id()).layer()
    )
    layout.addItem(legend)
    # 地図の右端（320mm）から 4mm あける。タイトル（テンプレートのラベル）も同じ左端
    legend.attemptMove(QgsLayoutPoint(324, 14, QgsUnitTypes.LayoutUnit.LayoutMillimeters))
    layout.addLayoutItem(legend)

    # レイアウトを開く
    iface.openLayoutDesigner(layout=layout)


def _layer_style_overrides(background_layers):
    overrides = {}
    for record in background_layers:
        layer = record.get("layer")
        opacity = record.get("opacity", 1.0)
        if layer is None:
            continue
        style_xml = _style_xml_with_opacity(layer, opacity)
        if style_xml:
            overrides[layer.id()] = style_xml
    return overrides


def _style_xml_with_opacity(layer, opacity):
    original_style = QgsMapLayerStyle()
    original_style.readFromLayer(layer)
    try:
        if not _set_layer_opacity(layer, opacity):
            return None
        override_style = QgsMapLayerStyle()
        override_style.readFromLayer(layer)
        return override_style.xmlData()
    finally:
        original_style.writeToLayer(layer)
        layer.triggerRepaint()


def _set_layer_opacity(layer, opacity):
    renderer = layer.renderer() if hasattr(layer, "renderer") else None
    if renderer is not None and hasattr(renderer, "setOpacity"):
        renderer.setOpacity(opacity)
        return True
    if hasattr(layer, "setOpacity"):
        layer.setOpacity(opacity)
        return True
    return False


def _centered_extent(layers):
    """各レイヤーの範囲（プロジェクトの座標系）の中心の真ん中を中心にして、全部の範囲が収まる範囲。
    片方の範囲が大きくても、中心はその大きい方に寄らない"""
    extents = [get_target_layer_extent(layer) for layer in layers if layer is not None and layer.isValid()]
    if not extents:
        return QgsRectangle()
    center_x = sum(extent.center().x() for extent in extents) / len(extents)
    center_y = sum(extent.center().y() for extent in extents) / len(extents)
    half_width = max(max(abs(extent.xMinimum() - center_x), abs(extent.xMaximum() - center_x)) for extent in extents)
    half_height = max(max(abs(extent.yMinimum() - center_y), abs(extent.yMaximum() - center_y)) for extent in extents)
    return QgsRectangle(center_x - half_width, center_y - half_height, center_x + half_width, center_y + half_height)


def get_target_layer_extent(target_layer):
    """
    対象のレイヤーのextentをプロジェクトCRSに再投影してから返す
    Returns:
        QgsRectangle
    """
    target_layer_crs = target_layer.crs()
    project_crs = QgsProject.instance().crs()
    transform = QgsCoordinateTransform(
        target_layer_crs, project_crs, QgsProject.instance()
    )
    extent = target_layer.extent()

    leftbottom = QgsPointXY(extent.xMinimum(), extent.yMinimum())
    righttop = QgsPointXY(extent.xMaximum(), extent.yMaximum())
    leftbottom_geom = QgsGeometry.fromPointXY(leftbottom)
    righttop_geom = QgsGeometry.fromPointXY(righttop)
    leftbottom_geom.transform(transform)
    righttop_geom.transform(transform)
    target_layer_extent = QgsRectangle(
        leftbottom_geom.asPoint(), righttop_geom.asPoint()
    )
    return target_layer_extent
