"""AI Analyst behavior from the live evaluation (AI Analyst Evaluation Report, 2026-10-23):

- MO-02: a model that returns 404 must not be reported as a temporary outage.
- OOS-01 / OOS-04: an off-topic task must not be performed when no query is needed.
- TR-08: a truncated JSON answer is retried only after a quick first call (bounded wait).
"""

import unittest
from unittest import mock

import support

from controlplane import ai as ai_mod
from controlplane.gcp import GcpError
from controlplane.httpkit import ApiError
from controlplane.services import analyst


class FakeAi:
    def __init__(self, plan=None, error=None):
        self.plan = plan
        self.error = error
        self.calls = 0

    def resolve_model(self, settings, requested):  # pylint: disable=unused-argument
        return requested or "gemini-test"

    def generate(self, *args, **kwargs):  # pylint: disable=unused-argument
        self.calls += 1
        if self.error is not None:
            raise self.error
        return self.plan


def chat(ai, locale="en"):
    ctx = support.FakeCtx(body={"question": "question", "locale": locale})
    ctx.app.ai = ai
    with mock.patch.object(analyst, "schema_context", return_value=[]):
        return analyst.chat(ctx)


class PlanningErrorTest(unittest.TestCase):
    def message(self, kind, locale="en"):
        with self.assertRaises(ApiError) as caught:
            chat(FakeAi(error=GcpError(kind, 404 if kind == "not_found" else None)), locale)
        self.assertEqual(caught.exception.code, "upstream_error")
        return caught.exception.message

    def test_missing_model_is_not_a_temporary_outage(self):
        self.assertEqual(self.message("not_found"), analyst.MESSAGES["en"]["model_missing"])
        self.assertEqual(self.message("not_found", "ko"), analyst.MESSAGES["ko"]["model_missing"])

    def test_other_kinds(self):
        self.assertEqual(self.message("denied"), analyst.MESSAGES["en"]["denied"])
        self.assertEqual(self.message("invalid"), analyst.MESSAGES["en"]["invalid"])
        self.assertEqual(self.message("unavailable"), analyst.MESSAGES["en"]["unavailable"])


class NoQueryAnswerTest(unittest.TestCase):
    def answer(self, text, locale="en"):
        plan = {"sql": "", "answer": text, "chartType": "none", "suggestions": ["How many prompts last week?"]}
        out = chat(FakeAi(plan=plan), locale)
        self.assertEqual(out["sql"], "")
        self.assertEqual(out["rows"], [])
        return out

    def test_code_is_not_returned(self):
        out = self.answer("That is outside my scope, but here it is:\n```python\ndef reverse(head):\n    pass\n```")
        self.assertEqual(out["answer"], analyst.MESSAGES["en"]["scope"])
        self.assertEqual(out["suggestions"], ["How many prompts last week?"])

    def test_drafted_documents_are_not_returned(self):
        draft = "Out of scope, but here is a draft.\n\nSubject: Gemini Enterprise is here\n\n" + "Dear all, " * 80
        self.assertEqual(self.answer(draft)["answer"], analyst.MESSAGES["en"]["scope"])
        self.assertEqual(self.answer(draft, "ko")["answer"], analyst.MESSAGES["ko"]["scope"])

    def test_short_explanations_are_kept(self):
        text = ("The billing export has no prepaid balance field, so the remaining credit cannot be shown. "
                "The Billing console shows the balance.")
        self.assertEqual(self.answer(text)["answer"], text)

    def test_empty_answer_gets_the_scope_message(self):
        self.assertEqual(self.answer("")["answer"], analyst.MESSAGES["en"]["scope"])

    def test_prompt_asks_for_ungrouped_counts_and_no_unrelated_tasks(self):
        ctx = support.FakeCtx()
        prompt = analyst._system_prompt(ctx, [], "en", "2026-10-26")  # pylint: disable=protected-access
        self.assertIn("ungrouped COUNT or SUM", prompt)
        self.assertIn("Never perform an unrelated task", prompt)


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


class FakeModels:
    """Returns the given texts in order; each call advances the clock by its duration."""

    def __init__(self, clock, outputs):
        self.clock = clock
        self.outputs = list(outputs)
        self.budgets = []

    def generate_content(self, model, contents, config):  # pylint: disable=unused-argument
        text, seconds = self.outputs.pop(0)
        self.budgets.append(config.max_output_tokens)
        self.clock.now += seconds
        return mock.Mock(text=text, candidates=[])


class JsonRetryWindowTest(unittest.TestCase):
    def run_generate(self, outputs):
        clock = Clock()
        models = FakeModels(clock, outputs)
        engine = ai_mod.AiEngine(gcp=None)
        settings = support.make_settings()
        with mock.patch.object(engine, "_client", return_value=mock.Mock(models=models)), \
                mock.patch.object(ai_mod.time, "monotonic", clock):
            try:
                return engine.generate(settings, "gemini-test", "q", schema={"type": "OBJECT"},
                                       max_output_tokens=8192), models
            except GcpError as err:
                return err, models

    def test_quick_truncated_answer_is_retried_with_a_larger_budget(self):
        value, models = self.run_generate([('{"sql": "SELE', 5), ('{"sql": ""}', 5)])
        self.assertEqual(value, {"sql": ""})
        self.assertEqual(models.budgets, [8192, 16384])

    def test_slow_truncated_answer_is_not_retried(self):
        value, models = self.run_generate([('{"sql": "SELE', 45)])
        self.assertIsInstance(value, GcpError)
        self.assertEqual(value.kind, "invalid")
        self.assertEqual(models.budgets, [8192])


if __name__ == "__main__":
    unittest.main()
