package com.kulai.memory.audio

import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.asStateFlow

enum class CaptureOwner { NOTE, QUESTION }

/** Reserve before opening a socket; release only after the native capture has drained. */
class MicrophoneGate {
    private val mutableOwner = MutableStateFlow<CaptureOwner?>(null)
    val owner = mutableOwner.asStateFlow()
    private var active: Lease? = null

    @Synchronized
    fun acquire(owner: CaptureOwner): Lease? {
        if (active != null) return null
        return Lease(this, owner).also { active = it; mutableOwner.value = owner }
    }
    @Synchronized
    private fun release(lease: Lease) {
        if (active === lease) { active = null; mutableOwner.value = null }
    }
    class Lease internal constructor(private val gate: MicrophoneGate, val owner: CaptureOwner) {
        fun release() = gate.release(this)
    }
}
