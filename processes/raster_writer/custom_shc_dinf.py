# -*- coding: utf-8 -*-
# Adapted from Forestry Operations Lite terrain/analysis.py (GPL-2.0-or-later, same author):
# dinf_proportions and multi_flow_accumulation. numpy only.
"""D-infinity (Tarboton 1997) flow accumulation, used to find stream centre lines
for the optional contour-based terrain complexity.

Each cell sends its flow to the two neighbours around its steepest-descent
direction, in proportion to the angle, so the streams come out as thin lines.
"""

import numpy as np

_NB8 = [(-1, -1), (-1, 0), (-1, 1),
        (0, -1),           (0, 1),
        (1, -1),  (1, 0),  (1, 1)]
_PROGRESS_EVERY = 16384


def dinf_proportions(dem, cell_size):
    """Distribution rates (rows, cols, 8, in _NB8 order). Cells without a way down get all zeros."""
    rows, cols = dem.shape
    pad = np.pad(dem, 1, constant_values=np.nan)
    e0 = pad[1:-1, 1:-1]
    diag = cell_size * 1.4142135623730951
    ang_f = np.pi / 4.0
    facets = [
        ((0, 1), (-1, 1)), ((-1, 0), (-1, 1)), ((-1, 0), (-1, -1)), ((0, -1), (-1, -1)),
        ((0, -1), (1, -1)), ((1, 0), (1, -1)), ((1, 0), (1, 1)), ((0, 1), (1, 1)),
    ]
    idx_of = {off: i for i, off in enumerate(_NB8)}
    best_s = np.full((rows, cols), -np.inf)
    best_r = np.zeros((rows, cols))
    best_card = np.full((rows, cols), -1, dtype=np.int64)
    best_diag = np.full((rows, cols), -1, dtype=np.int64)
    for (cr, cc), (gr, gc) in facets:
        e1 = pad[1 + cr: rows + 1 + cr, 1 + cc: cols + 1 + cc]
        e2 = pad[1 + gr: rows + 1 + gr, 1 + gc: cols + 1 + gc]
        s1 = (e0 - e1) / cell_size
        s2 = (e1 - e2) / cell_size
        r = np.arctan2(s2, s1)
        s = np.hypot(s1, s2)
        neg = r < 0
        r = np.where(neg, 0.0, r)
        s = np.where(neg, s1, s)
        big = r > ang_f
        r = np.where(big, ang_f, r)
        s = np.where(big, (e0 - e2) / diag, s)
        take = (~np.isnan(e1)) & (~np.isnan(e2)) & (s > 0) & (s > best_s)
        best_s = np.where(take, s, best_s)
        best_r = np.where(take, r, best_r)
        best_card = np.where(take, idx_of[(cr, cc)], best_card)
        best_diag = np.where(take, idx_of[(gr, gc)], best_diag)
    prop = np.zeros((rows, cols, 8), dtype=np.float64)
    has = best_card >= 0
    rr, cc_ = np.where(has)
    frac_d = best_r[rr, cc_] / ang_f
    prop[rr, cc_, best_card[rr, cc_]] = 1.0 - frac_d
    prop[rr, cc_, best_diag[rr, cc_]] = frac_d
    prop[np.isnan(e0)] = 0.0
    return prop.astype(np.float32)


def flow_and_proportions(dem, cell_size, context=None):
    """Upstream cell counts by D-infinity (highest cells first) and the distribution rates.
    Stops through context.check()."""
    prop = dinf_proportions(dem, cell_size)
    rows, cols = dem.shape
    accum = np.ones((rows, cols), dtype=np.float64)
    valid = ~np.isnan(dem)
    r_arr, c_arr = np.where(valid)
    order = np.argsort(-dem[r_arr, c_arr])
    for i, idx in enumerate(order):
        if context is not None and not i % _PROGRESS_EVERY:
            context.check()
        r, c = int(r_arr[idx]), int(c_arr[idx])
        base = accum[r, c]
        for k, (dr, dc) in enumerate(_NB8):
            pk = prop[r, c, k]
            if pk > 0.0:
                nr, nc = r + dr, c + dc
                if 0 <= nr < rows and 0 <= nc < cols and valid[nr, nc]:
                    accum[nr, nc] += base * pk
    return accum, prop


def flow_accumulation(dem, cell_size, context=None):
    return flow_and_proportions(dem, cell_size, context)[0]


def basin_labels(dem, accum, prop, threshold, context=None):
    """Catchment label per cell (0 = none) from the D-infinity result.

    A stream is where the flow reaches `threshold` cells. It is cut into segments where
    streams meet (each cell's drainage direction is its neighbour with the largest share),
    and every cell belongs to the segment it reaches first along the drainage directions.
    Cells in a pit (no way down) get 0.
    """
    rows, cols = dem.shape
    valid = ~np.isnan(dem)
    best = np.argmax(prop, axis=2)
    has_down = prop.max(axis=2) > 0
    down = np.full((rows, cols, 2), -1, dtype=np.int32)
    offsets = np.array(_NB8, dtype=np.int32)
    rr, cc = np.where(valid & has_down)
    nr = rr + offsets[best[rr, cc], 0]
    nc = cc + offsets[best[rr, cc], 1]
    inside = (nr >= 0) & (nr < rows) & (nc >= 0) & (nc < cols)
    rr, cc, nr, nc = rr[inside], cc[inside], nr[inside], nc[inside]
    ok = valid[nr, nc]
    down[rr[ok], cc[ok], 0] = nr[ok]
    down[rr[ok], cc[ok], 1] = nc[ok]

    stream = valid & (accum >= threshold)
    inflow = np.zeros((rows, cols), dtype=np.int32)
    sr, sc = np.where(stream)
    for r, c in zip(sr.tolist(), sc.tolist()):
        dr_, dc_ = int(down[r, c, 0]), int(down[r, c, 1])
        if dr_ >= 0 and stream[dr_, dc_]:
            inflow[dr_, dc_] += 1

    labels = np.zeros((rows, cols), dtype=np.int32)
    vr, vc = np.where(valid)
    order = np.argsort(-dem[vr, vc])  # 高い所から
    next_id = 1
    for i, idx in enumerate(order):
        if context is not None and not i % _PROGRESS_EVERY:
            context.check()
        r, c = int(vr[idx]), int(vc[idx])
        if not stream[r, c]:
            continue
        if labels[r, c] == 0:
            labels[r, c] = next_id
            next_id += 1
        dr_, dc_ = int(down[r, c, 0]), int(down[r, c, 1])
        if dr_ >= 0 and stream[dr_, dc_] and inflow[dr_, dc_] == 1:
            labels[dr_, dc_] = labels[r, c]
    for i, idx in enumerate(order[::-1]):  # 低い所から、流れ下る先のラベルを引き継ぐ
        if context is not None and not i % _PROGRESS_EVERY:
            context.check()
        r, c = int(vr[idx]), int(vc[idx])
        if stream[r, c]:
            continue
        dr_, dc_ = int(down[r, c, 0]), int(down[r, c, 1])
        if dr_ >= 0:
            labels[r, c] = labels[dr_, dc_]
    return labels


def weighted_accumulation(dem, prop, weight, context=None):
    """Sum of `weight` over all cells upstream (and the cell itself), carried down by the D-infinity shares.
    Divided by the plain cell count, it gives the mean of `weight` over the upstream area."""
    rows, cols = dem.shape
    accum = np.nan_to_num(weight.astype(np.float64)).copy()
    valid = ~np.isnan(dem)
    r_arr, c_arr = np.where(valid)
    order = np.argsort(-dem[r_arr, c_arr])
    for i, idx in enumerate(order):
        if context is not None and not i % _PROGRESS_EVERY:
            context.check()
        r, c = int(r_arr[idx]), int(c_arr[idx])
        base = accum[r, c]
        for k, (dr, dc) in enumerate(_NB8):
            pk = prop[r, c, k]
            if pk > 0.0:
                nr, nc = r + dr, c + dc
                if 0 <= nr < rows and 0 <= nc < cols and valid[nr, nc]:
                    accum[nr, nc] += base * pk
    return accum
