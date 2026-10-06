package com.kulai.memory.audio

import org.junit.Assert.assertArrayEquals
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test

class AudioContractTest {
    @Test
    fun `canonical audio limits match backend protocol`() {
        assertEquals(16_000, VoiceAudioContract.SAMPLE_RATE_HZ)
        assertEquals(1, VoiceAudioContract.CHANNELS)
        assertEquals(2, VoiceAudioContract.BYTES_PER_SAMPLE)
        assertEquals(19_200_000, VoiceAudioContract.MAX_TOTAL_BYTES)
        assertTrue(VoiceAudioContract.FRAME_BYTES <= VoiceAudioContract.MAX_FRAME_BYTES)
        assertEquals(0, VoiceAudioContract.FRAME_BYTES % 2)
    }

    @Test
    fun `samples are encoded as signed PCM16 little endian`() {
        val encoded = VoiceAudioContract.encodeLittleEndian(
            shortArrayOf(0, 1, -1, 0x1234),
            4,
        )

        assertArrayEquals(
            byteArrayOf(0, 0, 1, 0, -1, -1, 0x34, 0x12),
            encoded,
        )
    }

    @Test
    fun `native buffer is large enough and sample aligned`() {
        assertEquals(6_400, VoiceAudioContract.nativeBufferSize(1_024))
        assertEquals(10_002, VoiceAudioContract.nativeBufferSize(5_001))
    }
}
