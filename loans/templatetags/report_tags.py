"""Formatting for the period report: naira, changes and percentages."""

from django import template

register = template.Library()


@register.filter
def naira(value):
    """N1.2M / N850K / N12,500 - short naira amounts."""
    if value is None:
        return "–"
    value = float(value)
    for size, suffix in ((1e9, "B"), (1e6, "M"), (1e3, "K")):
        if abs(value) >= size:
            return f"₦{value / size:,.1f}{suffix}".replace(".0" + suffix, suffix)
    return f"₦{value:,.0f}"


@register.filter
def naira_full(value):
    return "–" if value is None else f"₦{float(value):,.0f}"


@register.filter
def pct(value):
    return "–" if value is None else f"{float(value):.1f}%"


@register.filter
def delta(value, unit="%"):
    """▲ 12.5% / ▼ 3 pts / –, for a change already worked out."""
    if value is None:
        return "–"
    arrow = "▲ " if value > 0 else "▼ " if value < 0 else ""
    return f"{arrow}{abs(value):.1f}{' pts' if unit == 'pts' else '%'}".replace(".0%", "%").replace(".0 pts", " pts")


@register.filter
def metric(value, kind):
    """A compare-metric value in its own unit."""
    if value is None:
        return "–"
    if kind == "money":
        return naira(value)
    if kind == "pct":
        return pct(value)
    if kind == "days":
        return f"{float(value):.1f} days"
    return f"{value:,}"
