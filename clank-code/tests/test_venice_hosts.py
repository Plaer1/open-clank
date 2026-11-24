"""Venice behavior after managed-provider unification.

Venice (https://api.venice.ai/api/v1) is a paid, OpenAI-compatible cloud API
with native tool-calling. Agent execution is now selected by the managed engine
route rather than by matching raw provider hostnames. Teacher escalation still
classifies Venice as a cloud endpoint (loop OFF, no added latency).
"""
from src import teacher_escalation


class TestTeacherEscalationSota:
    def test_venice_is_sota_not_self_hosted(self):
        assert teacher_escalation.is_self_hosted("https://api.venice.ai/api/v1/chat/completions") is False

    def test_known_cloud_still_sota(self):
        assert teacher_escalation.is_self_hosted("https://api.openai.com/v1") is False

    def test_local_endpoint_still_self_hosted(self):
        assert teacher_escalation.is_self_hosted("http://localhost:8000/v1") is True
