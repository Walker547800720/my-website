"""Sidebar source selection shared by every page."""

from pathlib import Path
import sqlite3

import streamlit as st

from website.runtime import suspend_workspace
from website.workspaces import create_demo, demo_path, list_demos


def _clear_page_state():
    st.query_params["workspace"] = st.session_state.get("workspace_choice", "current")
    if "device" in st.query_params:
        del st.query_params["device"]
    for key in list(st.session_state):
        if key not in ("workspace_choice", "workspace_feedback"):
            del st.session_state[key]
    st.session_state["navigation"] = "运维流程"


def _new_round(root):
    previous = st.session_state.get("workspace_choice", "current")
    try:
        info = create_demo(root)
        if previous != "current":
            suspend_workspace(demo_path(root, previous))
    except (OSError, ValueError, sqlite3.Error, RuntimeError) as error:
        st.session_state["workspace_feedback"] = f"创建失败：{error}"
        return
    st.session_state["workspace_choice"] = info["id"]
    st.session_state["workspace_feedback"] = "新一轮演示已准备好，初始预警已同步。"
    _clear_page_state()


def render_workspace_selector(base_path):
    base_path = Path(base_path)
    root = base_path.parent / "demos"
    rounds = {item["id"]: item for item in list_demos(root)}
    if "workspace_choice" not in st.session_state:
        st.session_state["workspace_choice"] = st.query_params.get("workspace", "current")
    if st.session_state.get("workspace_choice", "current") not in ("current", *rounds):
        st.session_state["workspace_choice"] = "current"
        st.query_params["workspace"] = "current"
    st.selectbox("数据环境", ["current", *rounds], key="workspace_choice",
                 format_func=lambda value: "现有数据 · 保留历史" if value == "current" else "演示 · " + rounds[value]["label"],
                 on_change=_clear_page_state)
    choice = st.session_state["workspace_choice"]
    st.button("新建答辩演示" if choice == "current" else "保存本轮并新建", key="new_demo_round",
              width="stretch", on_click=_new_round, args=(root,))
    st.caption("独立演示从75/92/45 h开始；新建时旧轮保留并暂停。")
    feedback = st.session_state.pop("workspace_feedback", None)
    if feedback:
        st.caption(feedback)
    path = base_path if choice == "current" else demo_path(root, choice)
    return path, choice != "current", rounds.get(choice)
