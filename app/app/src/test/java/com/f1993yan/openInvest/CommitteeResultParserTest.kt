package com.f1993yan.openInvest

import org.junit.Assert.*
import org.junit.Test

class CommitteeResultParserTest {
    @Test
    fun parsesDirectResponseLevelsAndBehavioralFactor() {
        val parsed = parseCachedResult("600900", """{"verdict":"ACCUMULATE","suggested_alloc_cny":5000,"entry_exit_points":{"buy_pullback_price":27.2,"buy_breakout_price":28.8,"stop_loss_price":25.9,"take_profit_price":32.1},"behavioral_factor":{"score":82.4,"target_weight_pct":24.6,"selected":true,"low_confidence":false,"optimizer_weight":0.81,"trailing_3m_sample_size":63}}""")
        assertNotNull(parsed)
        assertEquals(27.2, parsed!!.buyCriteria?.pullback_price ?: 0.0, 0.0001)
        assertEquals(25.9, parsed.exitPoints?.stop_loss_price ?: 0.0, 0.0001)
        assertEquals(82.4, parsed.behavioralFactor?.score ?: 0.0, 0.0001)
        assertTrue(parsed.behavioralFactor?.selected == true)
        assertEquals(63, parsed.behavioralFactor?.trailing_3m_sample_size)
    }
}
