package com.f1993yan.openInvest

import org.junit.Assert.*
import org.junit.Test

class CommitteeResultParserTest {
    @Test
    fun parsesDirectResponseLevelsAndBehavioralFactor() {
        val parsed = parseCachedResult("600900", """{"verdict":"ACCUMULATE","suggested_alloc_cny":5000,"entry_exit_points":{"buy_pullback_price":27.2,"buy_breakout_price":28.8,"stop_loss_price":25.9,"take_profit_price":32.1},"behavioral_factor":{"score":82.4,"target_weight_pct":24.6,"selected":true,"low_confidence":false,"optimizer_weight":0.81,"trailing_3m_sample_size":63,"selection_scope":"factor_model_target_portfolio","represents_account_holding":false}}""")
        assertNotNull(parsed)
        assertEquals(27.2, parsed!!.buyCriteria?.pullback_price ?: 0.0, 0.0001)
        assertEquals(25.9, parsed.exitPoints?.stop_loss_price ?: 0.0, 0.0001)
        assertEquals(82.4, parsed.behavioralFactor?.score ?: 0.0, 0.0001)
        assertTrue(parsed.behavioralFactor?.selected == true)
        assertEquals(63, parsed.behavioralFactor?.trailing_3m_sample_size)
        assertEquals("factor_model_target_portfolio", parsed.behavioralFactor?.selection_scope)
        assertTrue(parsed.behavioralFactor?.represents_account_holding == false)
    }

    @Test
    fun parsesHongKongSpatioFactorSeparately() {
        val parsed = parseCachedResult(
            "00700",
            """{"verdict":"HOLD","hk_spatio_factor":{"model_key":"hk_spatio_temporal_momentum_proxy_v1","score":91.2,"expected_return_pct":4.3,"target_weight_pct":35.0,"selected":true,"low_confidence":false,"sample_size":40,"optimizer_weight":0.85,"rebalance_sessions":20,"selection_scope":"hk_factor_model_target_portfolio","represents_account_holding":false}}"""
        )

        assertNotNull(parsed)
        assertEquals(91.2, parsed!!.hkSpatioFactor?.score ?: 0.0, 0.0001)
        assertEquals(35.0, parsed.hkSpatioFactor?.target_weight_pct ?: 0.0, 0.0001)
        assertEquals(20, parsed.hkSpatioFactor?.rebalance_sessions)
        assertTrue(parsed.hkSpatioFactor?.selected == true)
        assertNull(parsed.behavioralFactor)
    }
}
