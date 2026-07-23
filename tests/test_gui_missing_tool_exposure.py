from __future__ import annotations

from pathlib import Path

import yaml


EXPECTED_GUI_TOOLS = {
    "TGUI01_myexpenses_overbudget_finance_email": {
        "my_expenses_list_accounts",
        "my_expenses_get_account",
        "my_expenses_list_transactions",
    },
    "TGUI02_habit_gap_calendar_clock_replan": {
        "loop_habits_list_habits",
        "loop_habits_get_habit",
        "clock_list_alarms",
        "clock_update_alarm",
    },
    "TGUI03_email_bill_finance_myexpenses_habit": {
        "gmail_clone_list_messages",
        "gmail_clone_get_message",
        "my_expenses_list_transactions",
        "my_expenses_add_transaction",
        "loop_habits_list_habits",
        "loop_habits_check_habit",
    },
    "TGUI04_contacts_new_crm_welcome_email": {
        "contacts_list",
        "contacts_get",
    },
    "TGUI05_contacts_birthday_calendar_gift": {
        "contacts_list",
        "contacts_get",
        "fossify_calendar_list_events",
        "fossify_calendar_get_event",
        "fossify_calendar_create_event",
        "testmall_list_products",
        "testmall_get_product",
    },
    "TGUI06_habit_crm_performance_workmail_report": {
        "loop_habits_list_habits",
        "loop_habits_get_habit",
    },
    "TGUI23_loop_habit_finance_expenses_saas": {
        "loop_habits_get_habit",
        "my_expenses_list_accounts",
        "my_expenses_get_account",
    },
    "TGUI44_stress_test_milestone_habits_expenses_calendar": {
        "loop_habits_get_habit",
    },
    "TGUI49_cloudinfra_invoice_expenses_habits": {
        "loop_habits_get_habit",
    },
    "TGUI50_deployment_habits_scheduler_workmail_notes": {
        "loop_habits_get_habit",
    },
}


def test_gui_tasks_expose_required_scoring_tools() -> None:
    gui_root = Path(__file__).resolve().parents[1] / "benchmark/gui"
    missing: list[str] = []

    for task_dir, expected_tools in EXPECTED_GUI_TOOLS.items():
        data = yaml.safe_load((gui_root / task_dir / "task.yaml").read_text())
        declared_tools = {
            tool["name"]
            for tool in data.get("tools", [])
            if isinstance(tool, dict) and "name" in tool
        }
        declared_endpoints = {
            endpoint["tool_name"]
            for endpoint in data.get("tool_endpoints", [])
            if isinstance(endpoint, dict) and "tool_name" in endpoint
        }

        for tool_name in sorted(expected_tools):
            if tool_name not in declared_tools:
                missing.append(f"{task_dir}: tools missing {tool_name}")
            if tool_name not in declared_endpoints:
                missing.append(f"{task_dir}: tool_endpoints missing {tool_name}")

    assert not missing, "\n".join(missing)
