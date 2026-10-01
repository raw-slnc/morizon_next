"""
保存データの出力ファイルから、各タブの処理直後と同じレイヤーを作り直す部品。
レイヤーの追加（グループ分け等）は各タブの add_*_to_project をそのまま使うため、ここではレイヤーを作るだけ。

スタイル(qml)は保存データにあればそれを使い、無ければ各タブと同じ方法で作り直す。
原版の古い構成説明（ZoningKit の「ゾーニングファイル構成.txt」）にある
*_style1.qml / *_style2score.qml / *_style.qml という名前も候補に入れている。
"""

import os

from qgis.core import QgsRasterLayer, QgsVectorLayer

from .constants import (
    OUTPUT_AGGREGATE,
    OUTPUT_COST,
    OUTPUT_DISTANCE,
    OUTPUT_PROFIT,
    OUTPUT_RISK,
    OUTPUT_SAVEAREA,
    OUTPUT_SHC,
    OUTPUT_SITEIDX_HINOKI,
    OUTPUT_SITEIDX_KARAMATSU,
    OUTPUT_SITEIDX_SUGI,
    OUTPUT_SLOPE,
    OUTPUT_ZONING,
)
from .processes import raster_styler

SCORING_SUFFIX = "[スコアリング]"


def _raster(directory, file_name):
    for ext in (".tif", ".tiff"):
        path = os.path.join(directory, file_name + ext)
        if os.path.isfile(path):
            return path
    return None


def _existing_style(directory, file_name, suffixes):
    for suffix in suffixes:
        path = os.path.join(directory, file_name + suffix)
        if os.path.isfile(path):
            return path
    return None


def _style_layer(layer, directory, file_name, suffixes, regenerate, problems):
    """保存済みのqmlがあれば適用し、無ければ regenerate() で作ってから適用する。
    作り直せない・失敗した場合は既定の表示のまま problems に記録する。"""
    qml = _existing_style(directory, file_name, suffixes)
    if qml is None and regenerate is not None:
        try:
            qml = regenerate()
        except Exception as e:
            problems.append(f"{layer.name()}：表示スタイルを作り直せませんでした（{e}）")
            return
    if qml is None:
        problems.append(f"{layer.name()}：表示スタイルが無いため既定の表示です")
        return
    layer.loadNamedStyle(qml)


# 要素：(出力定義, 元データ表示のqml作成, スコア表示のqml作成)。並びは要素計算の処理順
def _element_table(directory, costcsv_path):
    def siteidx(wood_type):
        return (
            lambda path: raster_styler.siteidx.write_rawdata_qml(path, wood_type=wood_type),
            lambda path: raster_styler.siteidx.write_scoring_qml(path),
        )

    cost_raw = (
        (lambda path: raster_styler.cost.write_rawdata_qml(costcsv_path, directory))
        if costcsv_path else None
    )
    return [
        (OUTPUT_SITEIDX_SUGI, *siteidx("sugi")),
        (OUTPUT_SITEIDX_HINOKI, *siteidx("hinoki")),
        (OUTPUT_SITEIDX_KARAMATSU, *siteidx("karamatsu")),
        (OUTPUT_COST, cost_raw,
         lambda path: raster_styler.cost.write_scoring_qml(path, directory)),
        (OUTPUT_DISTANCE,
         lambda path: raster_styler.distance.write_rawdata_qml(directory),
         lambda path: raster_styler.distance.write_scoring_qml(path, directory)),
        (OUTPUT_SHC,
         lambda path: raster_styler.shc.write_rawdata_qml(path, directory),
         lambda path: raster_styler.shc.write_scoring_qml(path, directory)),
        (OUTPUT_SLOPE,
         lambda path: raster_styler.slope.write_rawdata_qml(directory),
         lambda path: raster_styler.slope.write_scoring_qml(directory)),
        (OUTPUT_SAVEAREA,
         lambda path: raster_styler.savearea.write_rawdata_qml(directory),
         lambda path: raster_styler.savearea.write_scoring_qml(directory)),
    ]


def build_element_layers(directory, costcsv_path, problems):
    """YOUSO フォルダから {表示名: [元データ表示, スコア表示]}（要素計算タブの出力と同じ形）"""
    layers = {}
    if not directory or not os.path.isdir(directory):
        return layers
    for output_def, raw_qml, score_qml in _element_table(directory, costcsv_path):
        file_name = output_def["FILE_NAME"]
        path = _raster(directory, file_name)
        if path is None:
            continue
        display_name = output_def["DISPLAY_NAME"]
        raw_layer = QgsRasterLayer(path, display_name)
        score_layer = QgsRasterLayer(path, display_name + SCORING_SUFFIX)
        if not raw_layer.isValid():
            problems.append(f"{display_name}：ファイルを開けませんでした（{path}）")
            continue
        _style_layer(raw_layer, directory, file_name, ("_raw.qml", "_style1.qml"),
                     (lambda f=raw_qml, p=path: f(p)) if raw_qml else None, problems)
        _style_layer(score_layer, directory, file_name, ("_score.qml", "_style2score.qml"),
                     lambda f=score_qml, p=path: f(p), problems)
        layers[display_name] = [raw_layer, score_layer]
    return layers


def build_scoring_layers(directory, problems):
    """ZONING フォルダから {表示名: レイヤー}（スコアリングタブの出力と同じ形）"""
    layers = {}
    if not directory or not os.path.isdir(directory):
        return layers
    for output_def, writer in (
        (OUTPUT_PROFIT, raster_styler.profit.write_qml),
        (OUTPUT_RISK, raster_styler.risk.write_qml),
    ):
        file_name = output_def["FILE_NAME"]
        path = _raster(directory, file_name)
        if path is None:
            continue
        layer = QgsRasterLayer(path, output_def["DISPLAY_NAME"])
        if not layer.isValid():
            problems.append(f"{output_def['DISPLAY_NAME']}：ファイルを開けませんでした（{path}）")
            continue
        _style_layer(layer, directory, file_name, (".qml", "_style.qml"),
                     lambda w=writer, p=path: w(p, directory), problems)
        layers[output_def["DISPLAY_NAME"]] = layer
    return layers


def build_zoning_layers(directory, problems):
    """ZONING フォルダから {表示名: レイヤー}（ゾーニングタブの出力と同じ形）"""
    if not directory or not os.path.isdir(directory):
        return {}
    file_name = OUTPUT_ZONING["FILE_NAME"]
    path = _raster(directory, file_name)
    if path is None:
        return {}
    layer = QgsRasterLayer(path, OUTPUT_ZONING["DISPLAY_NAME"])
    if not layer.isValid():
        problems.append(f"{OUTPUT_ZONING['DISPLAY_NAME']}：ファイルを開けませんでした（{path}）")
        return {}
    _style_layer(layer, directory, file_name, (".qml", "_style.qml"),
                 lambda: raster_styler.zoning.write_qml(directory), problems)
    return {OUTPUT_ZONING["DISPLAY_NAME"]: layer}


def find_aggregate_shp(directory):
    """AGGREGATE フォルダの集計結果。既定名を優先し、無ければ名前順で最初のshp"""
    if not directory or not os.path.isdir(directory):
        return None
    default = os.path.join(directory, OUTPUT_AGGREGATE["FILE_NAME"] + ".shp")
    if os.path.isfile(default):
        return default
    shps = sorted(name for name in os.listdir(directory) if os.path.splitext(name)[1].lower() == ".shp")
    return os.path.join(directory, shps[0]) if shps else None


def build_aggregate_layers(shp_path, style_threshold, problems):
    """集計結果のshpから {表示名: レイヤー}（集計タブの出力と同じ形）"""
    if not shp_path:
        return {}
    layer = QgsVectorLayer(shp_path, OUTPUT_AGGREGATE["DISPLAY_NAME"])
    if not layer.isValid():
        problems.append(f"{OUTPUT_AGGREGATE['DISPLAY_NAME']}：ファイルを開けませんでした（{shp_path}）")
        return {}
    directory = os.path.dirname(shp_path)
    file_name = os.path.splitext(os.path.basename(shp_path))[0]
    _style_layer(layer, directory, file_name, (".qml",),
                 lambda: raster_styler.aggregate.write_qml(shp_path, style_threshold), problems)
    return {OUTPUT_AGGREGATE["DISPLAY_NAME"]: layer}
