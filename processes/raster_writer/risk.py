import os

from PyQt5.QtCore import *
from PyQt5.QtGui import *
from PyQt5.QtWidgets import *
from qgis.core import *
from qgis.gui import *
from qgis.analysis import QgsRasterCalculator, QgsRasterCalculatorEntry

from ...settings_manager import SettingsManager
from ...constants import OUTPUT_RISK


def generate(slope_rlayer: QgsRasterLayer,
             slope_thresholds: list,
             shc_rlayer: QgsRasterLayer,
             shc_thresholds: list,
             savearea_rlayer: QgsRasterLayer,
             output_dir: str) -> str:
    """
    災害リスクラスターを生成する

    Returns:
        str: [description]
    """
    # ラスター計算のためにEntry生成
    slope_entry = QgsRasterCalculatorEntry()
    slope_entry.ref = "slope@1"
    slope_entry.raster = slope_rlayer
    slope_entry.bandNumber = 1

    shc_entry = QgsRasterCalculatorEntry()
    shc_entry.ref = "shc@1"
    shc_entry.raster = shc_rlayer
    shc_entry.bandNumber = 1

    savearea_entry = QgsRasterCalculatorEntry()
    savearea_entry.ref = "savearea@1"
    savearea_entry.raster = savearea_rlayer
    savearea_entry.bandNumber = 1

    output_filepath = os.path.join(output_dir,
                                   OUTPUT_RISK["FILE_NAME"] + ".tif")

    expression = _make_expression(slope_entry.ref, slope_thresholds,
                                  shc_entry.ref, shc_thresholds,
                                  savearea_entry.ref)

    calc = QgsRasterCalculator(expression,
                               output_filepath,
                               "GTiff",
                               slope_rlayer.extent(),
                               slope_rlayer.width(),
                               slope_rlayer.height(),
                               (slope_entry, shc_entry, savearea_entry))
    calc.processCalculation()

    return output_filepath


def _make_expression(slope_name: str, slope_thresholds: tuple,
                     shc_name: str, shc_thresholds: tuple,
                     savearea_name: str) -> str:
    """
    災害リスクをラスター計算するためのExpressionを生成する

    採点ルール
    score1 => value <= threshold1
    score2 => threshold1 < value <= threshold2
    score3 => threshold2 < value
    saveareだけ例外、valueが二値なので value = 0 => score1, value = 1 => score2
    """

    # 区分ごとのスコアを設定値から取得
    smanager = SettingsManager()
    settings = smanager.get_settings()
    slope_scores = settings.get('scores_slope')
    shc_scores = settings.get('scores_shc')
    savearea_scores = settings.get('scores_savearea')

    expression = f"""
{slope_scores[0]} * ({slope_name} <= {slope_thresholds[0]})
 + {slope_scores[1]} * ({slope_thresholds[0]} < {slope_name} AND {slope_name} <= {slope_thresholds[1]})
 + {slope_scores[2]} * ({slope_thresholds[1]} < {slope_name})
 + {shc_scores[0]} * ({shc_name} <= {shc_thresholds[0]})
 + {shc_scores[1]} * ({shc_thresholds[0]} < {shc_name} AND {shc_name} <= {shc_thresholds[1]})
 + {shc_scores[2]} * ({shc_thresholds[1]} < {shc_name})
 + {savearea_scores[0]} * ({savearea_name} = 0)
 + {savearea_scores[1]} * ({savearea_name} = 1)
    """

    return expression
