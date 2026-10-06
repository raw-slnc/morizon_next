# This file is part of MORIZON NEXT, modified from the original MORIZON (v2.1).
# Modified by Hideharu Masai since 2026-09-29. See the Git history for the changes and their dates.
# Licensed under the GNU General Public License v3. See LICENSE and NOTICE.

from functools import lru_cache
import gc
import math
import shutil
import tempfile
import time
import os
import re

from qgis.PyQt.QtCore import QCoreApplication
from qgis.core import (
    QgsApplication,
    QgsProcessingUtils,
    QgsCoordinateReferenceSystem,
    QgsLayerTreeGroup,
    QgsMapLayer,
    QgsMapLayerProxyModel,
    QgsProject,
    QgsProviderRegistry,
    QgsRasterLayer,
    QgsVectorLayer,
)
from qgis.gui import QgsMapLayerComboBox
from qgis.PyQt import sip
import processing

from ..constants import PIXELS_THRESHOLD_RESAMPLING, MANAGED_DIR_NAME, OUTPUT_GROUP_NAME, DIR_SHARED, DIR_LAYER


def _finite_float_or_none(value):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _stats_are_finite(stats: dict) -> bool:
    return all(_finite_float_or_none(value) is not None for value in stats.values())


def _gdal_band_metadata(rlayer: QgsRasterLayer):
    """GDAL で開けるラスターなら (ドライバ名, 1バンド目のメタデータの辞書)、それ以外は (None, {}) を返す"""
    if rlayer.providerType() != "gdal":
        return None, {}
    from osgeo import gdal

    path = QgsProviderRegistry.instance().decodeUri("gdal", rlayer.source()).get("path") or rlayer.source()
    try:
        dataset = gdal.OpenEx(path, gdal.OF_RASTER)
    except RuntimeError:
        dataset = None
    if dataset is None or dataset.RasterCount < 1:
        return None, {}
    driver_name = dataset.GetDriver().ShortName
    band = dataset.GetRasterBand(1)
    band_metadata = dict(band.GetMetadata() or {})
    if driver_name != "MBTiles" and "STATISTICS_MAXIMUM" not in band_metadata:
        # 記録が無ければ GDAL に計算させる。原版（レイヤー情報の表示から読んでいた）と同じく、大きなラスターでは
        # 一部のセルから見積もる概算の統計値にする（地形の複雑さは、この値を使う v2.1 の結果に合わせて調整してあり、
        # 全セルから正確に計算すると結果が変わる）。計算できなければ、呼び出し側で QGIS の統計値を使う
        try:
            statistics = band.GetStatistics(True, True)
        except RuntimeError:
            statistics = None
        if statistics and len(statistics) == 4:
            band_metadata.update(zip(
                ("STATISTICS_MINIMUM", "STATISTICS_MAXIMUM", "STATISTICS_MEAN", "STATISTICS_STDDEV"), statistics
            ))
    dataset = None
    return driver_name, band_metadata


@lru_cache(maxsize=None)
def get_raster_stats(rlayer: QgsRasterLayer) -> dict:
    """ラスターレイヤーの統計値を取得する

    Args:
        rlayer (QgsRasterLayer)

    Returns:
        dict: {'MAX': float, 'MEAN': float, 'MIN': float, 'STD_DEV': float}
    """

    """
    まずはラスターのファイルに記録された統計値（GDAL のバンドのメタデータ）を読む。
    ファイルを開いたときなどに、次のような値が記録されていることがある
    STATISTICS_MAXIMUM=255
    STATISTICS_MEAN=206.68737191625
    STATISTICS_MINIMUM=0
    STATISTICS_STDDEV=55.286417545366
    （原版はレイヤー情報の表示用の文章（HTML）を XML として読んで探していたが、
    GDAL から直接受け取る形にした）
    """

    metadata_stats = {
        "STATISTICS_MAXIMUM": None,
        "STATISTICS_MINIMUM": None,
        "STATISTICS_MEAN": None,
        "STATISTICS_STDDEV": None,
    }

    driver_name, band_metadata = _gdal_band_metadata(rlayer)
    if driver_name == "MBTiles":
        # MBTilesは処理対象外＋計算コストが非常に大きいので、計算せず不正な値を返す（ラスタータイルと同じ値）
        return {"MIN": 1000000, "MAX": -10000, "MEAN": 1000000, "STD_DEV": 1}
    for prefix in metadata_stats:
        if prefix in band_metadata:
            metadata_stats[prefix] = _finite_float_or_none(band_metadata[prefix])

    # メタデータに統計値が含まれていない場合は計算する
    band_stats = rlayer.dataProvider().bandStatistics(1)
    stats = {
        "MAX": metadata_stats["STATISTICS_MAXIMUM"]
        if metadata_stats["STATISTICS_MAXIMUM"] is not None
        else _finite_float_or_none(band_stats.maximumValue),
        "MIN": metadata_stats["STATISTICS_MINIMUM"]
        if metadata_stats["STATISTICS_MINIMUM"] is not None
        else _finite_float_or_none(band_stats.minimumValue),
        "MEAN": metadata_stats["STATISTICS_MEAN"]
        if metadata_stats["STATISTICS_MEAN"] is not None
        else _finite_float_or_none(band_stats.mean),
        "STD_DEV": metadata_stats["STATISTICS_STDDEV"]
        if metadata_stats["STATISTICS_STDDEV"] is not None
        else _finite_float_or_none(band_stats.stdDev),
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

    # processes は utils を使うため、utils の先頭では読み込まない（読み込み順によって循環して失敗する）
    from ..processes import raster_styler

    # QGIS画面上の地図スタイルを変換しないために、filepathから新たにインスタンスを作成する
    rlayer_from_path = QgsRasterLayer(rlayer_filepath)
    renderer = raster_styler.get_quantile_renderer(
        rlayer_from_path, [[0, 0, 0] for _ in range(classes_count)]
    )
    rlayer_from_path.setRenderer(renderer)

    # 等量区分のしきい値を、作ったスタイルから直接受け取る（小数第4位に丸める）。
    # 原版はスタイルをファイル（QML）に書き出し、XML として読み直していた
    items = renderer.shader().rasterShaderFunction().colorRampItemList()

    thresholds = []
    for i in range(classes_count - 1):
        try:
            value = items[i].value
        except IndexError:
            value = 0
        thresholds.append(value if math.isinf(value) else round(value, 4))

    return thresholds


def find(values: list, x) -> int:
    """
    https://note.nkmk.me/python-list-index/
    配列から要素を検索し、存在すればそのインデックスを返す
    存在しなければ-1を返す

    Args:
        values ([type]): 検索対象の配列
        x ([type]): 検索する値

    Returns:
        int: 見つかった最初の要素のインデックス
    """
    return values.index(x) if x in values else -1


def is_morizon_managed_layer(layer: QgsMapLayer, allowed_names=None,
                             allowed_extensions=None) -> bool:
    """
    MORIZON NEXTが今の作業フォルダ（プロジェクト内の morizon_next、または外部のフォルダ）、
    または描画用の DB（morizon_next/LAYER/<qgz 名>）で管理する成果物レイヤーか判定する。
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
    roots = [root for root in (get_workspace_dir(), get_morizon_layer_db_dir()) if root]
    if not any(is_under_dir(normalized_source, root) for root in roots):
        return False
    if allowed_extensions is None:
        return True
    return os.path.splitext(normalized_source)[1].lower() in {
        ext.lower() for ext in allowed_extensions
    }


# 選択欄の候補は「表示してよい一覧」で絞る。指定が空だと「制限なし」になるため、プロジェクトに入れない目印を必ず含める
_no_layer_marker = None


def _scope_marker():
    global _no_layer_marker
    if _no_layer_marker is None:
        _no_layer_marker = QgsVectorLayer("Point?crs=EPSG:4326", "_morizon_next_none", "memory")
    return _no_layer_marker


def set_morizon_layer_scope(combobox: QgsMapLayerComboBox,
                            allowed_names=None,
                            allowed_extensions=None,
                            keep_current=True):
    """QgsMapLayerComboBoxの候補をMORIZON管理フォルダ配下の成果物に限定する。
    候補が無ければ空にする(現在の選択がMORIZON管理フォルダ配下でないなら、選択を維持せず外す)。

    「表示してよい一覧」で絞るので、他のプラグインや手動で後から追加されたレイヤーは一覧に入らない
    （以前の「除外の一覧」は作った時点のレイヤーしか外せず、後から追加されたものを空の選択欄が拾っていた）。
    呼ぶのは、画面を開いた・タブを切り替えた・自分の出力を作った/外した・作業フォルダが変わった・
    ボタン操作のときだけにすること。選択欄やプロジェクトの合図の中（レイヤーの追加・削除の途中）から呼ぶと、
    QGIS が選択欄の一覧を更新している途中に触れることになり、QGIS ごと落ちる。
    選択欄の中の一覧（exceptedLayerList など）は読み出さないこと。削除されたレイヤーが壊れた参照のまま残り、
    読み出した時点で落ちる"""
    allowed_layers = [
        layer for layer in QgsProject.instance().mapLayers().values()
        if is_morizon_managed_layer(
            layer,
            allowed_names=allowed_names,
            allowed_extensions=allowed_extensions,
        )
    ]
    previous_layer = combobox.currentLayer()
    keep_layer = None
    if keep_current and previous_layer is not None:
        keep_layer = next((layer for layer in allowed_layers if layer is previous_layer), None)
    # 候補を入れ直すと、選択が変わっていなくても「選択が変わった」の合図が出ることがある（Windows で確認）。
    # その合図で、しきい値の計算し直し（初期値に戻る）が走っていたため、入れ直しても選択が変わらなければ合図を出さない
    model = sip.cast(combobox.model(), QgsMapLayerProxyModel)
    combobox.blockSignals(True)
    try:
        combobox.setExceptedLayerList([])  # 以前の作りで入れた除外の一覧を空にする（壊れた参照を残さない）
        model.setLayerAllowlist([_scope_marker()] + allowed_layers)
        if keep_layer is not None and combobox.currentLayer() is not keep_layer:
            combobox.setLayer(keep_layer)
    finally:
        combobox.blockSignals(False)
    if combobox.currentLayer() is not previous_layer:
        combobox.layerChanged.emit(combobox.currentLayer())


# 自分の出力が増えた・減ったことを画面へ知らせる（画面は、まとめて後で選択欄の候補などを合わせ直す）。
# プロジェクト全体のレイヤーの増減は見張らない（他のプラグインの操作に反応しないため）
_outputs_changed_callback = None


def set_outputs_changed_callback(callback):
    global _outputs_changed_callback
    _outputs_changed_callback = callback


def _notify_outputs_changed():
    if _outputs_changed_callback is not None:
        _outputs_changed_callback()


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
    # キャッシュ（shared）・描画用の DB（LAYER）のフォルダと重ならないようにする
    return name + "_project" if name in (DIR_SHARED, DIR_LAYER) else name


def get_morizon_root_dir(*subdirs) -> str:
    """<qgz のフォルダ>/morizon_next。プロジェクトごとの作業フォルダと、共有キャッシュ（shared）を置く"""
    project_home = QgsProject.instance().homePath() or os.path.expanduser("~")
    return os.path.normpath(os.path.join(project_home, MANAGED_DIR_NAME, *subdirs))


def get_morizon_managed_dir(*subdirs) -> str:
    """プロジェクト内の作業フォルダ（<qgz のフォルダ>/morizon_next/<qgz のファイル名>）。subdirsを渡すとその下のパス。
    構成は constants.py の DIR_DATA 等を参照"""
    return get_morizon_root_dir(_project_folder_name(), *subdirs)


def get_morizon_layer_db_dir() -> str:
    """描画用の DB（GPKG）を置くフォルダ（<qgz のフォルダ>/morizon_next/LAYER/<qgz のファイル名>）。
    プロジェクトが未保存なら空文字（layer_db.py）"""
    if not QgsProject.instance().homePath():
        return ""
    return get_morizon_root_dir(DIR_LAYER, _project_folder_name())


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
STAGE_ROAD_BUILDING = "road_building"  # 要素計算タブの「道路と建物を出力」で出す、入力の道路縁・建築物


def tag_output_layer(layer, stage: str, key: str = ""):
    """出力レイヤーに印（工程と、要素などの区別）を付ける（出力をプロジェクトに加える直前に呼ばれる）"""
    layer.setCustomProperty(OUTPUT_TAG_KEY, f"{stage}|{key}")
    _notify_outputs_changed()


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
    return detach_layers(output_layers(stage, keys))


def detach_layers(layers) -> list:
    """レイヤーを、削除せずにプロジェクトから取り外して返す（detach_output_layers と同じ。対象を呼び出し側が決める）"""
    project = QgsProject.instance()
    root = project.layerTreeRoot()
    records = []
    for layer in layers:
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
        _notify_outputs_changed()
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
    _notify_outputs_changed()


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
        _notify_outputs_changed()


# レイヤーを作り直すとき（外してから作る一連の流れ）に「Morizon Next」グループの場所を引き継ぐため、
# 中身が空になって取り除いたときの場所（最上位の何番目か・チェック・展開）を覚えておく。
# プロジェクトを開いたときは、保存されていたレイヤーを外す時点で覚えるので、再開で作り直すと前回あった場所に戻る。
# 前のプロジェクトの場所を持ち越さないよう、プロジェクトが閉じられたとき（cleared）に forget_output_group_place で忘れる
_output_group_place = None


def forget_output_group_place():
    global _output_group_place
    _output_group_place = None


def get_morizon_output_group():
    """出力レイヤーをまとめるグループ（プロジェクトの最上位の「Morizon Next」）。
    無ければ、直前に取り除いた場所があればそこに、無ければ一番上に作る。
    中身が空になったときは _remove_layers_and_empty_groups が取り除く"""
    global _output_group_place
    root = QgsProject.instance().layerTreeRoot()
    for child in root.children():
        if isinstance(child, QgsLayerTreeGroup) and child.name() == OUTPUT_GROUP_NAME:
            _output_group_place = None
            return child
    if _output_group_place is None:
        group = root.insertGroup(0, OUTPUT_GROUP_NAME)
        group.setExpanded(True)
        return group
    index, checked, expanded = _output_group_place
    _output_group_place = None
    group = root.insertGroup(min(index, len(root.children())), OUTPUT_GROUP_NAME)
    group.setItemVisibilityChecked(checked)
    group.setExpanded(expanded)
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
        _notify_outputs_changed()
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
        if parent is root and group.name() == OUTPUT_GROUP_NAME:
            # 作り直すときに同じ場所へ作れるよう、場所を覚えておく（get_morizon_output_group）
            global _output_group_place
            _output_group_place = (
                root.children().index(group), group.itemVisibilityChecked(), group.isExpanded()
            )
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


def get_tiff_info(tiff_filepath: str, feedback=None) -> dict:
    """
    ラスターの各種情報を取得する。QGIS の中の GDAL で直接読む
    （原版は gdalinfo のコマンドを Processing 経由で呼んでいた。外部のコマンドにパスを渡さないため、
    全角文字を含むパスでも別名に差し替えずに読める）。feedback は互換のために受け取るだけ

    Returns:
        dict: {"crs", "resolution", "extent", "nodata_value", "size"}
    """
    from osgeo import gdal

    if not os.path.exists(tiff_filepath):
        raise RuntimeError(f"ラスターのファイルがありません: {tiff_filepath}")
    try:
        gdalinfo = gdal.Info(tiff_filepath, format="json")
    except RuntimeError as e:
        raise RuntimeError(f"ラスターの情報を読めませんでした: {tiff_filepath}（{e}）") from e
    if not gdalinfo:
        raise RuntimeError(f"ラスターの情報を読めませんでした: {tiff_filepath}")

    wkt = (gdalinfo.get("coordinateSystem") or {}).get("wkt")
    crs = QgsCoordinateReferenceSystem.fromWkt(wkt) if wkt else QgsCoordinateReferenceSystem()
    if not crs.isValid():
        # GDAL から座標系が見えないときは、QGIS がそのラスターを開いたときの座標系を使う
        crs = QgsRasterLayer(tiff_filepath, "crs_check").crs()
    if not crs.isValid():
        raise RuntimeError(f"ラスターの座標系を読めませんでした: {tiff_filepath}")

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


def _has_non_ascii(text) -> bool:
    return isinstance(text, str) and re.search("[^\x01-\x7E]", text) is not None


def _move_with_siblings(src: str, dst: str):
    """src と同じベース名のファイル（.shp の付随ファイル、.aux.xml など）もまとめて dst 側へ移す"""
    src_dir = os.path.dirname(src)
    src_base = os.path.splitext(os.path.basename(src))[0]
    dst_dir = os.path.dirname(dst) or "."
    dst_base = os.path.splitext(os.path.basename(dst))[0]
    os.makedirs(dst_dir, exist_ok=True)
    for name in os.listdir(src_dir):
        if name == src_base or name.startswith(src_base + "."):
            target = os.path.join(dst_dir, dst_base + name[len(src_base):])
            if os.path.exists(target):
                os.remove(target)
            shutil.move(os.path.join(src_dir, name), target)


def run_processing(algorithm_id: str, parameters: dict, feedback=None, context=None):
    """
    processing.run の代わりに使う入口。GRASS・SAGA・GDAL のコマンドなど外部の処理に、全角文字を含むパスを渡さない
    （移植時の方針：利用者のフォルダ名・保存場所は変えさせず、処理の内部でだけ半角のパスで扱う）。
    - 入力：全角文字を含む既存ファイルのパスは、半角の別名（get_ascii_safe_alias）に差し替える
    - 出力：全角文字を含む出力先は、半角の一時フォルダに書かせてから元の場所へ移す
    - Processing が自分で作る一時出力（TEMPORARY_OUTPUT など）も、半角のフォルダに置かせる
    """
    from processing.tools import dataobjects

    own_context = context is None
    if own_context:
        context = dataobjects.createContext(feedback)
    safe_root = get_ascii_safe_dir("morizon_next_tmp")
    # 区切りは QGIS がフォルダを覚える形（/）にそろえる。Windows で \ のまま渡すと、QGIS は既にある一時フォルダを
    # 「指定の場所の中」と見なせず、問い合わせのたびに新しい一時フォルダを作る。GRASS は1回の計算の中で一時フォルダを
    # 何度も問い合わせるため、作業場所が食い違って出力ができなかった（Windows の起伏量の計算で発生）
    context.setTemporaryFolder(safe_root.replace("\\", "/"))
    # 呼ぶ前に、QGIS の一時フォルダを半角の置き場所へ切り替えておく。GRASS は計算を始めるとき（作業場所を作るとき）に
    # 場所を指定せずに一時フォルダを問い合わせ、そのあとで context 付きで問い合わせる。先に切り替えておかないと、
    # その QGIS で最初のプロセシングの呼び出しが GRASS だった場合に、作業場所を作ったフォルダと計算で探すフォルダが
    # 食い違って失敗する（集計タブの流域づくりで発生）
    QgsProcessingUtils.tempFolder(context)

    algorithm = QgsApplication.processingRegistry().algorithmById(algorithm_id)
    output_names = {d.name() for d in algorithm.destinationParameterDefinitions()} if algorithm else set()

    params = dict(parameters)
    moves = {}  # 出力の名前 -> (半角の一時パス, 本来の出力先)
    work_dir = None
    aliases = []
    for name, value in params.items():
        if not _has_non_ascii(value):
            continue
        if name in output_names:
            if work_dir is None:
                work_dir = tempfile.mkdtemp(prefix="out_", dir=safe_root)
            temp_path = os.path.join(work_dir, f"{name}{os.path.splitext(value)[1]}")
            moves[name] = (temp_path, value)
            params[name] = temp_path
        elif os.path.exists(value):
            params[name] = get_ascii_safe_alias(value, created=aliases)

    try:
        result = processing.run(algorithm_id, params, feedback=feedback, context=context)
        for name, (temp_path, final_path) in moves.items():
            if os.path.exists(temp_path):
                _move_with_siblings(temp_path, final_path)
            if isinstance(result, dict) and result.get(name) == temp_path:
                result[name] = final_path
        return result
    finally:
        if work_dir is not None:
            shutil.rmtree(work_dir, ignore_errors=True)
        if aliases and own_context:
            # 入力として開いたレイヤーは context が持っていて、閉じるときに統計値を別名の横（.aux.xml）へ書き出す。
            # 先に閉じてから別名を消す（あとに閉じると、消したあとで .aux.xml だけが残る）
            context.temporaryLayerStore().removeAllMapLayers()
            context = None
            gc.collect()
        remove_files(aliases)


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
    if not _stats_are_finite(stats):
        return False
    return stats["MIN"] <= stats["MEAN"] and stats["MEAN"] <= stats["MAX"]


def is_valid_scoring_layer(rlayer: QgsRasterLayer) -> bool:
    """
    ラスターレイヤーが有効なスコアリングレイヤーかチェックする
    """
    if not is_usable_raster_layer(rlayer):
        return False
    stats = get_raster_stats(rlayer)
    if not _stats_are_finite(stats):
        return False
    is_tile = stats["MIN"] >= stats["MAX"]
    has_negative = stats["MIN"] < 0
    not_integer = stats["MIN"] != int(stats["MIN"]) or stats["MAX"] != int(stats["MAX"])

    is_invalid = is_tile or has_negative or not_integer
    return not is_invalid


# 半角の置き場所の基準にする、OS 本来の一時フォルダ。計算の間は一時フォルダを半角の置き場所の中へ切り替えるので
# （AsciiSafeProcessingTmpdir）、そのたびに問い合わせると置き場所の中にさらに置き場所を作ってしまう。読み込み時に覚えておく
_BASE_TEMP_DIR = tempfile.gettempdir()
# 半角の置き場所（get_ascii_safe_dir("morizon_next_tmp")）の中で、片付けの対象にする古さ（秒）
TIDY_AGE_SECONDS = 24 * 60 * 60


def get_ascii_safe_dir(subdir_name: str) -> str:
    """
    ユーザー名・プロジェクト名等の全角文字を経由しない、ドライブ直下の半角安全な
    ディレクトリパスを返す（フォルダが無ければ作成する）。
    SAGA/GRASSの一時フォルダや、プラグイン自身が管理するデータの保存先など、
    「ユーザーの環境設定は変えず、内部で吸収する」ために使う。
    """
    drive = os.path.splitdrive(_BASE_TEMP_DIR)[0]
    if drive:
        safe_dir = os.path.join(drive + os.sep, subdir_name)
    else:
        safe_dir = os.path.join(_BASE_TEMP_DIR, subdir_name)
    os.makedirs(safe_dir, exist_ok=True)
    return safe_dir


def get_ascii_safe_alias(real_path: str, created: list = None) -> str:
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

    created にリストを渡すと、作った別名のファイル（付随ファイルを含む）を足す。使い終わったら
    remove_files で消す（残すと半角の置き場所にたまり、コピーの場合は元と同じ大きさを使い続けるため）
    """
    if re.search("[^\x01-\x7E]", real_path) is None:
        return real_path

    real_path = os.path.abspath(real_path)
    real_drive = os.path.splitdrive(real_path)[0]
    if real_drive:
        alias_dir = _ascii_safe_root_of(real_path)
    else:
        alias_dir = get_ascii_safe_dir("morizon_next_tmp")
    os.makedirs(alias_dir, exist_ok=True)

    import hashlib
    path_hash = hashlib.sha1(real_path.encode("utf-8"), usedforsecurity=False).hexdigest()[:12]
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
        if created is not None:
            created.append(sibling_dst)

    return alias_path


def remove_files(paths):
    """ファイルを消す（無い・使用中で消せないものは飛ばす）。GDAL があとから横に作る .aux.xml も消す"""
    for path in paths:
        for target in (path, path + ".aux.xml"):
            try:
                os.remove(target)
            except OSError:
                continue


def _newest_mtime(path: str) -> float:
    """フォルダならその中で最も新しく書き換えられた時刻、ファイルならその時刻"""
    newest = os.path.getmtime(path)
    if os.path.isdir(path):
        for root, dirs, files in os.walk(path):
            for name in dirs + files:
                try:
                    newest = max(newest, os.path.getmtime(os.path.join(root, name)))
                except OSError:
                    continue
    return newest


def _remove_entry(path: str):
    if os.path.isdir(path) and not os.path.islink(path):
        shutil.rmtree(path, ignore_errors=True)
    else:
        remove_files([path])


# 片付けの記録は QGIS 本体（QCoreApplication）の属性に持たせる。プラグインを読み直してもモジュールの変数は
# 作り直されるが、QGIS を起動している間は残したいため
_TIDY_ROOTS_PROPERTY = "morizon_next_tidied_tmp_roots"   # 片付けた置き場所（同じ置き場所は1回だけ片付ける）
_TIDY_ASKED_PROPERTY = "morizon_next_tidy_asked"          # 消してよいか聞いたか（起動中に聞くのは1回だけ）
_TIDY_RUNNING_PROPERTY = "morizon_next_tidy_running"      # 片付けの最中か（同時に走らせない）


def _app_property(name, default):
    value = QCoreApplication.instance().property(name)
    return default if value is None else value


def _set_app_property(name, value):
    QCoreApplication.instance().setProperty(name, value)


def _ascii_safe_root_of(path: str):
    """path があるドライブの半角の置き場所（入力の別名を作る場所。get_ascii_safe_alias と同じ決め方）。
    ドライブの区別が無い OS では get_ascii_safe_dir("morizon_next_tmp")"""
    drive = os.path.splitdrive(os.path.abspath(path))[0] if path else ""
    if drive:
        return os.path.join(drive + os.sep, "morizon_next_tmp")
    return get_ascii_safe_dir("morizon_next_tmp")


def ascii_safe_tmp_roots() -> list:
    """一時ファイルの置き場所の一覧：既定の置き場所と、今の作業フォルダがあるドライブの置き場所（あれば）"""
    roots = [get_ascii_safe_dir("morizon_next_tmp")]
    workspace = get_workspace_dir()
    if workspace:
        root = _ascii_safe_root_of(workspace)
        if os.path.normcase(os.path.abspath(root)) != os.path.normcase(os.path.abspath(roots[0])) \
                and os.path.isdir(root):
            roots.append(root)
    return roots


def _own_processing_tmp() -> str:
    """今使っている QGIS 自身のプロセシングの一時フォルダ（比べやすい形）。片付けでは消さない。
    消すと QGIS は作り直さないので、この QGIS での以後の計算が出力を作れずに失敗する"""
    return os.path.normcase(os.path.abspath(QgsProcessingUtils.tempFolder()))


def clear_ascii_safe_tmp(roots):
    """置き場所の中身を、日付に関係なくすべて消す（「設定をクリアする」で選んだとき）。
    今使っている QGIS 自身のプロセシングの一時フォルダは残す（消すと、この QGIS での以後の計算が失敗するため）。
    消せないもの（使用中・ほかの利用者のファイルなど）は飛ばす"""
    own = _own_processing_tmp()
    for root in roots:
        if not os.path.isdir(root):
            continue
        for name in os.listdir(root):
            path = os.path.join(root, name)
            if os.path.normcase(os.path.abspath(path)) == own:
                continue
            _remove_entry(path)


def tidy_ascii_safe_tmp_for_session(confirm_recent=None):
    """一時ファイルを置くドライブ（既定の置き場所）と、今の作業フォルダがあるドライブの置き場所を片付ける。
    入力のファイルは作業フォルダに取り込まれるので、別名ができるのはこの2か所のどちらか。
    この QGIS の起動中にすでに片付けた置き場所は飛ばす（作業フォルダが別のドライブに切り替わったときに呼ぶと、
    新しいドライブの置き場所だけを片付ける）。
    同時には走らせない（質問の画面を出している間に別の経路から呼ばれても何もしない）。消してよいか聞くのは、
    この QGIS の起動中に1回だけ（「はい」でも「いいえ」でも、以後は聞かずに古いものだけ片付ける）"""
    if _app_property(_TIDY_RUNNING_PROPERTY, False):
        return
    _set_app_property(_TIDY_RUNNING_PROPERTY, True)
    try:
        def confirm_once(root):
            if confirm_recent is None or _app_property(_TIDY_ASKED_PROPERTY, False):
                return False
            _set_app_property(_TIDY_ASKED_PROPERTY, True)
            return confirm_recent(root)

        tidied = list(_app_property(_TIDY_ROOTS_PROPERTY, []))
        for root in ascii_safe_tmp_roots():
            key = os.path.normcase(os.path.abspath(root))
            if key in tidied or not os.path.isdir(root):
                continue
            tidied.append(key)
            _set_app_property(_TIDY_ROOTS_PROPERTY, tidied)
            tidy_ascii_safe_tmp(confirm_once, root=root)
    finally:
        _set_app_property(_TIDY_RUNNING_PROPERTY, False)


def tidy_ascii_safe_tmp(confirm_recent=None, root=None):
    """半角の置き場所（get_ascii_safe_dir("morizon_next_tmp")、または root）に残った一時ファイルを片付ける。
    - 最後に書き換えられてから TIDY_AGE_SECONDS 以上たったものは、聞かずに消す
    - それより新しいものが残っていれば confirm_recent(置き場所のパス) を呼び、True が返れば消す
      （直前に QGIS が落ちたのか、ほかの QGIS で計算中なのかはプラグインから見分けられないので、利用者に聞く）
    正常に終えた計算は自分の一時ファイルを消すので、普段は何も残らず、聞かれない"""
    root = root or get_ascii_safe_dir("morizon_next_tmp")
    own = _own_processing_tmp()
    now = time.time()
    recent = []
    for name in os.listdir(root):
        path = os.path.join(root, name)
        if os.path.normcase(os.path.abspath(path)) == own:
            # この QGIS 自身のプロセシングの一時フォルダ（プラグインを読み直したときなど、起動中に片付けが走る場合がある）
            continue
        try:
            age = now - _newest_mtime(path)
        except OSError:
            continue
        if age >= TIDY_AGE_SECONDS:
            _remove_entry(path)
        else:
            recent.append(path)
    if recent and confirm_recent is not None and confirm_recent(root):
        for path in recent:
            _remove_entry(path)


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
        self._original_tempfile_tempdir = None
        self._run_dir = None
        self._entered = False

    def enter(self):
        if self._entered:
            return
        self._original_tmp = os.environ.get("TMP")
        self._original_temp = os.environ.get("TEMP")
        self._original_tempfile_tempdir = tempfile.tempdir

        # 計算ごとに専用のフォルダを作り、終わったら丸ごと消す（スタイルの作業ファイルや GRASS・SAGA の一時ファイルを
        # 共有の置き場所に残さないため）
        safe_dir = tempfile.mkdtemp(prefix="run_", dir=get_ascii_safe_dir("morizon_next_tmp"))
        self._run_dir = safe_dir

        os.environ["TMP"] = safe_dir
        os.environ["TEMP"] = safe_dir
        # tempfile は gettempdir() の結果をモジュール変数へキャッシュするため、
        # 環境変数の変更だけでは以後の mkdtemp() が元の全角パスを使い続ける。
        # 外部処理用の一時ファイルを確実に半角パスへ作るため、キャッシュも切り替える。
        tempfile.tempdir = safe_dir
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
        tempfile.tempdir = self._original_tempfile_tempdir
        shutil.rmtree(self._run_dir, ignore_errors=True)
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
