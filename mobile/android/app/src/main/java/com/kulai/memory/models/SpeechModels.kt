package com.kulai.memory.models

enum class SpeechLanguage { PL, EN }
data class SpeechSegment(val text: String, val language: SpeechLanguage) {
    init { require(text.isNotEmpty()) { "Invalid speech plan." } }
}
data class SpeechPlan(val segments: List<SpeechSegment>) {
    fun validate(answer: String): SpeechPlan {
        require(answer.isNotBlank() && answer.length <= 6000 && segments.size in 1..64 &&
            segments.all { it.text.isNotEmpty() } && segments.joinToString("") { it.text } == answer) {
            "Invalid speech plan."
        }
        return this
    }
}
