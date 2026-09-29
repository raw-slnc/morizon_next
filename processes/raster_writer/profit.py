import os

from PyQt5.QtCore import *
from PyQt5.QtGui import *
from PyQt5.QtWidgets import *
from qgis.core import *
from qgis.gui import *
from qgis.analysis import QgsRasterCalculator, QgsRasterCalculatorEntry

from ...settings_manager import SettingsManager
from ...constants import OUTPUT_PROFIT


def generate(siteidx_rlayer: QgsRasterLayer,
             siteidx_thresholds: list,
             cost_rlayer: QgsRasterLayer,
             cost_thresholds: list,
             distance_rlayer: QgsRasterLayer,
             distance_thresholds: list,
             output_dir: str) -> str:
    """
    収益性ラスターを生成する

    Returns:
        str: 出力ファイルパス
    """

    # ラスター計算のためにEntry生成
    siteidx_entry = QgsRasterCalculatorEntry()
    siteidx_entry.ref = "siteidx@1"
    siteidx_entry.raster = siteidx_rlayer
    siteidx_entry.bandNumber = 1

    cost_entry = QgsRasterCalculatorEntry()
    cost_entry.ref = "cost@1"
    cost_entry.raster = cost_rlayer
    cost_entry.bandNumber = 1

    distance_entry = QgsRasterCalculatorEntry()
    distance_entry.ref = "distance@1"
    distance_entry.raster = distance_rlayer
    distance_entry.bandNumber = 1

    output_filepath = os.path.join(output_dir,
                                   OUTPUT_PROFIT["FILE_NAME"] + ".tif")

    expression = _make_expression(siteidx_entry.ref, siteidx_thresholds,
                                  cost_entry.ref, cost_thresholds,
                                  distance_entry.ref, distance_thresholds)

    calc = QgsRasterCalculator(expression,
                               output_filepath,
                               "GTiff",
                               siteidx_rlayer.extent(),
                               siteidx_rlayer.width(),
                               siteidx_rlayer.height(),
                               (siteidx_entry, cost_entry, distance_entry))
    calc.processCalculation()

    return output_filepath


def _make_expression(siteidx_name: str, siteidx_thresholds: tuple,
                     cost_name: str, cost_thresholds: tuple,
                     distance_name: str, distance_thresholds: tuple) -> str:
    """
    収益性をラスター計算するためのExpressionを生成する

    採点ルール
    score1 => value <= threshold1
    score2 => threshold1 < value <= threshold2
    score3 => threshold2 < value
    """

    # 区分ごとのスコアを設定値から取得
    smanager = SettingsManager()
    settings = smanager.get_settings()
    siteidx_scores = settings.get('scores_siteidx')
    cost_scores = settings.get('scores_cost')
    distance_scores = settings.get('scores_distance')

    expression = f"""
{siteidx_scores[0]} * ({siteidx_name} <= {siteidx_thresholds[0]})
 + {siteidx_scores[1]} * ({siteidx_thresholds[0]} < {siteidx_name} AND {siteidx_name} <= {siteidx_thresholds[1]})
 + {siteidx_scores[2]} * ({siteidx_thresholds[1]} < {siteidx_name})
 + {cost_scores[0]} * ({cost_name} <= {cost_thresholds[0]})
 + {cost_scores[1]} * ({cost_thresholds[0]} < {cost_name} AND {cost_name} <= {cost_thresholds[1]})
 + {cost_scores[2]} * ({cost_thresholds[1]} < {cost_name})
 + {distance_scores[0]} * ({distance_name} <= {distance_thresholds[0]})
 + {distance_scores[1]} * ({distance_thresholds[0]} < {distance_name} AND {distance_name} <= {distance_thresholds[1]})
 + {distance_scores[2]} * ({distance_thresholds[1]} < {distance_name})
    """

    return expression
