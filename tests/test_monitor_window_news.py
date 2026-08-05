from scripts.monitor_window_news import MonitorNewsMixin, _news_leader_label, _news_leader_query
from scripts.monitor_window_services import _load_weekend_news_cards


class _StatusText:
    def __init__(self):
        self.value = ""

    def set(self, value):
        self.value = value


class _NewsWindowStub:
    def __init__(self, rows):
        self.current_rows = rows
        self.status_text = _StatusText()
        self.closed = False
        self.opened = []

    def _close_news_popover(self):
        self.closed = True

    def _open_analysis_dialog(self, symbol, row):
        self.opened.append((symbol, row))


def test_news_leader_query_prefers_symbol_then_code_then_name():
    assert _news_leader_query({"symbol": "601138", "code": "SH601138", "name": "工业富联"}) == "601138"
    assert _news_leader_query({"code": "00700", "name": "腾讯控股"}) == "00700"
    assert _news_leader_query({"name": "工业富联"}) == "工业富联"


def test_news_leader_label_keeps_name_and_symbol_visible():
    assert _news_leader_label({"symbol": "601138", "name": "工业富联"}) == "工业富联 (601138)"
    assert _news_leader_label({"symbol": "601138"}) == "601138"
    assert _news_leader_label({}) == "未知标的"


def test_news_leader_click_opens_existing_monitor_row_directly():
    row = {"symbol": "601138", "name": "工业富联", "price": {"current": 50.0}}
    window = _NewsWindowStub([row])

    MonitorNewsMixin._open_news_leader_analysis(window, {"symbol": "601138", "name": "工业富联"})

    assert window.closed is True
    assert window.opened == [("601138", row)]


def test_news_leader_click_opens_resolving_analysis_for_untracked_stock():
    window = _NewsWindowStub([])

    MonitorNewsMixin._open_news_leader_analysis(window, {"symbol": "300476", "name": "胜宏科技"})

    symbol, row = window.opened[0]
    assert window.closed is True
    assert symbol == "300476"
    assert row["name"] == "胜宏科技"
    assert row["_is_resolving"] is True


def test_news_leader_without_identifier_does_not_open_analysis():
    window = _NewsWindowStub([])

    MonitorNewsMixin._open_news_leader_analysis(window, {})

    assert window.closed is False
    assert window.opened == []
    assert "缺少" in window.status_text.value


def test_weekend_news_signature_tracks_latest_summary_change(tmp_path):
    window = _NewsWindowStub([])
    window.weekend_news_dir = tmp_path
    summary = tmp_path / "summary_2026-08-02.json"
    summary.write_text('{"sector_opportunities": []}', encoding="utf-8")

    first = MonitorNewsMixin._weekend_news_files_signature(window)
    summary.write_text('{"sector_opportunities": [{"sector": "半导体"}]}', encoding="utf-8")
    second = MonitorNewsMixin._weekend_news_files_signature(window)

    assert first != second
    assert second[1] == summary.name


def test_weekend_news_source_distinguishes_news_date_from_summary_filename(tmp_path):
    summary = tmp_path / "summary_2026-08-05.json"
    summary.write_text(
        '{"_source_date_range":"2026-08-02","sector_opportunities":'
        '[{"theme":"半导体","sector":"半导体","logic":"产业新闻",'
        '"heat_score":0.8,"leaders":[]}]}',
        encoding="utf-8",
    )

    cards, source = _load_weekend_news_cards(tmp_path)

    assert len(cards) == 1
    assert source == "2026-08-02 新闻 · summary_2026-08-05.json"
