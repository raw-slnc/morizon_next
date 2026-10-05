# This file is part of MORIZON NEXT, modified from the original MORIZON (v2.1).
# Modified by Hideharu Masai since 2026-09-29. See the Git history for the changes and their dates.
# Licensed under the GNU General Public License v3. See LICENSE and NOTICE.

from . import create_printlayout

# 外から printlayout.create_printlayout で使う部品（読み込むこと自体が目的）
__all__ = ["create_printlayout"]
