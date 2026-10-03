# This file is part of MORIZON NEXT, modified from the original MORIZON (v2.1).
# Modified by Hideharu Masai since 2026-09-29. See the Git history for the changes and their dates.
# Licensed under the GNU General Public License v3. See LICENSE and NOTICE.

import os
import tempfile

from qgis.core import QgsContrastEnhancement, QgsRasterLayer, QgsRasterMinMaxOrigin

from ...constants import (
    OUTPUT_RISK,
    SCORING_COLORS_RISK
)
from .utils import (
    apply_output_blend_mode,
    get_quantile_renderer,
    hex_to_rgb,
    replace_colorramp_labels,
    round_label_precision,
    add_tiny_value_to_thresholds
)


def write_qml(risk_filepath: str, output_dir: str) -> str:
    rlayer = QgsRasterLayer(risk_filepath, '')
    colors = list(map(hex_to_rgb, SCORING_COLORS_RISK))
    renderer = get_quantile_renderer(rlayer, colors)
    renderer.setOpacity(0.8)
    rlayer.setRenderer(renderer)
    apply_output_blend_mode(rlayer)
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
                output_filepath = add_tiny_value_to_thresholds(
                    temp_qml_label_replaced.name,
                    os.path.join(output_dir, OUTPUT_RISK["FILE_NAME"] + '.qml'),
                )
    return output_filepath
