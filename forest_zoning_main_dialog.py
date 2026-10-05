# This file is part of MORIZON NEXT, modified from the original MORIZON (v2.1).
# Modified by Hideharu Masai since 2026-09-29. See the Git history for the changes and their dates.
# Licensed under the GNU General Public License v3. See LICENSE and NOTICE.

import os

from qgis.PyQt import uic
from contextlib import contextmanager

from qgis.PyQt.QtCore import Qt, QTimer, QUrl
from qgis.PyQt.QtGui import QDesktopServices, QIcon, QKeySequence
try:
    from qgis.PyQt.QtGui import QShortcut  # Qt6
except ImportError:
    from qgis.PyQt.QtWidgets import QShortcut  # Qt5
from qgis.PyQt.QtWidgets import (
    QDialog, QDoubleSpinBox, QGridLayout, QHBoxLayout, QLabel, QMessageBox, QPushButton, QSpinBox, QVBoxLayout,
    QWidget,
)
from qgis.gui import QgsMapLayerComboBox
from qgis.core import Qgis, QgsMessageLog, QgsProject

from .forest_zoning_main_dialog_elements import ForestZoningMainDialogElements
from .forest_zoning_main_dialog_scoring import ForestZoningMainDialogScoring
from .forest_zoning_main_dialog_zoning import ForestZoningMainDialogZoning
from .forest_zoning_main_dialog_aggregate import ForestZoningMainDialogAggregate
from .forest_zoning_main_dialog_printlayout import ForestZoningMainDialogPrintlayout
from .forest_zoning_main_dialog_settings import ForestZoningMainDialogSettings
from .forest_zoning_main_dialog_archive import ForestZoningMainDialogArchive
from .forest_zoning_main_dialog_costcsv_editor import ElidedLabel
# utils は processes より後に読み込む（utils が processes.raster_styler を使い、processes が utils を使うため。
# 先に読み込むと processes の読み込みが途中で失敗する）
from . import morizon_data, utils
from .constants import DIR_AGGREGATE, DIR_DATA, DIR_SHARED, DIR_YOUSO, DIR_ZONING, OUTPUT_AGGREGATE


class ForestZoningMainDialog(QDialog):
    def __init__(self):
        super().__init__()
        flags = self.windowFlags()
        flags &= ~Qt.WindowType.WindowType_Mask
        flags |= Qt.WindowType.Window | Qt.WindowType.WindowMinimizeButtonHint
        self.setWindowFlags(flags)

        self.ui = uic.loadUi(
            os.path.join(os.path.dirname(__file__), "forest_zoning_main_dialog.ui"),
            self,
        )
        self.setWindowIcon(
            QIcon(os.path.join(os.path.dirname(__file__), "imgs", "icon.png"))
        )

        # 「設定」タブに設定画面のUIを埋め込む
        self.settings_widget = uic.loadUi(
            os.path.join(os.path.dirname(__file__), "forest_zoning_settings_dialog.ui")
        )
        self.settingsTabLayout.addWidget(self.settings_widget)
        self._init_tab_shortcuts()

        # 各タブのUIを初期化する：実装は各クラスへ移譲
        self.elements = ForestZoningMainDialogElements(self)
        self.scoring = ForestZoningMainDialogScoring(self)
        self.zoning = ForestZoningMainDialogZoning(self)
        self.aggregate = ForestZoningMainDialogAggregate(self)
        self.printlayout = ForestZoningMainDialogPrintlayout(self)
        self.settings = ForestZoningMainDialogSettings(self.settings_widget, self)
        self.archive = ForestZoningMainDialogArchive(self)
        self._input_import_suppressed = 0
        self._restore_pending = False
        self.lock_output_dirs()
        self._init_workspace_status()
        self._init_layer_watch()
        self.use_no_workspace(persist=False)
        # 開いているプロジェクトがあれば、画面を表示したときに再開の分岐へ進む
        if QgsProject.instance().fileName():
            self._restore_pending = True

    # タブの切り替え（Ctrl+Tab／Ctrl+Shift+Tab）。Qt のタブはこのキーで切り替わるが、フォーカスのある部品が
    # キーを先に受け取ると届かない（レイヤーの選択欄で止まった）。画面全体のショートカットにして、どこからでも切り替える。
    # 実際のキーボードの Ctrl+Shift+Tab は、OS のキー情報から「Ctrl+Shift+Tab」と「Ctrl+Backtab」の両方に当てはまる。
    # 以前は2つとも登録していたため Qt はどちらを実行するか決められず（activatedAmbiguously）、何もしなかった。
    # そのため Ctrl+Shift+Tab だけを登録する。念のため、決められないときも同じ切り替えをする
    # （決められないときは、押すたびにどれか1つにだけ届く）
    def _init_tab_shortcuts(self):
        self._tab_shortcuts = []
        for sequence, step in (("Ctrl+Tab", 1), ("Ctrl+Shift+Tab", -1)):
            shortcut = QShortcut(QKeySequence(sequence), self)
            shortcut.setContext(Qt.ShortcutContext.WindowShortcut)
            shortcut.activated.connect(lambda step=step: self._step_tab(step))
            shortcut.activatedAmbiguously.connect(lambda step=step: self._step_tab(step))
            self._tab_shortcuts.append(shortcut)

    def _step_tab(self, step: int):
        count = self.tabWidget.count()
        if count:
            self.tabWidget.setCurrentIndex((self.tabWidget.currentIndex() + step) % count)

    # ── 作業フォルダ ───────────────────────────────────────────────────────
    # 作業フォルダは入力（DATA/）と出力（YOUSO/・ZONING/・AGGREGATE/）を置く場所で、3つの状態がある。
    #   なし：初期状態（設定のクリア・未保存のプロジェクト・記録の無いプロジェクト）。出力先は空欄で、入力欄の「…」は使えない
    #   プロジェクト内：<プロジェクト>/morizon_next（DEMブラウザから開始・保存ファイルの読み直し・ZIPの読み込み）
    #   外部：「フォルダ選択から開始する」「保存ファイルを読み込む（フォルダ）」で選んだフォルダ（その場で使う）
    # 作業フォルダはプロジェクトに書き込み、プロジェクトを開いたときに再開するかを聞く。
    # 出力先は作業フォルダの中に決まり、ユーザーは変更できない

    def output_targets(self):
        return (
            (self.elementsOutputDirFileWidget, (DIR_YOUSO,)),
            (self.scoringOutputDirFileWidget, (DIR_ZONING,)),
            (self.zoningOutputDirFileWidget, (DIR_ZONING,)),
            (self.aggregateOutputDirFileWidget, (DIR_AGGREGATE, OUTPUT_AGGREGATE["FILE_NAME"] + ".shp")),
        )

    def has_workspace(self) -> bool:
        """作業フォルダが決まっているか（プロジェクト内の作業フォルダは、プロジェクトが保存済みのときだけ）"""
        workspace = utils.get_workspace()
        if workspace is None:
            return False
        if workspace == utils.WORKSPACE_INTERNAL:
            return QgsProject.instance().homePath() != ""
        return True

    def apply_workspace_output_dirs(self):
        """出力先の欄を、今の作業フォルダの中の決まった場所にする（作業フォルダが無ければ空欄）"""
        for filewidget, parts in self.output_targets():
            if not self.has_workspace():
                filewidget.setFilePath("")
                continue
            path = utils.get_workspace_dir(*parts)
            os.makedirs(path if len(parts) == 1 else os.path.dirname(path), exist_ok=True)
            filewidget.setFilePath(path)
        if getattr(self, "_tmp_tidied", False):
            # 作業フォルダが別のドライブに切り替わったら、そのドライブの一時ファイルも片付ける（片付け済みなら何もしない）
            QTimer.singleShot(0, self._tidy_tmp_files)

    def lock_output_dirs(self):
        """出力先は作業フォルダの中に決まり変えられないので、選ぶ欄ではなく文字列として見せる。
        欄そのものは隠して値の入れ物にし（計算の処理がそのまま読む）、同じ場所に省略表示付きのラベルを置く。
        長いパスは途中を「…」で省略し、マウスを重ねると全文を出す"""
        self._output_dir_labels = []
        for filewidget, _ in self.output_targets():
            filewidget.setReadOnly(True)
            label = ElidedLabel()
            label.setStyleSheet("color:#333;")
            layout = filewidget.parentWidget().layout() if filewidget.parentWidget() else None
            if layout is not None and layout.replaceWidget(filewidget, label) is not None:
                filewidget.hide()
            else:
                continue
            # 行の余った幅はすべてこのラベルに回す（割り当てが無いと見出しのラベルも広がり、パスが早く省略される）。
            # replaceWidget は外側のレイアウトから中の行までたどって置き換えるので、ラベルが入った行を探して付ける
            row = self._layout_containing(layout, label)
            if row is not None and hasattr(row, "setStretchFactor"):
                row.setStretchFactor(label, 1)
            filewidget.fileChanged.connect(
                lambda path, target=label: target.setText(path, tooltip=path or self.OUTPUT_DIR_TOOLTIP)
            )
            label.setText(filewidget.filePath(), tooltip=filewidget.filePath() or self.OUTPUT_DIR_TOOLTIP)
            self._output_dir_labels.append(label)

    @classmethod
    def _layout_containing(cls, layout, widget):
        """layout とその中のレイアウトから、widget を直接持つレイアウトを探す"""
        for index in range(layout.count()):
            item = layout.itemAt(index)
            if item.widget() is widget:
                return layout
            if item.layout() is not None:
                found = cls._layout_containing(item.layout(), widget)
                if found is not None:
                    return found
        return None

    OUTPUT_DIR_TOOLTIP = "出力先は作業フォルダの中に決まります（作業フォルダ：なし のときは空欄）"

    def update_input_availability(self):
        """作業フォルダが無いときは、入力欄の「…」を使えなくする（選んだファイルをコピーする作業フォルダが無いため）"""
        enabled = self.has_workspace()
        for filewidget in self.elements.input_filewidgets().values():
            filewidget.setEnabled(enabled)

    @contextmanager
    def suppress_input_import(self):
        """プラグインが入力欄を設定する間は、「…」で選んだときの取り込み（作業フォルダへのコピー）をしない"""
        self._input_import_suppressed += 1
        try:
            yield
        finally:
            self._input_import_suppressed -= 1

    def is_input_import_suppressed(self) -> bool:
        return self._input_import_suppressed > 0

    def set_inputs_from_data_dir(self, data_dir):
        """作業フォルダの DATA にあるデータで入力欄を埋める（無い入力は空欄）"""
        found = morizon_data.find_inputs(data_dir) if data_dir and os.path.isdir(data_dir) else {}
        with self.suppress_input_import():
            for key, filewidget in self.elements.input_filewidgets().items():
                candidates = found.get(key) or []
                filewidget.setFilePath(candidates[0] if candidates else "")
            dem = found.get("dem") or []
            self.aggregateDemFileWidget.setFilePath(dem[0] if dem else "")
        self.elements.forget_dem_browser_state()
        return found

    def use_project_workspace(self, refill_inputs=True, persist=True):
        """作業フォルダをプロジェクト内（<プロジェクト>/morizon_next）にする。
        refill_inputs なら入力欄を morizon_next/DATA のデータで埋め直す。persist ならプロジェクトにも書き込む"""
        utils.set_workspace(utils.WORKSPACE_INTERNAL)
        if persist:
            utils.write_project_workspace(utils.WORKSPACE_INTERNAL)
        if refill_inputs:
            project_saved = QgsProject.instance().homePath() != ""
            self.set_inputs_from_data_dir(
                utils.get_morizon_managed_dir(DIR_DATA) if project_saved else None
            )
        self._after_workspace_changed()

    def use_external_workspace(self, root, persist=True):
        """作業フォルダを外部にする（入力欄は呼び出し側が設定する）"""
        utils.set_workspace(root)
        if persist:
            utils.write_project_workspace(root)
        self._after_workspace_changed()

    def use_no_workspace(self, persist=True):
        """作業フォルダをなし（初期状態）にする。入力欄を空にし、persist ならプロジェクトの記録も消す"""
        utils.set_workspace(None)
        if persist:
            utils.write_project_workspace(None)
        with self.suppress_input_import():
            self.elements.reset_elements_inputs()
            self.aggregateDemFileWidget.setFilePath("")
        self.elements.forget_dem_browser_state()
        self._after_workspace_changed()

    # 初期状態にする操作（DEMブラウザから開始・フォルダ選択から開始・設定をクリア・保存ファイルの読み込み）は、
    # レイヤーを初期化してから、それぞれの工程に入る。初期状態にはプラグインのレイヤーは無いので、
    # 指しているファイルの場所に関係なくすべて外す（ファイルは残す）。
    # データを消す・上書きするかは、それぞれの工程の中で別に判断する

    def layers_to_initialize(self) -> list:
        """初期化で外すレイヤー：プラグインのレイヤーすべてと、作業フォルダのファイルを自分で追加したレイヤー
        （共有キャッシュは除く）"""
        layers = utils.output_layers()
        if self.has_workspace():
            current = utils.get_workspace_dir()
            ids = {layer.id() for layer in layers}
            layers += [layer for layer in utils.project_layers_under_dir(current, [os.path.join(current, DIR_SHARED)])
                       if layer.id() not in ids]
        return layers

    @staticmethod
    def initialize_message(layers) -> str:
        return f"レイヤーを初期化します。\nMorizon Next のレイヤー {len(layers)}件をプロジェクトから外します（ファイルは残ります）。"

    def initialize_layers(self, confirm=True) -> bool:
        """プラグインのレイヤーをすべてプロジェクトから外す。confirm なら「レイヤーを初期化します」と確かめ、
        取りやめたら False（呼び出し側は何もせずに戻る）。外すレイヤーが無ければ確かめずに True"""
        layers = self.layers_to_initialize()
        if not layers:
            return True
        if confirm and not self._confirm_initialize(layers):
            return False
        utils.remove_project_layers(layers)
        return True

    def _confirm_initialize(self, layers) -> bool:
        answer = QMessageBox.question(
            self, "レイヤーの初期化", self.initialize_message(layers),
            QMessageBox.StandardButton.Ok | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Ok,
        )
        return answer == QMessageBox.StandardButton.Ok

    # ── 初期状態から始める操作を、途中で取りやめたときの戻し ─────────────────────
    # 「フォルダ選択から開始する」「DEMブラウザから開始する」などは、最初にレイヤーを初期化してから
    # フォルダ選択などへ進む。そこで取りやめても入力欄や作業フォルダは前のままなので、レイヤーも元に戻す。
    # そのため初期化では削除せずに取り外して持っておき、取りやめたら restore_initialized_layers で戻し、
    # 先へ進むときに discard_initialized_layers で削除する（ファイルの掴みを解放する）

    def detach_layers_to_initialize(self):
        """「レイヤーを初期化します」と確かめてから、プラグインのレイヤーを取り外して返す。取りやめたら None"""
        layers = self.layers_to_initialize()
        if layers and not self._confirm_initialize(layers):
            return None
        selections = self.snapshot_selections()
        return {"records": utils.detach_layers(layers), "selections": selections}

    def restore_initialized_layers(self, detached):
        utils.restore_detached_layers(detached["records"])
        self.restore_selections(detached["selections"])

    @staticmethod
    def discard_initialized_layers(detached):
        utils.discard_detached_layers(detached["records"])

    # ── 工程の上書きをキャンセルしたときの戻し ─────────────────────────
    # 工程を実行するときに片付けたレイヤーは、上書きをキャンセルしたら元に戻す（utils.restore_detached_layers）。
    # レイヤーを取り外すと、ほかのタブの選択欄からそのレイヤーが外れ、スコアリングでは選択が変わったことで
    # しきい値も初期値に戻ってしまう。そこで、取り外す前に選択欄と数値の欄を控えておき、戻すときに一緒に戻す

    def snapshot_selections(self) -> dict:
        project = QgsProject.instance()
        return {
            "layers": [(combo, combo.currentLayer().id() if combo.currentLayer() else None)
                       for combo in self.findChildren(QgsMapLayerComboBox)],
            "values": [(box, box.value()) for box in self.findChildren(QSpinBox) + self.findChildren(QDoubleSpinBox)],
            "_project": project,
        }

    def restore_selections(self, snapshot: dict):
        project = snapshot["_project"]
        for combo, layer_id in snapshot["layers"]:
            layer = project.mapLayer(layer_id) if layer_id else None
            if combo.currentLayer() is layer:
                continue
            combo.blockSignals(True)
            combo.setLayer(layer)
            combo.blockSignals(False)
        for box, value in snapshot["values"]:
            if box.value() != value:
                box.blockSignals(True)
                box.setValue(value)
                box.blockSignals(False)
        # 信号を止めて戻したので、ボタンの有効・無効などの見た目は改めて合わせる
        self.scoring.refresh_scoring_ui()
        self.zoning.refresh_zoning_ui()
        self.aggregate.refresh_aggregate_ui()

    # ── プロジェクトを開いたとき ───────────────────────────────────────
    # 記録なし → 作業フォルダなし
    # 記録あり → フォルダがあるか確かめる
    #   ある → 再開しますか？ → はい：プロジェクト内なら読み直し、外部なら作業フォルダを戻す（レイヤーはプロジェクトのものでそろっている）
    #                         いいえ：初期化して作業フォルダなし（記録も消す）
    #   無い（外部のフォルダが見つからない）→ プロジェクト内にデータがあれば、そこから再開（読み直し）
    #                                       無ければ知らせてクリア（作業フォルダなし、記録も消す）
    # 画面を開いていないときは、次に画面を表示したときに行う

    def on_project_read(self):
        if self.isVisible():
            self.restore_project_workspace()
        else:
            self._restore_pending = True

    def on_project_cleared(self):
        """新規・閉じる：作業フォルダなし（新しいプロジェクトには記録が無い）"""
        self._restore_pending = False
        self.use_no_workspace(persist=False)

    def on_project_path_changed(self):
        """別名で保存などで保存先が変わったとき：作業フォルダの場所の表示を合わせ直す"""
        self._after_workspace_changed()

    # ── 選択欄の候補・ボタンの状態を合わせ直す場面 ──────────────────────────
    # 各タブの選択欄の候補（Morizon Next の出力）が変わるのは、Morizon Next 自身が出力を作った・外したときと、
    # 作業フォルダが変わったときだけ。利用者が見るのは画面を開いた・タブを切り替えたとき。
    # そのときだけ、まとめて1回（イベント処理が一巡した後に）合わせ直す。
    # プロジェクト全体のレイヤーの増減は見張らない。他のプラグインの操作に反応して動く必要は無く、
    # レイヤーの追加・削除の途中で選択欄に触れると QGIS ごと落ちた（raster_loader・FOL の操作で確認）。
    # 選択欄の中身が変わったときのボタン等の更新は、各タブが選択欄ごとにつないでいる（候補は作り直さない）

    def _init_layer_watch(self):
        self._refresh_pending = False
        self._refresh_timer = QTimer(self)
        self._refresh_timer.setSingleShot(True)
        self._refresh_timer.setInterval(100)
        self._refresh_timer.timeout.connect(self.refresh_all_tabs)
        self.tabWidget.currentChanged.connect(lambda _index: self.schedule_refresh_all_tabs())
        utils.set_outputs_changed_callback(self.schedule_refresh_all_tabs)

    def stop_layer_watch(self):
        """プラグインを外すときに呼ぶ（この画面は破棄されるので、知らせの受け先から外す）"""
        self._refresh_timer.stop()
        utils.set_outputs_changed_callback(None)

    def schedule_refresh_all_tabs(self):
        if self.isVisible():
            self._refresh_timer.start()
        else:
            self._refresh_pending = True

    def refresh_all_tabs(self):
        self._refresh_pending = False
        self.scoring.update_scoring_layer_scope()
        self.zoning.update_zoning_layer_scope()
        self.aggregate.update_aggregate_layer_scope()
        self.printlayout.update_printlayout_layer_scope()
        self.elements.refresh_elements_ui()
        self.scoring.refresh_scoring_ui()
        self.zoning.refresh_zoning_ui()
        self.aggregate.refresh_aggregate_ui()
        self.printlayout.refresh_create_zoning_printlayout_ui()
        self.printlayout.refresh_create_aggregate_printlayout_ui()

    def showEvent(self, event):
        super().showEvent(event)
        # 閉じている間の変化（他のプラグインが足したレイヤーを含む）を、開いたときにまとめて合わせ直す
        self._refresh_timer.start()
        if not getattr(self, "_tmp_tidied", False):
            # QGIS を起動して最初に開いたときだけ、前回までの一時ファイルを片付ける
            self._tmp_tidied = True
            QTimer.singleShot(0, self._tidy_tmp_files)
        if self._restore_pending:
            self._restore_pending = False
            QTimer.singleShot(0, self.restore_project_workspace)

    def _tidy_tmp_files(self):
        """計算に使った半角の置き場所の一時ファイルを片付ける（utils.tidy_ascii_safe_tmp_for_session）。
        新しいものが残っているときだけ、消してよいか聞く。画面を最初に開いたときと、作業フォルダが切り替わったとき
        （別のドライブなら、そのドライブの置き場所）に呼ぶ"""
        def confirm(root):
            return QMessageBox.question(
                self, "一時ファイルの片付け",
                f"前回の計算で使った一時ファイルが残っています（{root}）。\n"
                "ほかの QGIS で Morizon Next の計算をしていなければ、消してかまいません。消しますか？",
                QMessageBox.StandardButton.Yes, QMessageBox.StandardButton.No,
            ) == QMessageBox.StandardButton.Yes

        try:
            utils.tidy_ascii_safe_tmp_for_session(confirm)
        except OSError as e:
            QgsMessageLog.logMessage(f"一時ファイルを片付けられませんでした（{e}）", "Morizon Next", Qgis.MessageLevel.Warning)

    def _internal_has_data(self) -> bool:
        managed_dir = utils.get_morizon_managed_dir()
        return any(
            any(files for _, _, files in os.walk(os.path.join(managed_dir, name)))
            for name in (DIR_DATA, DIR_YOUSO, DIR_ZONING, DIR_AGGREGATE)
        )

    def _ask_resume(self, place: str) -> bool:
        answer = QMessageBox.question(
            self, "再開",
            f"このプロジェクトは、前回 {place} で作業していました。再開しますか？\n"
            "「いいえ」を選ぶと、レイヤーを初期化して初期状態（作業フォルダ：なし）にします。",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.Yes,
        )
        return answer == QMessageBox.StandardButton.Yes

    def _resume_from_internal(self):
        """プロジェクト内のデータから再開する（保存ファイルを読み込む → プロジェクト内のデータを読み直す と同じ）"""
        self.initialize_layers(confirm=False)
        self.archive.load_managed_data()

    def _resume_from_external(self, root):
        """外部のフォルダのデータから再開する（プロジェクトに保存されていたレイヤーは開いた時点で取り除かれているので、
        プロジェクト内からの再開と同じく、作業フォルダのデータからレイヤーを作り直す）"""
        self.initialize_layers(confirm=False)
        self.archive.load_workspace_data(root)

    def _reset_to_initial(self):
        """初期状態にする（レイヤーを初期化し、作業フォルダなし。プロジェクトの記録も消す）"""
        self.initialize_layers(confirm=False)
        self.use_no_workspace(persist=True)

    def restore_project_workspace(self):
        record = utils.read_project_workspace()
        if record is None or not QgsProject.instance().homePath():
            self.use_no_workspace(persist=False)
            return
        if record == utils.WORKSPACE_INTERNAL:
            if not self._internal_has_data():
                # 記録はあるがデータが無い → 記録なしとして扱う
                self._reset_to_initial()
            elif self._ask_resume(f"プロジェクト内（morizon_next/{os.path.basename(utils.get_morizon_managed_dir())}）"):
                self._resume_from_internal()
            else:
                self._reset_to_initial()
            return
        if os.path.isdir(record):
            if self._ask_resume(f"外部のフォルダ（{os.path.basename(record) or record}）"):
                self._resume_from_external(record)
            else:
                self._reset_to_initial()
            return
        if self._internal_has_data():
            QMessageBox.information(
                self, "再開",
                f"保存されていた外部のフォルダが見つかりません。\n{record}\n\nプロジェクト内のデータから再開します。",
            )
            self._resume_from_internal()
            return
        QMessageBox.information(
            self, "再開",
            "保存されていた記録の外部フォルダが見つかりません。削除または移動などが行われた可能性があります。\n"
            f"{record}\n\nクリアします。再開はフォルダから読み込んでください。",
        )
        self._reset_to_initial()

    def _after_workspace_changed(self):
        self.apply_workspace_output_dirs()
        self.update_workspace_status()
        self.update_input_availability()
        # 候補が変わるので、選択欄の候補とボタンや注意書きの状態をまとめて合わせ直す
        # （プロジェクトの合図からも呼ばれるため、その場では選択欄に触れない）
        self.schedule_refresh_all_tabs()

    # ── 操作マニュアル（プラグインに同梱の docs/manual.html を既定のブラウザで開く） ──
    MANUAL_PATH = os.path.join(os.path.dirname(__file__), "docs", "manual.html")

    def open_manual(self):
        if not os.path.isfile(self.MANUAL_PATH):
            QMessageBox.information(self, "マニュアル", f"マニュアルが見つかりません。\n{self.MANUAL_PATH}")
            return
        if not QDesktopServices.openUrl(QUrl.fromLocalFile(self.MANUAL_PATH)):
            QMessageBox.information(
                self, "マニュアル", f"マニュアルを開けませんでした。次のファイルを直接開いてください。\n{self.MANUAL_PATH}"
            )

    # 作業フォルダの表示。作業フォルダの中に出力先が決まる4つのタブ（要素計算・スコアリング・ゾーニング・
    # ゾーン統計量）で、「出力先フォルダ」の行のすぐ上に出す（印刷・設定は作業フォルダに関わらないので出さない）
    # 行の右寄せに「作業フォルダを開く」（作業フォルダをファイルマネージャーで開く）を置く
    def _init_workspace_status(self):
        self._workspace_status_labels = []
        self._workspace_open_buttons = []
        for output_label in self._output_dir_labels:
            row = QWidget()
            row_layout = QHBoxLayout(row)
            row_layout.setContentsMargins(0, 0, 0, 0)
            status = QLabel()
            status.setStyleSheet("color:#555;")
            open_button = QPushButton("作業フォルダを開く")
            open_button.setToolTip("作業フォルダをファイルマネージャーで開きます")
            open_button.clicked.connect(self.open_workspace_folder)
            row_layout.addWidget(status)
            row_layout.addStretch(1)
            row_layout.addWidget(open_button)
            if output_label is self._output_dir_labels[0]:
                # 要素計算タブだけ、「作業フォルダを開く」の左に「道路と建物を出力」（入力の道路縁・建築物をレイヤーに出す）
                road_building_button = QPushButton("道路と建物を出力")
                road_building_button.setToolTip(
                    "取得・読み込みした道路縁と建築物を、「Morizon Next」グループの「災害リスク」の下に表示します"
                )
                road_building_button.clicked.connect(self.elements.show_road_building_layers)
                row_layout.insertWidget(row_layout.indexOf(open_button), road_building_button)
            if self._insert_above_row(output_label, row):
                self._workspace_status_labels.append(status)
                self._workspace_open_buttons.append(open_button)

    def open_workspace_folder(self):
        folder = utils.get_workspace_dir() if self.has_workspace() else ""
        if not folder or not os.path.isdir(folder):
            QMessageBox.information(self, "作業フォルダを開く", "作業フォルダがありません。")
            return
        if not QDesktopServices.openUrl(QUrl.fromLocalFile(folder)):
            QMessageBox.information(
                self, "作業フォルダを開く", f"作業フォルダを開けませんでした。次のフォルダを直接開いてください。\n{folder}"
            )

    def _insert_above_row(self, widget_in_row, new_widget) -> bool:
        """widget_in_row がある行のすぐ上に new_widget を置く"""
        parent = widget_in_row.parentWidget()
        root = parent.layout() if parent is not None else None
        row = self._layout_containing(root, widget_in_row) if root is not None else None
        container = self._layout_parent(root, row) if row is not None else None
        if container is None:
            return False
        index = container.indexOf(row)
        if isinstance(container, QGridLayout):
            # グリッドの1マスには1つしか置けないので、その行を「表示＋行」の縦の組に置き換える
            position = container.getItemPosition(index)
            container.removeItem(row)
            row.setParent(None)
            box = QVBoxLayout()
            box.setContentsMargins(0, 0, 0, 0)
            box.addWidget(new_widget)
            box.addLayout(row)
            container.addLayout(box, *position)
        else:
            container.insertWidget(index, new_widget)
        return True

    @classmethod
    def _layout_parent(cls, layout, target):
        """layout とその中のレイアウトから、target（レイアウト）を直接持つレイアウトを探す"""
        for index in range(layout.count()):
            child = layout.itemAt(index).layout()
            if child is None:
                continue
            if child is target:
                return layout
            found = cls._layout_parent(child, target)
            if found is not None:
                return found
        return None

    def update_workspace_status(self):
        external = utils.get_external_workspace()
        if external:
            text = f"作業フォルダ：外部 {os.path.basename(external) or external}"
            tooltip = external
        elif self.has_workspace():
            text = "作業フォルダ：プロジェクト内"
            tooltip = utils.get_morizon_managed_dir()
        else:
            text = "作業フォルダ：なし"
            tooltip = ("「DEMブラウザから開始する」「フォルダ選択から開始する」「保存ファイルを読み込む」で始めます"
                       if QgsProject.instance().homePath() else
                       "QGISプロジェクトが未保存です。保存してから始めてください")
        for status in self._workspace_status_labels:
            status.setText(text)
            status.setToolTip(tooltip)
        # 作業フォルダが無いときは開けない
        folder = utils.get_workspace_dir() if self.has_workspace() else ""
        for open_button in self._workspace_open_buttons:
            open_button.setEnabled(bool(folder) and os.path.isdir(folder))
