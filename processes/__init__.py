# This file is part of MORIZON NEXT, modified from the original MORIZON (v2.1).
# Modified by Hideharu Masai since 2026-09-29. See the Git history for the changes and their dates.
# Licensed under the GNU General Public License v3. See LICENSE and NOTICE.

# 読み込みに失敗したらそのまま例外にする（原版は try/except で print するだけで先へ進んでいたため、
# 処理が丸ごと無いままプラグインが起動してしまい、失敗に気づけなかった）
from . import aggregate
from . import elements
from . import scoring
from . import zoning
from . import raster_writer
from . import raster_styler
from . import printlayout
from . import dem_fetch
from . import siteindex_fetch
from . import building_road_fetch
from . import data_import
from . import data_archive
from . import data_restore

# 外から processes.<名前> で使う部品（読み込むこと自体が目的）
__all__ = [
    "aggregate",
    "elements",
    "scoring",
    "zoning",
    "raster_writer",
    "raster_styler",
    "printlayout",
    "dem_fetch",
    "siteindex_fetch",
    "building_road_fetch",
    "data_import",
    "data_archive",
    "data_restore",
]
