from unittest.mock import MagicMock

from agents.sdk_agent import SDKAgent
from core.llm_telemetry import TelemetryMeta


def test_telemetry_provider_is_independent_from_client_transport(monkeypatch):
    client_factory = MagicMock()
    monkeypatch.setattr("agents.sdk_agent.OpenAI", client_factory)
    meta = TelemetryMeta(provider="dashscope", model="qwen-plus")

    agent = SDKAgent(
        system_prompt="test",
        model="qwen-plus",
        api_key="test-key",
        base_url="https://dashscope.example/v1",
        provider="deepseek",
        telemetry_provider="dashscope",
        telemetry_meta=meta,
    )

    client_factory.assert_called_once_with(
        api_key="test-key",
        base_url="https://dashscope.example/v1",
    )
    assert agent.provider == "deepseek"
    assert agent.telemetry_meta.provider == "dashscope"
