# This file is part of MORIZON NEXT, modified from the original MORIZON (v2.1).
# Modified by Hideharu Masai since 2026-09-29. See the Git history for the changes and their dates.
# Licensed under the GNU General Public License v3. See LICENSE and NOTICE.

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
from qgis.PyQt import sip
import processing

from ..constants import PIXELS_THRESHOLD_RESAMPLING, MANAGED_DIR_NAME, OUTPUT_GROUP_NAME, DIR_SHARED


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

    # processes は utils を使うため、utils の先頭では読み込まない（読み込み順によって循環して失敗する）
    from ..processes import raster_styler

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
    MORIZON NEXTが今の作業フォルダ（プロジェクト内の morizon_next、または外部のフォルダ）で管理する成果物レイヤーか判定する。
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
    managed_dir = get_workspace_dir()
    if not managed_dir:
        return False
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
    """QgsMapLayerComboBoxの候補をMORIZON管理フォルダ配下の成果物に限定する。
    候補が無ければ空にする(現在の選択がMORIZON管理フォルダ配下でないなら、
    選択を維持せず除外する)。"""
    project_layers = list(QgsProject.instance().mapLayers().values())
    current_layer = combobox.currentLayer() if keep_current else None
    if current_layer is not None and not is_morizon_managed_layer(
        current_layer,
        allowed_names=allowed_names,
        allowed_extensions=allowed_extensions,
    ):
        current_layer = None
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


# 作業フォルダ：入力（DATA/）と出力（YOUSO/・ZONING/・AGGREGATE/）を置く場所。3つの状態がある。
#   なし（None）：初期状態（設定のクリア・未保存のプロジェクト・記録の無いプロジェクト）。出力先は空欄
#   プロジェクト内（WORKSPACE_INTERNAL）：<プロジェクト>/morizon_next
#   外部（フォルダのパス）：「フォルダ選択から開始する」「保存ファイルを読み込む（フォルダ）」で選んだフォルダ
WORKSPACE_INTERNAL = "internal"
_workspace = None


def set_workspace(value):
    """作業フォルダを設定する。None＝なし、WORKSPACE_INTERNAL＝プロジェクト内、それ以外＝外部のフォルダのパス"""
    global _workspace
    if value and value != WORKSPACE_INTERNAL:
        value = os.path.normpath(value)
    _workspace = value or None


def get_workspace():
    """今の作業フォルダ（None／WORKSPACE_INTERNAL／外部のフォルダのパス）"""
    return _workspace


def is_internal_workspace() -> bool:
    return _workspace == WORKSPACE_INTERNAL


def get_external_workspace():
    """外部のフォルダを作業フォルダにしていればそのパス、それ以外は None"""
    return _workspace if _workspace and _workspace != WORKSPACE_INTERNAL else None


# 作業フォルダはQGISプロジェクトに書き込み、プロジェクトの保存で残す（開き直したときに同じ作業フォルダで再開するため）。
# プロジェクト内なら "internal"、外部ならそのパス、なしなら書き込まない
_PROJECT_SCOPE = "MorizonNext"
_PROJECT_KEY_WORKSPACE = "workspace"
_PROJECT_KEY_WORKSPACE_OLD = "external_workspace"  # 以前の書き方（外部のパスだけを記録していた）


def read_project_workspace():
    """プロジェクトに書き込まれた作業フォルダ（None／WORKSPACE_INTERNAL／外部のフォルダのパス）"""
    project = QgsProject.instance()
    value, _ = project.readEntry(_PROJECT_SCOPE, _PROJECT_KEY_WORKSPACE, "")
    if not value:
        value, _ = project.readEntry(_PROJECT_SCOPE, _PROJECT_KEY_WORKSPACE_OLD, "")
    if not value:
        return None
    return value if value == WORKSPACE_INTERNAL else os.path.normpath(value)


def write_project_workspace(value):
    """作業フォルダをプロジェクトに書き込む（None なら記録を消す）。
    書き込むとプロジェクトが変更ありの扱いになるので、同じ値なら書き込まない"""
    if value and value != WORKSPACE_INTERNAL:
        value = os.path.normpath(value)
    value = value or None
    project = QgsProject.instance()
    old_value, _ = project.readEntry(_PROJECT_SCOPE, _PROJECT_KEY_WORKSPACE_OLD, "")
    if old_value:
        project.removeEntry(_PROJECT_SCOPE, _PROJECT_KEY_WORKSPACE_OLD)
    elif read_project_workspace() == value:
        return
    if value:
        project.writeEntry(_PROJECT_SCOPE, _PROJECT_KEY_WORKSPACE, value)
    else:
        project.removeEntry(_PROJECT_SCOPE, _PROJECT_KEY_WORKSPACE)


def get_workspace_dir(*subdirs) -> str:
    """今の作業フォルダ（外部のフォルダ、またはプロジェクト内の morizon_next）。subdirsを渡すとその下のパス。
    作業フォルダがなしなら空文字"""
    external = get_external_workspace()
    if external:
        return os.path.normpath(os.path.join(external, *subdirs))
    if is_internal_workspace():
        return get_morizon_managed_dir(*subdirs)
    return ""


def _project_folder_name() -> str:
    """プロジェクト内の作業フォルダ名：qgz のファイル名（拡張子を除く）。
    同じフォルダに複数の qgz を置いても、プロジェクトごとに作業フォルダが分かれるようにするため。
    キャッシュのフォルダ名（shared）と重なる場合は、別の名前にする"""
    file_name = QgsProject.instance().fileName()
    name = os.path.splitext(os.path.basename(file_name))[0] if file_name else ""
    if not name:
        return "untitled"
    return name + "_project" if name == DIR_SHARED else name


def get_morizon_root_dir(*subdirs) -> str:
    """<qgz のフォルダ>/morizon_next。プロジェクトごとの作業フォルダと、共有キャッシュ（shared）を置く"""
    project_home = QgsProject.instance().homePath() or os.path.expanduser("~")
    return os.path.normpath(os.path.join(project_home, MANAGED_DIR_NAME, *subdirs))


def get_morizon_managed_dir(*subdirs) -> str:
    """プロジェクト内の作業フォルダ（<qgz のフォルダ>/morizon_next/<qgz のファイル名>）。subdirsを渡すとその下のパス。
    構成は constants.py の DIR_DATA 等を参照"""
    return get_morizon_root_dir(_project_folder_name(), *subdirs)


def get_morizon_shared_dir(*subdirs) -> str:
    """解析をまたいで使い回すキャッシュ（<qgz のフォルダ>/morizon_next/shared）。同じフォルダのプロジェクトで共有し、
    保存・破棄・クリアの対象にしない"""
    return get_morizon_root_dir(DIR_SHARED, *subdirs)


def layer_source_path(layer: QgsMapLayer) -> str:
    """レイヤーが読み込んでいるファイルのパス（ファイルでなければ空）"""
    return _layer_source_path(layer)


def _layer_source_path(layer: QgsMapLayer) -> str:
    source = layer.source()
    if "|" in source:
        source = source.split("|", 1)[0]
    return os.path.abspath(source) if source else ""


def move_output_layers_to_main_thread(layers_dict: dict) -> dict:
    """
    処理スレッドで作成した出力レイヤーを、スレッドを抜ける前にメインスレッドへ移す。
    必ず処理スレッド側（processFinished.emitの直前）で呼ぶ（moveToThreadは所属スレッドからしか行えない）。

    移さないと、レイヤーは終了済みスレッドに属したままになり、
    ・Windows環境で追加直後に描画されない（プロジェクトを開き直すと描画される）
    ・削除してもファイルが開かれたまま残り、Windowsで次回の上書きが「アクセスが拒否されました」で失敗する
    という問題が起きる。値はレイヤー単体またはレイヤーの配列。
    """
    main_thread = QCoreApplication.instance().thread()
    for value in layers_dict.values():
        layers = value if isinstance(value, (list, tuple)) else [value]
        for layer in layers:
            if layer is not None and layer.thread() is not main_thread:
                layer.moveToThread(main_thread)
    return layers_dict


def is_under_dir(path: str, directory: str) -> bool:
    try:
        return os.path.commonpath([os.path.normpath(path), directory]) == directory
    except ValueError:
        return False


# ── プラグインの出力レイヤーの印 ─────────────────────────────────
# プラグインが作るレイヤーには、どの工程（と要素）のものかを印としてレイヤーに付ける（プロジェクトに保存される）。
# 片付けはこの印で行い、レイヤーが指しているファイルの場所には頼らない。場所で見分けると、フォルダを移動して
# 参照を直したレイヤーや、作業フォルダの外を指すレイヤーを見失い、新しい出力と混ざって残るため
OUTPUT_TAG_KEY = "morizon_next/output"
STAGE_ELEMENTS = "elements"
STAGE_SCORING = "scoring"
STAGE_ZONING = "zoning"
STAGE_AGGREGATE = "aggregate"


def tag_output_layer(layer, stage: str, key: str = ""):
    """出力レイヤーに印（工程と、要素などの区別）を付ける"""
    layer.setCustomProperty(OUTPUT_TAG_KEY, f"{stage}|{key}")


@lru_cache(maxsize=1)
def _legacy_output_names() -> dict:
    """印を付ける前に作られた出力レイヤーを、名前から (工程, 区別) に対応づける表"""
    from .. import constants
    names = {}
    for value in vars(constants).values():
        if not isinstance(value, dict) or "DISPLAY_NAME" not in value:
            continue
        display_name = value["DISPLAY_NAME"]
        if "/" in display_name:  # 要素（「収益性/地利」など）。生データとスコアリングの2レイヤー
            names[display_name] = (STAGE_ELEMENTS, display_name)
            names[display_name + "[スコアリング]"] = (STAGE_ELEMENTS, display_name)
    for output, stage in ((constants.OUTPUT_PROFIT, STAGE_SCORING), (constants.OUTPUT_RISK, STAGE_SCORING),
                          (constants.OUTPUT_ZONING, STAGE_ZONING), (constants.OUTPUT_AGGREGATE, STAGE_AGGREGATE)):
        names[output["DISPLAY_NAME"]] = (stage, output["DISPLAY_NAME"])
    return names


def _output_tag(layer, in_output_group: bool):
    """レイヤーの (工程, 区別)。プラグインのレイヤーでなければ None。
    印の無いもの（印を付ける前に作られたもの）は、「Morizon Next」グループの中にあり名前が出力の名前なら、そうとみなす"""
    value = layer.customProperty(OUTPUT_TAG_KEY)
    if value:
        stage, _, key = str(value).partition("|")
        return stage, key
    if in_output_group:
        return _legacy_output_names().get(layer.name())
    return None


def output_layers(stage: str = None, keys=None) -> list:
    """プラグインの出力レイヤー（指しているファイルの場所に関係なく）。stage・keys で工程・区別を絞る"""
    root = QgsProject.instance().layerTreeRoot()
    group_layer_ids = set()
    for child in root.children():
        if isinstance(child, QgsLayerTreeGroup) and child.name() == OUTPUT_GROUP_NAME:
            group_layer_ids = {node.layerId() for node in child.findLayers()}
    result = []
    for layer in QgsProject.instance().mapLayers().values():
        tag = _output_tag(layer, layer.id() in group_layer_ids)
        if tag is None:
            continue
        if stage is not None and tag[0] != stage:
            continue
        if keys is not None and tag[1] not in keys:
            continue
        result.append(layer)
    return result


def remove_output_layers(stage: str = None, keys=None) -> int:
    """プラグインの出力レイヤーをプロジェクトから外す（ファイルは消さない）"""
    return _remove_layers_and_empty_groups(output_layers(stage, keys))


def _group_path(node) -> list:
    """レイヤーツリーのグループを、ルートから順に (名前, 親の中の位置, 排他か, チェック, 展開) の並びで返す（ルートは含めない）"""
    path = []
    group = node.parent()
    while group is not None and group.parent() is not None:
        path.insert(0, (group.name(), group.parent().children().index(group), group.isMutuallyExclusive(),
                        group.itemVisibilityChecked(), group.isExpanded()))
        group = group.parent()
    return path


def detach_output_layers(stage: str = None, keys=None) -> list:
    """プラグインの出力レイヤーを、削除せずにプロジェクトから取り外して返す（キャンセルされたら restore_detached_layers で戻す）。
    戻すため、グループの中の位置とチェックの状態、グループの設定を一緒に覚えておく。
    取り外したレイヤーはファイルを掴んだままなので、ファイルを上書きする前に discard_detached_layers で削除すること"""
    project = QgsProject.instance()
    root = project.layerTreeRoot()
    records = []
    for layer in output_layers(stage, keys):
        node = root.findLayer(layer.id())
        if node is None or node.parent() is None:
            continue
        parent = node.parent()
        records.append({
            "path": _group_path(node),
            "index": parent.children().index(node),
            "checked": node.itemVisibilityChecked(),
            "expanded": node.isExpanded(),
            "parent": parent,
            "layer_id": layer.id(),
        })
    taken = []
    for record in records:
        layer = project.takeMapLayer(project.mapLayer(record.pop("layer_id")))
        if layer is not None:
            record["layer"] = layer
            taken.append(record)
    _remove_empty_groups([record["parent"] for record in records])
    for record in taken:
        record.pop("parent", None)
    if taken:
        _refresh_map_canvas()
    return taken


def restore_detached_layers(records):
    """detach_output_layers で取り外したレイヤーを、元のグループの元の位置に戻す（無くなったグループは作り直す）"""
    if not records:
        return
    project = QgsProject.instance()
    for record in sorted(records, key=lambda r: (len(r["path"]), r["index"])):
        group = project.layerTreeRoot()
        for name, index, exclusive, checked, expanded in record["path"]:
            child = next((c for c in group.children()
                          if isinstance(c, QgsLayerTreeGroup) and c.name() == name), None)
            if child is None:
                child = group.insertGroup(min(index, len(group.children())), name)
                child.setIsMutuallyExclusive(exclusive)
                child.setItemVisibilityChecked(checked)
                child.setExpanded(expanded)
            group = child
        project.addMapLayer(record["layer"], False)
        node = group.insertLayer(min(record["index"], len(group.children())), record["layer"])
        node.setItemVisibilityChecked(record["checked"])
        node.setExpanded(record["expanded"])
    _refresh_map_canvas()


def discard_detached_layers(records):
    """detach_output_layers で取り外したレイヤーを削除し、ファイルの掴みを解放する"""
    project = QgsProject.instance()
    for record in records or []:
        layer = record.get("layer")
        if layer is None:
            continue
        # 取り外したレイヤーはプロジェクトの管理外なので、いったん登録してから外し、QGIS に削除させる
        project.addMapLayer(layer, False)
        project.removeMapLayer(layer.id())
    if records:
        records.clear()


def get_morizon_output_group():
    """出力レイヤーをまとめるグループ（プロジェクトの最上位の「Morizon Next」）。無ければ一番上に作る。
    中身が空になったときは _remove_layers_and_empty_groups が取り除く"""
    root = QgsProject.instance().layerTreeRoot()
    for child in root.children():
        if isinstance(child, QgsLayerTreeGroup) and child.name() == OUTPUT_GROUP_NAME:
            return child
    group = root.insertGroup(0, OUTPUT_GROUP_NAME)
    group.setExpanded(True)
    return group


def _remove_layers_and_empty_groups(layers) -> int:
    """
    レイヤーをプロジェクトから外し、それによって空になったグループも取り除く。
    読み込み中のファイルを上書き・削除できるよう、先にファイルの参照を解放する用途。
    """
    project = QgsProject.instance()
    root = project.layerTreeRoot()
    parent_groups = []
    for layer in layers:
        node = root.findLayer(layer.id())
        if node is not None and node.parent() is not None:
            parent_groups.append(node.parent())
    project.removeMapLayers([layer.id() for layer in layers])
    _remove_empty_groups(parent_groups)
    if layers:
        _refresh_map_canvas()
    return len(layers)


def _remove_empty_groups(parent_groups):
    """空になったグループを親方向へ順に取り除く（ルートは残す）。
    同じグループが複数回積まれるため、削除済みのものは飛ばす"""
    root = QgsProject.instance().layerTreeRoot()
    parent_groups = list(parent_groups)
    while parent_groups:
        group = parent_groups.pop()
        if sip.isdeleted(group) or group is root or group.children():
            continue
        parent = group.parent()
        if parent is None:
            continue
        parent.removeChildNode(group)
        parent_groups.append(parent)


def _refresh_map_canvas():
    """外したレイヤーの絵が地図に残らないよう、描画のキャッシュを消して描き直す。
    地図はレイヤーごとの描画結果をキャッシュしていて、外した直後は前の絵が幽霊のように残ることがある"""
    from qgis.utils import iface
    canvas = iface.mapCanvas() if iface is not None else None
    if canvas is None:
        return
    canvas.stopRendering()
    canvas.clearCache()
    canvas.refresh()


def remove_project_layers_by_sources(filepaths) -> int:
    """指定ファイルを読み込んでいるレイヤーをプロジェクトから外す。"""
    targets = {os.path.normcase(os.path.abspath(path)) for path in filepaths}
    layers = [
        layer for layer in QgsProject.instance().mapLayers().values()
        if os.path.normcase(_layer_source_path(layer)) in targets
    ]
    return _remove_layers_and_empty_groups(layers)


def project_layers_under_dir(directory: str, excluded_dirs=()) -> list:
    """指定フォルダ配下（除外フォルダを除く）のファイルを読み込んでいるレイヤー"""
    directory = os.path.normpath(directory)
    excluded_dirs = [os.path.normpath(path) for path in excluded_dirs]
    layers = []
    for layer in QgsProject.instance().mapLayers().values():
        source = _layer_source_path(layer)
        if not source or not is_under_dir(source, directory):
            continue
        if any(is_under_dir(source, excluded) for excluded in excluded_dirs):
            continue
        layers.append(layer)
    return layers


def remove_project_layers(layers) -> int:
    """レイヤーをプロジェクトから外す（空になったグループも消す）。ファイルは消さない"""
    return _remove_layers_and_empty_groups(list(layers))


def remove_project_layers_under_dir(directory: str, excluded_dirs=()) -> int:
    """指定フォルダ配下（除外フォルダを除く）のファイルを読み込んでいるレイヤーをプロジェクトから外す。"""
    return _remove_layers_and_empty_groups(project_layers_under_dir(directory, excluded_dirs))


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


def is_usable_raster_layer(layer) -> bool:
    """ラスター用の統計を取れる、使えるラスターレイヤーか。
    選択欄が指しているレイヤーは、置き換えのための削除の最中などに、ラスターでない・削除済み・無効なものが
    渡ってくることがある（ゾーニングタブで QgsVectorDataProvider の bandStatistics を呼んで落ちた例がある）"""
    if layer is None or sip.isdeleted(layer):
        return False
    raster_layer_type = getattr(QgsMapLayer, "LayerType", QgsMapLayer).RasterLayer
    if layer.type() != raster_layer_type or not layer.isValid():
        return False
    provider = layer.dataProvider()
    return provider is not None and hasattr(provider, "bandStatistics")


def is_valid_elements_layer(rlayer: QgsRasterLayer) -> bool:
    """
    ラスターレイヤーが有効な要素レイヤーかチェックする
    """
    if not is_usable_raster_layer(rlayer):
        return False
    stats = get_raster_stats(rlayer)
    return stats["MIN"] <= stats["MEAN"] and stats["MEAN"] <= stats["MAX"]


def is_valid_scoring_layer(rlayer: QgsRasterLayer) -> bool:
    """
    ラスターレイヤーが有効なスコアリングレイヤーかチェックする
    """
    if not is_usable_raster_layer(rlayer):
        return False
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
