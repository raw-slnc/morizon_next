import os

from qgis.PyQt import uic
from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtGui import QIcon
from qgis.PyQt.QtWidgets import QDialog

from .forest_zoning_main_dialog_elements import ForestZoningMainDialogElements
from .forest_zoning_main_dialog_scoring import ForestZoningMainDialogScoring
from .forest_zoning_main_dialog_zoning import ForestZoningMainDialogZoning
from .forest_zoning_main_dialog_aggregate import ForestZoningMainDialogAggregate
from .forest_zoning_main_dialog_printlayout import ForestZoningMainDialogPrintlayout
from .forest_zoning_main_dialog_settings import ForestZoningMainDialogSettings


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
