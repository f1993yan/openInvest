from core.fundamental_model import assess_fundamentals, select_fundamental_model


def test_selects_sector_specific_models():
    assert select_fundamental_model(symbol="600900", name="长江电力") == "regulated_yield"
    assert select_fundamental_model(symbol="000001", sector="银行") == "financial_residual_income"
    assert select_fundamental_model(symbol="688099", sector="半导体") == "growth_innovation"
    assert select_fundamental_model(symbol="603308", industry="高端铸件/航空航天") == "industrial_quality_value"


def test_selects_models_from_eastmoney_industry_names():
    assert select_fundamental_model(symbol="000000", sector="电子元件") == "growth_innovation"
    assert select_fundamental_model(symbol="000000", sector="消费电子") == "growth_innovation"
    assert select_fundamental_model(symbol="000000", sector="通信设备") == "growth_innovation"
    assert select_fundamental_model(symbol="000000", sector="电力行业") == "regulated_yield"
    assert select_fundamental_model(symbol="000000", sector="电网设备") == "industrial_quality_value"
    assert select_fundamental_model(symbol="000000", sector="通用设备") == "industrial_quality_value"
    assert select_fundamental_model(symbol="000000", sector="非金属材料") == "industrial_quality_value"


def test_missing_fundamentals_are_neutral_low_confidence():
    assessment = assess_fundamentals(
        symbol="600900",
        name="长江电力",
        sector="公用事业",
        industry="水电运营",
        metrics={},
    )

    assert assessment.model_key == "regulated_yield"
    assert assessment.low_confidence is True
    assert assessment.score == 50.0
    assert assessment.anchor_multiplier == 1.0
    assert assessment.expected_return_adjustment_pct == 0.0


def test_growth_innovation_model_rewards_quality_growth_with_valuation_discipline():
    assessment = assess_fundamentals(
        symbol="688099",
        name="晶晨股份",
        sector="半导体",
        industry="SoC芯片",
        metrics={
            "revenue_growth": 0.28,
            "gross_margin": 0.48,
            "operating_margin": 0.18,
            "rd_to_revenue": 0.15,
            "roic": 0.18,
            "fcf_yield": 0.04,
            "ps": 5.0,
            "peg": 1.2,
            "net_debt_to_ebitda": -0.2,
        },
    )

    assert assessment.low_confidence is False
    assert assessment.coverage > 0.8
    assert assessment.score > 70.0
    assert assessment.anchor_multiplier > 1.0
    assert assessment.expected_return_adjustment_pct > 0.0
