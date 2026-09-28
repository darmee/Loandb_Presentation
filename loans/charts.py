import math
from decimal import Decimal

# Nine distinct colours, so a pie of up to nine slices never repeats one. The
# first six are unchanged; the three added after them are amber, teal and pink.
SERIES_COLOURS = [
    "#5b2a86", "#2563eb", "#0f766e", "#b45309", "#15803d", "#be123c"
]

def _nice_ceiling(value):
    if value <= 0:
        return 1
    value = float(value)
    magnitude = 10 ** (len(str(int(value))) - 1)
    for step in (1, 2, 2.5, 5, 10):
        candidate = step * magnitude
        if value <= candidate:
            return candidate
    return 10 * magnitude

def _format_axis(value):
    number = float(value)
    for limit, suffix in ((1e9, "b"), (1e6, "m"), (1e3, "k")):
        if abs(number) >= limit:
            trimmed = f"{number / limit:.1f}".removesuffix(".0")
            return f"{trimmed}{suffix}"
    return f"{number:,.0f}"

def column_chart(points, width=780, height=230, gridlines=4):
    pad = {"left": 46, "right": 10, "top": 22, "bottom": 28}   # top leaves room for the value label over the tallest bar
    plot_w = width - pad["left"] - pad["right"]
    plot_h = height - pad["top"] - pad["bottom"]

    values = [float(p["value"] or 0) for p in points]
    top = _nice_ceiling(max(values)) if values else 1
    slot = plot_w / len(points) if points else plot_w
    bar_w = max(4.0, slot * 0.62)

    bars = []
    for index, point in enumerate(points):
        value = float(point["value"] or 0)
        bar_h = (value / top) * plot_h if top else 0
        x = pad["left"] + slot * index + (slot - bar_w) / 2
        bars.append({
            "x": round(x, 2),
            "y": round(pad["top"] + plot_h - bar_h, 2),
            "width": round(bar_w, 2),
            "height": round(bar_h, 2),
            "label": point["label"],
            "value": point["value"],
            "label_x": round(x + bar_w / 2, 2),
            "value_y": round(pad["top"] + plot_h - bar_h - 5, 2),
            "colour": SERIES_COLOURS[index % len(SERIES_COLOURS)],
            # Short text to print above the bar ("120", "104.3m").
            "display": point.get("display") or _format_axis(value),
        })

    axis = []
    for step in range(gridlines + 1):
        value = top * step / gridlines
        y = pad["top"] + plot_h - (plot_h * step / gridlines)
        axis.append({
            "y": round(y, 2),
            "label": _format_axis(value),
            "x1": pad["left"],
            "x2": width - pad["right"],
            "label_x": pad["left"] - 8,
        })

    return {
        "width": width,
        "height": height,
        "bars": bars,
        "axis": axis,
        "baseline_y": round(pad["top"] + plot_h, 2),
        "label_y": height - 9,
        "empty": not points,
    }


def bar_chart(items, width=780, row_height=30, label_width=210, value_width=120):
    track_x = label_width
    track_w = max(60, width - label_width - value_width)
    values = [float(item["value"] or 0) for item in items]
    top = max(values) if values else 0

    rows = []
    for index, item in enumerate(items):
        value = float(item["value"] or 0)
        bar_w = (value / top) * track_w if top else 0
        y = index * row_height
        rows.append({
            "y": round(y, 2),
            "bar_y": round(y + row_height * 0.18, 2),
            "text_y": round(y + row_height * 0.66, 2),
            "width": round(max(bar_w, 1.5), 2),
            "height": round(row_height * 0.58, 2),
            "label": item["label"],
            "value": item["value"],
            "display": item.get("display") or _format_axis(value),
            "note": item.get("note", ""),
            "stripe": index % 2 == 1,
            "colour": SERIES_COLOURS[index % len(SERIES_COLOURS)],
        })

    return {
        "width": width,
        "height": max(row_height, row_height * len(items)),
        "rows": rows,
        "track_x": track_x,
        "track_w": track_w,
        "value_x": track_x + track_w + 10,
        "row_height": row_height,
        "empty": not items,
    }

def pie_chart(items, size=250, hole=0.0):
    """Pie slices as SVG path data.

    `items` is [{"label": ..., "value": <number>}, ...]. Returns one entry per
    slice carrying a ready-made `d` attribute, its colour and its share.

    Angles start at twelve o'clock and run clockwise. A single slice covering
    the whole circle is flagged rather than pathed - an arc whose start and end
    points are identical draws nothing at all, which is the classic way a pie
    chart renders blank when one category holds everything.
    """
    values = [float(item["value"] or 0) for item in items]
    total = sum(values)
    radius = size / 2
    cx = cy = radius

    def point(degrees):
        radians = math.radians(degrees)
        return (
            round(cx + radius * math.cos(radians), 2),
            round(cy + radius * math.sin(radians), 2),
        )

    slices = []
    angle = -90.0
    for index, item in enumerate(items):
        value = float(item["value"] or 0)
        share = (value / total) if total else 0.0
        sweep = share * 360.0
        start, end = angle, angle + sweep
        x1, y1 = point(start)
        x2, y2 = point(end)
        large = 1 if sweep > 180 else 0
        mid = point(start + sweep / 2)
        label_at = radius * 0.62
        mid_rad = math.radians(start + sweep / 2)

        slices.append({
            "label": item["label"],
            "value": item["value"],
            "pct": round(share * 100, 1),
            # An item may carry its own colour (e.g. the same status is the
            # same colour everywhere); otherwise slices take the palette in order.
            "colour": item.get("colour") or SERIES_COLOURS[index % len(SERIES_COLOURS)],
            "full": share >= 0.9995,
            "d": f"M {cx} {cy} L {x1} {y1} A {radius} {radius} 0 {large} 1 {x2} {y2} Z",
            "label_x": round(cx + label_at * math.cos(mid_rad), 2),
            "label_y": round(cy + label_at * math.sin(mid_rad), 2),
            # Below about 7% the slice is too narrow to hold its own label.
            "show_label": share >= 0.07,
        })
        angle = end

    return {
        "size": size,
        "cx": cx,
        "cy": cy,
        "radius": radius,
        "slices": slices,
        "total": total,
        "empty": not total,
    }


def money(value):
    if value is None:
        return "0.00"
    return f"{Decimal(value):,.2f}"