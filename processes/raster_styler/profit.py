import os
import tempfile

from qgis.PyQt.QtCore import *
from qgis.PyQt.QtGui import *
from qgis.PyQt.QtWidgets import *
from qgis.core import *
from qgis.gui import *

from ...constants import (
    OUTPUT_PROFIT,
    SCORING_COLORS_PROFIT
)
from .utils import (
    get_quantile_renderer,
    hex_to_rgb,
    replace_colorramp_labels,
    round_label_precision,
    add_tiny_value_to_thresholds
)


def write_qml(profit_filepath: str, output_dir: str) -> str:
    rlayer = QgsRasterLayer(profit_filepath, '')
    colors = list(map(hex_to_rgb, SCORING_COLORS_PROFIT))
    renderer = get_quantile_renderer(rlayer, colors)
    renderer.setOpacity(0.5)
    rlayer.setRenderer(renderer)
    rlayer.setContrastEnhancement(QgsContrastEnhancement.ContrastEnhancementAlgorithm.StretchToMinimumMaximum,
                                  QgsRasterMinMaxOrigin.Limits.MinMax)

    with tempfile.NamedTemporaryFile(delete=False, suffix='.qml') as temp_qml:
        rlayer.saveNamedStyle(temp_qml.name)
        with tempfile.NamedTemporaryFile(delete=False, suffix='.qml') as temp_qml_value_rounded:
            round_label_precision(temp_qml.name,
                                  temp_qml_value_rounded.name,
                                  precision=0)
            with tempfile.NamedTemporaryFile(delete=False, suffix='.qml') as temp_qml_label_replaced:
                replace_colorramp_labels(temp_qml_value_rounded.name,
                                         temp_qml_label_replaced.name,
                                         labels=["低", "高"])
                output_filepath = add_tiny_value_to_thresholds(temp_qml_label_replaced.name,
                                                               os.path.join(output_dir, OUTPUT_PROFIT["FILE_NAME"] + '.qml'))
    return output_filepath
