"""Tests for Nova agent loop — handler dispatch, StepOutcome."""

from nova.agent_loop import StepOutcome, BaseHandler


class TestStepOutcome:

    def test_step_outcome_fields(self):
        outcome = StepOutcome(data={"key": "val"}, next_prompt="continue", should_exit=False)
        assert outcome.data == {"key": "val"}
        assert outcome.next_prompt == "continue"
        assert outcome.should_exit is False

    def test_step_outcome_defaults(self):
        outcome = StepOutcome(data="result")
        assert outcome.next_prompt is None
        assert outcome.should_exit is False

    def test_step_outcome_exit(self):
        outcome = StepOutcome(data=None, next_prompt="", should_exit=True)
        assert outcome.should_exit is True


class TestHandlerDispatch:

    def test_dispatch_finds_do_method(self):
        class TestHandler(BaseHandler):
            def do_test_tool(self, args, response):
                return StepOutcome(data=args.get("value"), next_prompt="done")

        handler = TestHandler()
        outcome = handler.dispatch("test_tool", {"value": 42}, None)
        assert outcome.data == 42

    def test_dispatch_unknown_tool(self):
        handler = BaseHandler()
        outcome = handler.dispatch("unknown_tool", {}, None)
        assert "Unknown tool" in outcome.next_prompt

    def test_dispatch_bad_json(self):
        handler = BaseHandler()
        outcome = handler.dispatch("bad_json", {"msg": "test"}, None)
        assert outcome.next_prompt == "test"
        assert outcome.data is None