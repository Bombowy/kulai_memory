package com.kulai.memory.audio

import android.Manifest
import android.annotation.SuppressLint
import android.content.Context
import android.content.pm.PackageManager
import android.media.AudioFormat
import android.media.AudioRecord
import android.media.MediaRecorder
import androidx.core.content.ContextCompat
import java.util.concurrent.atomic.AtomicBoolean

enum class RecordingEnd {
    STOPPED,
    LIMIT_REACHED,
}

interface VoiceAudioRecorder {
    suspend fun record(
        onStarted: () -> Unit,
        onPcm: suspend (ByteArray) -> Boolean,
    ): RecordingEnd
    fun requestStop()
}

fun interface AudioRecordFactory {
    fun create(bufferSizeBytes: Int): AudioRecord
}

class AndroidVoiceAudioRecorder(
    private val context: Context,
    private val minBufferSize: () -> Int = {
        AudioRecord.getMinBufferSize(
            VoiceAudioContract.SAMPLE_RATE_HZ,
            AudioFormat.CHANNEL_IN_MONO,
            AudioFormat.ENCODING_PCM_16BIT,
        )
    },
    private val factory: AudioRecordFactory? = null,
) : VoiceAudioRecorder {
    @SuppressLint("MissingPermission")
    private fun createRecorder(bufferSizeBytes: Int): AudioRecord =
        AudioRecord.Builder()
            .setAudioSource(MediaRecorder.AudioSource.VOICE_RECOGNITION)
            .setAudioFormat(
                AudioFormat.Builder()
                    .setEncoding(AudioFormat.ENCODING_PCM_16BIT)
                    .setSampleRate(VoiceAudioContract.SAMPLE_RATE_HZ)
                    .setChannelMask(AudioFormat.CHANNEL_IN_MONO)
                    .build(),
            ).setBufferSizeInBytes(bufferSizeBytes)
            .build()

    private val stopRequested = AtomicBoolean(false)
    private val active = AtomicBoolean(false)

    override suspend fun record(
        onStarted: () -> Unit,
        onPcm: suspend (ByteArray) -> Boolean,
    ): RecordingEnd {
        check(active.compareAndSet(false, true)) { "Audio recording is already active." }
        stopRequested.set(false)
        if (ContextCompat.checkSelfPermission(context, Manifest.permission.RECORD_AUDIO) !=
            PackageManager.PERMISSION_GRANTED
        ) {
            active.set(false)
            throw IllegalStateException("Microphone permission is unavailable.")
        }
        val bufferSize = VoiceAudioContract.nativeBufferSize(minBufferSize())
        val recorder = factory?.create(bufferSize) ?: createRecorder(bufferSize)
        if (recorder.state != AudioRecord.STATE_INITIALIZED) {
            recorder.release()
            active.set(false)
            throw IllegalStateException("Microphone could not be initialized.")
        }

        var totalBytes = 0
        val samples = ShortArray(VoiceAudioContract.FRAME_SAMPLES)
        try {
            recorder.startRecording()
            if (recorder.recordingState != AudioRecord.RECORDSTATE_RECORDING) {
                throw IllegalStateException("Microphone did not start recording.")
            }
            onStarted()
            while (!stopRequested.get()) {
                val read = recorder.read(
                    samples,
                    0,
                    samples.size,
                    AudioRecord.READ_BLOCKING,
                )
                if (read < 0) {
                    throw IllegalStateException("Microphone read failed.")
                }
                if (read == 0) {
                    continue
                }
                val remainingSamples =
                    (VoiceAudioContract.MAX_TOTAL_BYTES - totalBytes) /
                        VoiceAudioContract.BYTES_PER_SAMPLE
                val acceptedSamples = minOf(read, remainingSamples)
                if (acceptedSamples > 0) {
                    val pcm = VoiceAudioContract.encodeLittleEndian(samples, acceptedSamples)
                    check(pcm.size % 2 == 0 && pcm.size <= VoiceAudioContract.MAX_FRAME_BYTES)
                    if (!onPcm(pcm)) {
                        throw IllegalStateException("Audio transport is unavailable.")
                    }
                    totalBytes += pcm.size
                }
                if (totalBytes >= VoiceAudioContract.MAX_TOTAL_BYTES) {
                    return RecordingEnd.LIMIT_REACHED
                }
            }
            return RecordingEnd.STOPPED
        } finally {
            try {
                if (recorder.recordingState == AudioRecord.RECORDSTATE_RECORDING) {
                    recorder.stop()
                }
            } finally {
                recorder.release()
                active.set(false)
                stopRequested.set(false)
            }
        }
    }

    override fun requestStop() {
        stopRequested.set(true)
    }
}
