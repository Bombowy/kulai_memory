package com.kulai.memory.audio

object VoiceAudioContract {
    const val SAMPLE_RATE_HZ = 16_000
    const val CHANNELS = 1
    const val BYTES_PER_SAMPLE = 2
    const val FRAME_SAMPLES = 3_200
    const val FRAME_BYTES = FRAME_SAMPLES * BYTES_PER_SAMPLE
    const val MAX_FRAME_BYTES = 256 * 1024
    const val MAX_DURATION_SECONDS = 10 * 60
    const val MAX_TOTAL_BYTES = SAMPLE_RATE_HZ * BYTES_PER_SAMPLE * MAX_DURATION_SECONDS

    fun nativeBufferSize(minBufferSize: Int): Int {
        require(minBufferSize > 0) { "AudioRecord minimum buffer is unavailable." }
        val doubled = Math.multiplyExact(minBufferSize, 2)
        val selected = maxOf(doubled, FRAME_BYTES)
        return if (selected % BYTES_PER_SAMPLE == 0) selected else selected + 1
    }

    fun encodeLittleEndian(samples: ShortArray, count: Int): ByteArray {
        require(count in 0..samples.size)
        val output = ByteArray(count * BYTES_PER_SAMPLE)
        for (index in 0 until count) {
            val sample = samples[index].toInt()
            output[index * 2] = (sample and 0xff).toByte()
            output[index * 2 + 1] = ((sample ushr 8) and 0xff).toByte()
        }
        return output
    }
}
