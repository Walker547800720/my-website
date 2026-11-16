"""Presentation helpers. All displayed values come from existing services."""

from html import escape
from pathlib import Path

import streamlit as st


def apply_theme():
    st.html(f"<style>{Path(__file__).with_name('style.css').read_text(encoding='utf-8')}</style>")


def badge(text, tone="neutral"):
    return f'<span class="badge {escape(tone, quote=True)}">{escape(str(text))}</span>'


def status_tone(status):
    return {"运行中": "green", "正常": "green", "已处理": "green", "已解除": "green",
            "即将维护": "amber", "处理中": "amber", "警告": "amber",
            "维护到期": "red", "故障": "red", "严重": "red", "未处理": "red"}.get(status, "neutral")


def kpis(items):
    """Items are (label, value, hint, tone); no business rules live here."""
    cards = ''.join(
        f'<div class="kpi"><div class="kpi-label"><i class="dot {escape(tone)}"></i>{escape(label)}</div>'
        f'<div class="kpi-value">{escape(str(value))}</div><div class="kpi-hint">{escape(hint)}</div></div>'
        for label, value, hint, tone in items
    )
    st.html(f'<div class="kpi-grid" style="--count:{len(items)}">{cards}</div>')


def section(title, subtitle=""):
    st.html(f'<div class="section-title">{escape(title)}</div>'
            f'<div class="section-subtitle">{escape(subtitle)}</div>' if subtitle else
            f'<div class="section-title">{escape(title)}</div>')


def remaining_text(hours):
    if hours < 0:
        return f"已超期 {abs(hours):g} 小时"
    if hours == 0:
        return "已到维护时间"
    return f"剩余 {hours:g} 小时"


def styled_records(frame, columns):
    colors = {"red": ("#fff0ef", "#b83732"), "amber": ("#fff7e5", "#9b650c"),
              "green": ("#eaf7f0", "#22815c"), "neutral": ("#f0f3f8", "#64748b")}
    def style(value):
        bg, fg = colors[status_tone(value)]
        return f"background-color: {bg}; color: {fg}; font-weight: 600"
    return frame.style.map(style, subset=[name for name in columns if name in frame.columns])
