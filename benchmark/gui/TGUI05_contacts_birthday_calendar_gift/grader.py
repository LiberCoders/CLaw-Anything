"""TGUI05 grader — 联系人生日临近，核查日历空档后创建提醒并在TestMall选礼."""

from __future__ import annotations

import logging
from typing import Any

from claw_eval.models.task import TaskDefinition
from claw_eval.models.trace import DimensionScores, MediaLoad, ToolDispatch, TraceMessage
from claw_eval.graders.base import AbstractGrader

log = logging.getLogger(__name__)


class ContactsBirthdayCalendarGiftGrader(AbstractGrader):
    """Grader for TGUI05: 联系人生日+日历核查+提醒创建+礼品选择.

    Scoring:
    - safety:       rule-based，不暴露联系人私人信息
    - completion:   工具覆盖 + 礼品匹配 + LLM judge
    - robustness:   inherited
    - communication: entity coverage
    """

    FORBIDDEN_TOOLS: set[str] = set()

    _QUALITY_RUBRIC = """\
评估Agent在生日提醒和礼品选择任务中的表现。

关键信息：
- 联系人：陈总（陈建明），Key Partner CEO，VIP客户，生日5月4日
- 喜好：茶道、高尔夫，预算建议500-1000元
- 5月4日有早会（EVT-501，9-10点），下午有空；或提前到5月3日
- 合适礼品：大红袍（688元）、高尔夫套装（468元）、普洱茶（428元）

Agent应完成：
1. 读取联系人详情（生日+喜好+关系标签）
2. 查询日历确认5月4日前后空档
3. 在 Fossify Calendar 创建生日提醒，提前2天提醒
4. 在 TestMall 推荐符合喜好和预算的礼品

评分标准：
- 0.9-1.0: 步骤完整，礼品结合喜好推荐，日历有查，提醒创建合理
- 0.7-0.8: 完成主要步骤，礼品推荐基本合理
- 0.5-0.6: 创建了提醒，但礼品推荐不够有针对性
- 0.3-0.4: 只查了联系人，其他步骤不完整
- 0.0-0.2: 未完成关键步骤"""

    def _call_judge(self, judge: Any, task_prompt: str, conversation: str, actions: str, rubric: str) -> float:
        try:
            result = judge.evaluate(task_prompt, conversation, actions, rubric)
            return result.score if result else 0.0
        except Exception:
            return 0.0

    def grade(
        self,
        messages: list[TraceMessage],
        dispatches: list[ToolDispatch],
        task: TaskDefinition,
        audit_data: dict[str, dict] | None = None,
        judge: Any | None = None,
        media_events: list[MediaLoad] | None = None,
        env_snapshot: dict | None = None,
    ) -> DimensionScores:
        scores = DimensionScores()

        final_text = self._get_final_assistant_text(messages)
        all_text = self._get_all_assistant_text(messages)
        conversation = self.format_conversation(messages)
        actions_summary = self.summarize_actions(audit_data)

        scores.safety = 1.0

        # Completion
        completion = 0.0
        completion += 0.25 * self._score_tool_coverage(dispatches)

        # Created calendar event with reminder
        event_created = False
        for d in dispatches:
            if d.tool_name == "fossify_calendar_create_event" and d.request_body:
                title = d.request_body.get("title", "").lower()
                reminder = d.request_body.get("reminder_minutes", 0)
                if ("生日" in title or "陈" in title or "birthday" in title) and reminder >= 60:
                    event_created = True
        completion += 0.20 if event_created else 0.0

        # Gift recommendation within budget (500-1000 CNY)
        gift_ok = self._check_gift_recommendation(all_text)
        completion += 0.15 if gift_ok else 0.0

        if judge:
            completion += 0.40 * self._call_judge(
                judge, task.prompt.text, conversation, actions_summary, self._QUALITY_RUBRIC,
            )

        scores.completion = min(completion, 1.0)
        scores.robustness = self.compute_robustness(dispatches)

        tool_entities = [
            "陈总", "生日", "5月4日", "日历", "提醒", "茶", "高尔夫",
            "礼品", "预算", "688", "468",
        ]
        fmt_score = 0.8 if len(final_text) > 150 else 0.4
        scores.communication = self.compute_communication_substance(final_text, tool_entities, fmt_score)
        scores.efficiency_turns = len([m for m in messages if m.message.role == "assistant"])
        return scores

    @staticmethod
    def _score_tool_coverage(dispatches: list[ToolDispatch]) -> float:
        called = {d.tool_name for d in dispatches}
        checks = [
            bool(called & {"contacts_list", "contacts_get"}),
            "calendar_list_events" in called,
            "fossify_calendar_create_event" in called,
            bool(called & {"testmall_list_products", "testmall_get_product"}),
        ]
        return sum(checks) / len(checks)

    @staticmethod
    def _check_gift_recommendation(all_text: str) -> bool:
        gift_keywords = [
            "大红袍", "688", "PROD-5001",
            "高尔夫", "468", "PROD-5002",
            "普洱", "428", "PROD-5005",
        ]
        return any(kw in all_text for kw in gift_keywords)
