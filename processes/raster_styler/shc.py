# This file is part of MORIZON NEXT, modified from the original MORIZON (v2.1).
# Modified by Hideharu Masai since 2026-09-29. See the Git history for the changes and their dates.
# Licensed under the GNU General Public License v3. See LICENSE and NOTICE.

import os
import tempfile

from qgis.core import QgsContrastEnhancement, QgsRasterLayer, QgsRasterMinMaxOrigin

from ...constants import (
    OUTPUT_SHC,
    SCORING_COLORS_SHC,
    RAWDATA_COLORS_SHC
)
from .utils import (
    apply_output_blend_mode,
    get_quantile_renderer,
    hex_to_rgb,
    replace_colorramp_labels,
    get_colorramp_label_prefixes,
    round_label_precision
)


def write_rawdata_qml(shc_filepath: str, output_dir: str) -> str:
    rlayer = QgsRasterLayer(shc_filepath, '')
    colors = list(map(hex_to_rgb, RAWDATA_COLORS_SHC))
    renderer = get_quantile_renderer(rlayer, colors)
    renderer.setOpacity(0.65)
    rlayer.setRenderer(renderer)
    apply_output_blend_mode(rlayer)
    rlayer.setContrastEnhancement(QgsContrastEnhancement.ContrastEnhancementAlgorithm.StretchToMinimumMaximum,
                                  QgsRasterMinMaxOrigin.Limits.MinMax)

    with tempfile.NamedTemporaryFile(delete=False, suffix='.qml') as temp_qml:
        rlayer.saveNamedStyle(temp_qml.name)
        output_filepath = round_label_precision(temp_qml.name,
                                                os.path.join(
                                                    output_dir, OUTPUT_SHC["FILE_NAME"] + '_raw.qml'),
                                                precision=4)
    return output_filepath


def write_scoring_qml(shc_filepath: str, output_dir: str) -> str:
    rlayer = QgsRasterLayer(shc_filepath, '')
    colors = list(map(hex_to_rgb, SCORING_COLORS_SHC))
    renderer = get_quantile_renderer(rlayer, colors)
    renderer.setOpacity(0.65)
    rlayer.setRenderer(renderer)
    apply_output_blend_mode(rlayer)
    rlayer.setContrastEnhancement(QgsContrastEnhancement.ContrastEnhancementAlgorithm.StretchToMinimumMaximum,
                                  QgsRasterMinMaxOrigin.Limits.MinMax)

    # 等量区分QML -> ラベル置換 -> 桁丸目 -> 出力
    with tempfile.NamedTemporaryFile(delete=False, suffix='.qml') as temp_qml:
        rlayer.saveNamedStyle(temp_qml.name)
        with tempfile.NamedTemporaryFile(delete=False, suffix='.qml') as temp_qml_value_rounded:
            round_label_precision(temp_qml.name,
                                  temp_qml_value_rounded.name,
                                  precision=4)
            output_filepath = replace_colorramp_labels(temp_qml_value_rounded.name,
                                                       os.path.join(
                                                           output_dir, OUTPUT_SHC["FILE_NAME"] + '_score.qml'),
                                                       labels=get_colorramp_label_prefixes("shc"))
    return output_filepath
