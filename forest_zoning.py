import os

# QGIS-API
from qgis.PyQt.QtCore import *
from qgis.PyQt.QtGui import *
from qgis.PyQt.QtWidgets import *
from qgis.core import *
from qgis.gui import *

from .forest_zoning_main_dialog import ForestZoningMainDialog
from .forest_zoning_main_dialog_elements import ForestZoningMainDialogElements

PLUGIN_NAME = "MORIZON"


class ForestZoning:
    def __init__(self, iface):
        self.iface = iface
        self.win = self.iface.mainWindow()
        self.plugin_dir = os.path.dirname(__file__)
        self.actions = []
        self.menu = PLUGIN_NAME

        self.main_dialog = None
        self._syncing_output_layer_tree = False

    def add_action(
        self,
        icon_path,
        text,
        callback,
        enabled_flag=True,
        add_to_menu=True,
        add_to_toolbar=True,
        status_tip=None,
        whats_this=None,
        parent=None,
    ):
        icon = QIcon(icon_path)
        action = QAction(icon, text, parent)
        action.triggered.connect(callback)
        action.setEnabled(enabled_flag)
        if status_tip is not None:
            action.setStatusTip(status_tip)
        if whats_this is not None:
            action.setWhatsThis(whats_this)
        if add_to_toolbar:
            self.iface.addRasterToolBarIcon(action)
        if add_to_menu:
            self.iface.addPluginToRasterMenu(self.menu, action)
        self.actions.append(action)
        return action

    def initGui(self):
        icon_path = os.path.join(self.plugin_dir, "imgs", "icon.png")

        # メニュー設定
        self.add_action(
            icon_path=icon_path,
            text="ゾーニング",
            callback=self.show_main_dialog,
            parent=self.win,
        )

        QgsProject.instance().layerTreeRoot().addedChildren.connect(
            self.onLayersChanged
        )
        QgsProject.instance().layerTreeRoot().removedChildren.connect(
            self.onLayersChanged
        )
        QgsProject.instance().layerTreeRoot().visibilityChanged.connect(
            self.onLayersChanged
        )
        self.iface.layerTreeView().layerTreeModel().dataChanged.connect(
            self.onLayersChanged
        )  # nopep8
        self._setup_output_layer_tree_visibility()

    def unload(self):
        for action in self.actions:
            self.iface.removePluginRasterMenu(self.menu, action)
            self.iface.removeRasterToolBarIcon(action)

        self._safe_disconnect(
            QgsProject.instance().layerTreeRoot().addedChildren,
            self.onLayersChanged,
        )
        self._safe_disconnect(
            QgsProject.instance().layerTreeRoot().removedChildren,
            self.onLayersChanged,
        )
        self._safe_disconnect(
            QgsProject.instance().layerTreeRoot().visibilityChanged,
            self.onLayersChanged,
        )
        self._safe_disconnect(
            self.iface.layerTreeView().layerTreeModel().dataChanged,
            self.onLayersChanged,
        )
        if self.main_dialog is not None:
            self.main_dialog.close()
            self.main_dialog.deleteLater()
            self.main_dialog = None

    @staticmethod
    def _safe_disconnect(signal, slot):
        try:
            signal.disconnect(slot)
        except (TypeError, RuntimeError):
            pass

    def onLayersChanged(self, *args):
        self._setup_output_layer_tree_visibility()

        if not self.is_visible_main_dialog():
            return

        self.main_dialog.elements.refresh_elements_ui()
        self.main_dialog.scoring.refresh_scoring_ui()
        self.main_dialog.zoning.refresh_zoning_ui()
        self.main_dialog.aggregate.refresh_aggregate_ui()
        self.main_dialog.printlayout.refresh_create_zoning_printlayout_ui()
        self.main_dialog.printlayout.refresh_create_aggregate_printlayout_ui()

    def _setup_output_layer_tree_visibility(self):
        if self._syncing_output_layer_tree:
            return
        self._syncing_output_layer_tree = True
        try:
            ForestZoningMainDialogElements.setup_output_layer_tree_visibility()
        finally:
            self._syncing_output_layer_tree = False

    def show_main_dialog(self):
        self._setup_output_layer_tree_visibility()
        if self.main_dialog is None:
            self.main_dialog = ForestZoningMainDialog()
        self.main_dialog.show()
        self.main_dialog.raise_()
        self.main_dialog.activateWindow()

    def is_visible_main_dialog(self):
        if self.main_dialog is None:
            return False
        return self.main_dialog.isVisible()
