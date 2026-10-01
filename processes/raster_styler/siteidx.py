# This file is part of MORIZON NEXT, modified from the original MORIZON (v2.1).
# Modified by Hideharu Masai since 2026-09-29. See the Git history for the changes and their dates.
# Licensed under the GNU General Public License v3. See LICENSE and NOTICE.

import tempfile
import xml.etree.ElementTree as ET

from qgis.PyQt.QtCore import *
from qgis.PyQt.QtGui import *
from qgis.PyQt.QtWidgets import *
from qgis.core import *
from qgis.gui import *

from ...constants import (
    SCORING_COLORS_SITEIDX,
    RAWDATA_COLORS_SITEIDX_SUGI,
    RAWDATA_COLORS_SITEIDX_HINOKI,
    RAWDATA_COLORS_SITEIDX_KARAMATSU
)

from .utils import (
    apply_output_blend_mode,
    get_quantile_renderer,
    hex_to_rgb,
    replace_colorramp_labels,
    get_colorramp_label_prefixes,
    round_label_precision
)


def write_rawdata_qml(siteidx_filepath: str, wood_type="sugi") -> str:
    """
    地位指数・生データスタイル生成

    Args:
        siteidx_filepath (str)
        wood_type (str, optional): "sugi" or "hinoki" or "karamatsu". Defaults to "sugi".

    Raises:
        ValueError: when wood_type is not "sugi" or "hinoki" or "karamatsu"

    Returns:
        str: qml_filepath
    """
    colors_dict = {
        "sugi": RAWDATA_COLORS_SITEIDX_SUGI,
        "hinoki": RAWDATA_COLORS_SITEIDX_HINOKI,
        "karamatsu": RAWDATA_COLORS_SITEIDX_KARAMATSU
    }
    if colors_dict.get(wood_type) is None:
        raise ValueError

    colors = list(map(hex_to_rgb, colors_dict[wood_type]))

    rlayer = QgsRasterLayer(siteidx_filepath, '')
    renderer = get_quantile_renderer(rlayer, colors)
    renderer.setOpacity(0.65)
    rlayer.setRenderer(renderer)
    apply_output_blend_mode(rlayer)
    rlayer.setContrastEnhancement(QgsContrastEnhancement.ContrastEnhancementAlgorithm.StretchToMinimumMaximum,
                                  QgsRasterMinMaxOrigin.Limits.MinMax)

    with tempfile.NamedTemporaryFile(delete=False, suffix='.qml') as temp_qml:
        rlayer.saveNamedStyle(temp_qml.name)
        output_filepath = round_label_precision(temp_qml.name,
                                                siteidx_filepath.replace('.tif', '_raw.qml'))
    return output_filepath


def write_scoring_qml(siteidx_filepath: str) -> str:
    rlayer = QgsRasterLayer(siteidx_filepath, '')
    colors = list(map(hex_to_rgb, SCORING_COLORS_SITEIDX))
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
                                  temp_qml_value_rounded.name)
            output_filepath = replace_colorramp_labels(temp_qml_value_rounded.name,
                                                       siteidx_filepath.replace(
                                                           '.tif', '_score.qml'),
                                                       labels=get_colorramp_label_prefixes("siteidx"))

    return output_filepath
