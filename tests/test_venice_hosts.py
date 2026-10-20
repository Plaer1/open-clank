"""Venice behavior after managed-provider unification.

Venice (https://api.venice.ai/api/v1) is a paid, OpenAI-compatible cloud API
with native tool-calling. Agent execution is now selected by the managed engine
route rather than by matching raw provider hostnames. Teacher escalation still
classifies Venice as a cloud endpoint (loop OFF, no added latency).
"""
import inspect

from src import agent_loop, teacher_escalation


class TestAgentToolHosts:
    def test_agent_tools_are_not_selected_by_provider_hostnames(self):
        source = inspect.getsource(agent_loop)
        assert "_API_HOSTS" not in source

    def test_agent_loop_dispatches_through_managed_route(self):
        source = inspect.getsource(agent_loop)
        assert "stream_agent_target(" in source

    def test_agent_loop_has_no_raw_venice_or_unknown_provider_url_authority(self):
        source = inspect.getsource(agent_loop)
        assert "api.venice.ai" not in source
        assert "example.invalid" not in source


class TestTeacherEscalationSota:
    def test_venice_is_sota_not_self_hosted(self):
        assert teacher_escalation.is_self_hosted("https://api.venice.ai/api/v1/chat/completions") is False

    def test_known_cloud_still_sota(self):
        assert teacher_escalation.is_self_hosted("https://api.openai.com/v1") is False

    def test_local_endpoint_still_self_hosted(self):
        assert teacher_escalation.is_self_hosted("http://localhost:8000/v1") is True
