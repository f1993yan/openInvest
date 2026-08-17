from core.decision_mode import ALGORITHM_ONLY, LLM_COMMITTEE, normalize_decision_mode


def test_empty_decision_mode_defaults_to_algorithm_only(monkeypatch):
    monkeypatch.delenv("INVEST_COMMITTEE_MODE", raising=False)
    monkeypatch.delenv("INVEST_DECISION_MODE", raising=False)

    assert normalize_decision_mode("", market="a") == ALGORITHM_ONLY


def test_auto_decision_mode_uses_algorithm_only_for_a_shares():
    assert normalize_decision_mode("auto", market="a") == ALGORITHM_ONLY
    assert normalize_decision_mode("auto", market="hk") == LLM_COMMITTEE


def test_llm_alias_restores_committee_debate():
    assert normalize_decision_mode("llm", market="a") == LLM_COMMITTEE
