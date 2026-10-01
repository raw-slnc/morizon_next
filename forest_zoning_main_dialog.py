# This file is part of MORIZON NEXT, modified from the original MORIZON (v2.1).
# Modified by Hideharu Masai since 2026-09-29. See the Git history for the changes and their dates.
# Licensed under the GNU General Public License v3. See LICENSE and NOTICE.

import os

from qgis.PyQt import uic
from contextlib import contextmanager

from qgis.PyQt.QtCore import QEvent, Qt
from qgis.PyQt.QtGui import QIcon
from qgis.PyQt.QtWidgets import QDialog, QLabel, QMessageBox
from qgis.core import QgsProject
from qgis.utils import iface

from .forest_zoning_main_dialog_elements import ForestZoningMainDialogElements
from .forest_zoning_main_dialog_scoring import ForestZoningMainDialogScoring
from .forest_zoning_main_dialog_zoning import ForestZoningMainDialogZoning
from .forest_zoning_main_dialog_aggregate import ForestZoningMainDialogAggregate
from .forest_zoning_main_dialog_printlayout import ForestZoningMainDialogPrintlayout
from .forest_zoning_main_dialog_settings import ForestZoningMainDialogSettings
from .forest_zoning_main_dialog_archive import ForestZoningMainDialogArchive
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

        # 各タブのUIを初期化する：実装は各クラスへ移譲
        self.elements = ForestZoningMainDialogElements(self)
        self.scoring = ForestZoningMainDialogScoring(self)
        self.zoning = ForestZoningMainDialogZoning(self)
        self.aggregate = ForestZoningMainDialogAggregate(self)
        self.printlayout = ForestZoningMainDialogPrintlayout(self)
        self.settings = ForestZoningMainDialogSettings(self.settings_widget, self)
        self.archive = ForestZoningMainDialogArchive(self)
        self._input_import_suppressed = 0
        self._missing_workspace_notified = None
        self.lock_output_dirs()
        self._init_workspace_status()
        self.restore_project_workspace()

    # ── 作業場 ───────────────────────────────────────────────────────
    # 作業場は入力（DATA/）と出力（YOUSO/・ZONING/・AGGREGATE/）を置く場所で、2種類ある。
    #   プロジェクト内：<プロジェクト>/morizon_next（通常。DEMブラウザ・保存ファイルの読み込み・設定のクリア）
    #   外部：「フォルダ選択から開始する」で選んだフォルダ（その場で使う。原版のキットと同じ使い方）
    # 作業場はプロジェクトに書き込み、プロジェクトを保存して開き直せば同じ作業場で続けられる。
    # 出力先は作業場の中に決まり、ユーザーは変更できない

    def output_targets(self):
        return (
            (self.elementsOutputDirFileWidget, (DIR_YOUSO,)),
            (self.scoringOutputDirFileWidget, (DIR_ZONING,)),
            (self.zoningOutputDirFileWidget, (DIR_ZONING,)),
            (self.aggregateOutputDirFileWidget, (DIR_AGGREGATE, OUTPUT_AGGREGATE["FILE_NAME"] + ".shp")),
        )

    def has_workspace(self) -> bool:
        """作業場が決まっているか（プロジェクト内の作業場は、プロジェクトが保存済みのときだけ）"""
        return bool(utils.get_external_workspace()) or QgsProject.instance().homePath() != ""

    def apply_workspace_output_dirs(self):
        """出力先の欄を、今の作業場の中の決まった場所にする（作業場が無ければ空欄）"""
        for filewidget, parts in self.output_targets():
            if not self.has_workspace():
                filewidget.setFilePath("")
                continue
            path = utils.get_workspace_dir(*parts)
            os.makedirs(path if len(parts) == 1 else os.path.dirname(path), exist_ok=True)
            filewidget.setFilePath(path)

    def lock_output_dirs(self):
        for filewidget, _ in self.output_targets():
            filewidget.setReadOnly(True)
            filewidget.setToolTip(
                "出力先は作業場の中に決まります（プロジェクト内なら QGISプロジェクトと同じフォルダの morizon_next）"
            )

    @contextmanager
    def suppress_input_import(self):
        """プラグインが入力欄を設定する間は、「…」で選んだときの取り込み（作業場へのコピー）をしない"""
        self._input_import_suppressed += 1
        try:
            yield
        finally:
            self._input_import_suppressed -= 1

    def is_input_import_suppressed(self) -> bool:
        return self._input_import_suppressed > 0

    def set_inputs_from_data_dir(self, data_dir):
        """作業場の DATA にあるデータで入力欄を埋める（無い入力は空欄）"""
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
        """作業場をプロジェクト内（<プロジェクト>/morizon_next）にする。
        refill_inputs なら入力欄を morizon_next/DATA のデータで埋め直す（外部の作業場やほかのプロジェクトの
        データを指したままにしない）。persist ならプロジェクトにも書き込む"""
        utils.set_external_workspace(None)
        if persist:
            utils.write_project_workspace(None)
        if refill_inputs:
            project_saved = QgsProject.instance().homePath() != ""
            self.set_inputs_from_data_dir(
                utils.get_morizon_managed_dir(DIR_DATA) if project_saved else None
            )
        self._after_workspace_changed()

    def use_external_workspace(self, root):
        """作業場を外部のフォルダにする（入力欄は呼び出し側が設定する）"""
        utils.set_external_workspace(root)
        utils.write_project_workspace(root)
        self._after_workspace_changed()

    # 初期状態にする操作（DEMブラウザから開始・フォルダ選択から開始・設定をクリア・保存ファイルの読み込み）は、
    # ボタンを押した（読み込みはメニューで選んだ）時点でレイヤーを初期化してから、それぞれの工程に入る。
    # 初期状態にはプラグインのレイヤーは無いので、指しているファイルの場所に関係なくすべて外す（ファイルは残す）。
    # データを消す・上書きするかは、それぞれの工程の中で別に判断する。
    # プロジェクトを開いたときの作業場の復元では外さない（そのプロジェクトのレイヤーなので）

    def initialize_layers(self) -> bool:
        """「レイヤーを初期化します」と確かめて、プラグインのレイヤーをすべてプロジェクトから外す。
        取りやめたら False（呼び出し側は何もせずに戻る）。外すレイヤーが無ければ確かめずに True"""
        layers = utils.output_layers()
        if self.has_workspace():
            # 作業場のフォルダのファイルを、自分で追加したレイヤーも外す（共有キャッシュは除く）
            current = utils.get_workspace_dir()
            ids = {layer.id() for layer in layers}
            layers += [layer for layer in utils.project_layers_under_dir(current, [os.path.join(current, DIR_SHARED)])
                       if layer.id() not in ids]
        if not layers:
            return True
        answer = QMessageBox.question(
            self, "レイヤーの初期化",
            f"レイヤーを初期化します。\nMorizon Next のレイヤー {len(layers)}件をプロジェクトから外します（ファイルは残ります）。",
            QMessageBox.StandardButton.Ok | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Ok,
        )
        if answer != QMessageBox.StandardButton.Ok:
            return False
        utils.remove_project_layers(layers)
        return True

    def restore_project_workspace(self):
        """プロジェクトに書き込まれた作業場に戻す（プロジェクトを開いた・切り替えたとき）。
        外部のフォルダが見つからなければプロジェクト内にして知らせる。プロジェクトの記録は書き換えない
        （外付けのドライブをつなぎ忘れただけなら、つないで開き直せば戻れるように）"""
        root = utils.read_project_workspace()
        if root and os.path.isdir(root):
            self._missing_workspace_notified = None
            self.use_external_workspace(root)
            self.set_inputs_from_data_dir(morizon_data.resolve_data_dir(root))
            return
        self.use_project_workspace(refill_inputs=True, persist=False)
        # プロジェクトを開くと切り替えの通知が続けて届くため、同じフォルダについては1回だけ知らせる。
        # 読み込みの最初（記録がまだ無い時点）で忘れるので、開き直せばまた知らせる
        if not root:
            self._missing_workspace_notified = None
        elif self._missing_workspace_notified != root:
            self._missing_workspace_notified = root
            iface.messageBar().pushWarning(
                "Morizon Next",
                f"作業場（外部 {os.path.basename(root) or root}）が見つからないため、プロジェクト内にしました：{root}",
            )

    def _after_workspace_changed(self):
        self.apply_workspace_output_dirs()
        self.update_workspace_status()
        self.scoring.update_scoring_layer_scope()
        self.zoning.update_zoning_layer_scope()
        self.aggregate.update_aggregate_layer_scope()
        self.printlayout.update_printlayout_layer_scope()

    def on_project_changed(self):
        """プロジェクトが切り替わったら、作業場をそのプロジェクトに書き込まれたものにする（無ければ morizon_next）。
        ダイアログは使い回すので、何もしないと前のプロジェクト（や外部のフォルダ）を指したまま計算してしまう"""
        self.restore_project_workspace()

    # 作業場の表示（タブ列の「設定」の右隣）
    def _init_workspace_status(self):
        self.workspaceStatusLabel = QLabel(self.tabWidget)
        self.workspaceStatusLabel.setStyleSheet("color:#555;")
        self.tabWidget.installEventFilter(self)
        self.tabWidget.tabBar().installEventFilter(self)

    def update_workspace_status(self):
        external = utils.get_external_workspace()
        if external:
            text = f"作業場：外部 {os.path.basename(external) or external}"
            tooltip = external
        elif QgsProject.instance().homePath():
            text = "作業場：プロジェクト内"
            tooltip = utils.get_morizon_managed_dir()
        else:
            text = "作業場：なし（QGISプロジェクトが未保存）"
            tooltip = "QGISプロジェクトを保存すると、プロジェクト内の morizon_next が作業場になります"
        self.workspaceStatusLabel.setText(text)
        self.workspaceStatusLabel.setToolTip(tooltip)
        self.workspaceStatusLabel.adjustSize()
        self._place_workspace_status()

    def _place_workspace_status(self):
        bar = self.tabWidget.tabBar()
        if bar.count() == 0:
            return
        last = bar.tabRect(bar.count() - 1)
        top_left = bar.mapTo(self.tabWidget, last.topRight())
        label = self.workspaceStatusLabel
        label.move(top_left.x() + 12, top_left.y() + (last.height() - label.height()) // 2)
        label.raise_()

    def eventFilter(self, obj, event):
        if event.type() in (QEvent.Type.Resize, QEvent.Type.Show, QEvent.Type.LayoutRequest):
            if hasattr(self, "workspaceStatusLabel"):
                self._place_workspace_status()
        return super().eventFilter(obj, event)
