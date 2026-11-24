from src import teacher_escalation


class TestTeacherEscalationSota:
    def test_kimi_code_is_sota_not_self_hosted(self):
        assert teacher_escalation.is_self_hosted("https://api.kimi.com/coding/v1/chat/completions") is False
