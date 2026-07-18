from services.event_notifier import _STANCE_ICON, _build_subject


def test_opportunity_uses_inspection_not_buy_icon():
    assert _STANCE_ICON["opportunity"] != "🎯"
    subject = _build_subject([{"stance": "opportunity", "affected_symbols": ["000001"]}])
    assert "🎯" not in subject
    assert "[Opportunity]" in subject
