try:
    # unittest時にQGIS-APIが読めなくてエラーになるのを避ける
    from qgis.PyQt.QtCore import *
    from qgis.PyQt.QtGui import *
    from qgis.PyQt.QtWidgets import *
    from qgis.core import *
    from qgis.gui import *
    import os

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
    from ..constants import OUTPUT_AGGREGATE, ZONING_COLORS
except Exception as e:
    print(e)



