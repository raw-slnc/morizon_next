import os

from qgis.PyQt import uic
from PyQt5.QtWidgets import QDialog

from .forest_zoning_main_dialog_elements import ForestZoningMainDialogElements
from .forest_zoning_main_dialog_scoring import ForestZoningMainDialogScoring
from .forest_zoning_main_dialog_zoning import ForestZoningMainDialogZoning
from .forest_zoning_main_dialog_aggregate import ForestZoningMainDialogAggregate
from .forest_zoning_main_dialog_printlayout import ForestZoningMainDialogPrintlayout


class ForestZoningMainDialog(QDialog):
    def __init__(self):
        super().__init__()
        self.ui = uic.loadUi(
            os.path.join(os.path.dirname(__file__), "forest_zoning_main_dialog.ui"),
            self,
        )

        # 各タブのUIを初期化する：実装は各クラスへ移譲
        self.elements = ForestZoningMainDialogElements(self)
        self.scoring = ForestZoningMainDialogScoring(self)
        self.zoning = ForestZoningMainDialogZoning(self)
        self.aggregate = ForestZoningMainDialogAggregate(self)
        self.printlayout = ForestZoningMainDialogPrintlayout(self)
