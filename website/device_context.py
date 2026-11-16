"""Keep the selected device consistent across the demonstration workflow."""

import streamlit as st

from simulation.welding_simulator import DEMO_PROFILES


PAGE_WIDGETS = {
    "运维流程": "workflow_equipment", "运行监测": "monitor_equipment",
    "维护排程": "maintenance_equipment", "故障台账": "fault_equipment",
    "预警中心": "alarm_device_filter", "仿真控制": "simulation_device_tabs",
}


def select_device(equipment_id):
    if not equipment_id:
        return  # The alarm list may explicitly show all equipment.
    st.session_state["active_equipment"] = equipment_id
    st.query_params["device"] = equipment_id
    # Keep one durable context, and restore only the visible page's widget.
    # Writing hidden widget keys resurrects stale widget state during cleanup;
    # on remount the browser can then send the original default back as a change.
    key = PAGE_WIDGETS.get(st.session_state.get("navigation"))
    if key and (key != "simulation_device_tabs" or equipment_id in DEMO_PROFILES):
        # A same-value assignment to an already-rendered widget is also forbidden.
        if st.session_state.get(key) != equipment_id:
            st.session_state[key] = equipment_id


def device_changed(widget_key):
    page = next((page for page, key in PAGE_WIDGETS.items() if key == widget_key), None)
    if page and st.session_state.get("navigation") != page:
        return  # Ignore a late browser event from the page just left.
    select_device(st.session_state.get(widget_key))


def initialize_device_context():
    if "active_equipment" not in st.session_state:
        candidate = st.query_params.get("device")
        if candidate in DEMO_PROFILES:
            select_device(candidate)
        else:
            st.session_state["active_equipment"] = next(iter(DEMO_PROFILES))
            if "device" in st.query_params:
                del st.query_params["device"]


def prepare_device_widget(key):
    """A widget may be cleaned up when its page is hidden; restore from context."""
    if key not in st.session_state:
        if key == "alarm_device_filter" and "device" not in st.query_params:
            return  # Initial alarm page shows all equipment.
        st.session_state[key] = st.session_state.get("active_equipment", "WELD-001")
