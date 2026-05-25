"""Regression tests for validator._extract_service.

These guard against the original `tool_name.split("_")[0]` bug that
silently dropped every `claw_*` service (mapping `claw_notion_append_blocks`
to the non-existent service `"claw"`) and against the verb-whitelist regex
that missed `append`, `post`, `archive`, etc.
"""

from claw_anything.gen.validator import _extract_service


def test_simple_service():
    assert _extract_service("gmail_list_messages") == "gmail"
    assert _extract_service("calendar_create_event") == "calendar"
    assert _extract_service("scheduler_list_jobs") == "scheduler"


def test_non_whitelisted_verb():
    # `append`, `post`, `archive`, `history` are not in the old verb regex.
    assert _extract_service("scheduler_job_history") == "scheduler"


def test_two_segment_claw_services():
    # The original split('_')[0] bug returned "claw" for these, which is
    # not in ALL_SERVICES — every claw_* tool was silently dropped.
    assert _extract_service("claw_notion_append_blocks") == "claw_notion"
    assert _extract_service("claw_slack_post_message") == "claw_slack"
    assert _extract_service("claw_feishu_create_doc") == "claw_feishu"
    assert _extract_service("claw_zhihu_get_question") == "claw_zhihu"
    assert _extract_service("claw_obsidian_update_note") == "claw_obsidian"


def test_three_segment_claw_services():
    # claw_wechat_moments is itself a service, not claw_wechat + moments.
    # The longest-prefix match must pick the 3-segment service.
    assert _extract_service("claw_wechat_moments_list") == "claw_wechat_moments"
    assert _extract_service("claw_qq_zone_post") == "claw_qq_zone"
    # But plain claw_wechat tools still resolve to claw_wechat.
    assert _extract_service("claw_wechat_send_message") == "claw_wechat"


def test_unknown_service_returns_none():
    assert _extract_service("nonexistent_foo_bar") is None
    assert _extract_service("") is None
    assert _extract_service("claw") is None  # not a complete service name


def test_exact_service_name():
    # The tool_name could match the service name exactly (rare but valid).
    assert _extract_service("gmail") == "gmail"
    assert _extract_service("claw_notion") == "claw_notion"
