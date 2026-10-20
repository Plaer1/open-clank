"""Kimi Code host-allowlist behavior (follow-up to provider support).

Kimi Code (https://api.kimi.com/coding/v1) is a subscription, OpenAI-compatible
cloud API with native tool-calling. These tests pin the three host-list integrations:
  - agent loop sends native tool schemas to Kimi Code (not fenced-block parsing),
  - teacher escalation treats Kimi Code as SOTA (loop OFF, no added latency).
"""
import inspect

from src import agent_loop, teacher_escalation


class TestAgentToolHosts:
    def test_agent_tools_are_not_selected_by_provider_hostnames(self):
        source = inspect.getsource(agent_loop)
        assert "_API_HOSTS" not in source
        assert "stream_agent_target(" in source

    def test_agent_loop_has_no_raw_kimi_or_unknown_provider_url_authority(self):
        source = inspect.getsource(agent_loop)
        assert "api.kimi.com" not in source
        assert "example.invalid" not in source


class TestTeacherEscalationSota:
    def test_kimi_code_is_sota_not_self_hosted(self):
        assert teacher_escalation.is_self_hosted("https://api.kimi.com/coding/v1/chat/completions") is False

    def test_known_cloud_still_sota(self):
        assert teacher_escalation.is_self_hosted("https://api.openai.com/v1") is False

    def test_local_endpoint_still_self_hosted(self):
        assert teacher_escalation.is_self_hosted("http://localhost:8000/v1") is True
