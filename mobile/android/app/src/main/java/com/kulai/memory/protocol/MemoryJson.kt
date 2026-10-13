package com.kulai.memory.protocol

import com.kulai.memory.models.*
import kotlinx.serialization.json.*
import java.time.OffsetDateTime
import java.util.UUID

/** One strict projection for HTTP answers and assistant.completed. No response repair. */
object MemoryJson {
    fun speech(raw: String, answer: String): SpeechPlan {
        val root = StrictJson.objectFrom(raw)
        StrictJson.keys(root, "segments")
        val elements = StrictJson.array(root, "segments")
        if (elements.size !in 1..64) StrictJson.invalid()
        val segments = elements.map {
            val segment = StrictJson.objectValue(it)
            StrictJson.keys(segment, "text", "language")
            val text = StrictJson.string(segment, "text")
            if (text.isEmpty()) StrictJson.invalid()
            val language = when (StrictJson.string(segment, "language")) {
                "pl" -> SpeechLanguage.PL
                "en" -> SpeechLanguage.EN
                else -> StrictJson.invalid()
            }
            SpeechSegment(text, language)
        }
        return try { SpeechPlan(segments).validate(answer) } catch (_: IllegalArgumentException) { StrictJson.invalid() }
    }

    fun history(raw: String): MemoryHistoryPage {
        val root = StrictJson.objectFrom(raw)
        StrictJson.keys(root, "items", "next_cursor")
        val items = StrictJson.array(root, "items").map { element ->
            val item = StrictJson.objectValue(element)
            StrictJson.keys(item, "id", "created_at", "revision", "archived", "content_preview")
            val preview = StrictJson.string(item, "content_preview")
            if (preview.codePointCount(0, preview.length) > 200) StrictJson.invalid()
            MemoryHistoryItem(StrictJson.uuid(item, "id"), StrictJson.date(item, "created_at"),
                StrictJson.positiveInt(item, "revision"), StrictJson.boolean(item, "archived"), preview)
        }
        if (items.size > 100 || items.map { it.id }.distinct().size != items.size) StrictJson.invalid()
        val cursor = if (root["next_cursor"] == JsonNull) null else {
            val value = StrictJson.objectValue(root["next_cursor"])
            StrictJson.keys(value, "before_created_at", "before_id")
            MemoryHistoryCursor(StrictJson.date(value, "before_created_at"), StrictJson.uuid(value, "before_id"))
        }
        return MemoryHistoryPage(items, cursor)
    }

    fun detail(raw: String): MemoryDetail {
        val root = StrictJson.objectFrom(raw)
        StrictJson.keys(root, "id", "created_at", "revision", "archived", "content")
        return MemoryDetail(StrictJson.uuid(root, "id"), StrictJson.date(root, "created_at"),
            StrictJson.positiveInt(root, "revision"), StrictJson.boolean(root, "archived"),
            StrictJson.string(root, "content"))
    }

    fun answer(raw: String): AssistantAnswer = answer(StrictJson.objectFrom(raw))

    fun answer(root: JsonObject): AssistantAnswer {
        StrictJson.keys(root, "status", "answer", "citations")
        val status = when (StrictJson.string(root, "status")) {
            "answered" -> AssistantStatus.ANSWERED
            "insufficient_context" -> AssistantStatus.INSUFFICIENT_CONTEXT
            else -> StrictJson.invalid()
        }
        val answer = StrictJson.string(root, "answer")
        if (answer.isBlank()) StrictJson.invalid()
        val citations = StrictJson.array(root, "citations").map { element ->
            val citation = StrictJson.objectValue(element)
            StrictJson.keys(citation, "memory_id", "rank", "score")
            AssistantCitation(StrictJson.uuid(citation, "memory_id"),
                StrictJson.positiveInt(citation, "rank"), StrictJson.finiteDouble(citation, "score"))
        }
        if ((status == AssistantStatus.ANSWERED && citations.isEmpty()) ||
            (status == AssistantStatus.INSUFFICIENT_CONTEXT && citations.isNotEmpty()) ||
            citations.map { it.memoryId }.distinct().size != citations.size) StrictJson.invalid()
        return AssistantAnswer(status, answer, citations)
    }
}

internal object StrictJson {
    private val json = Json { isLenient = false }
    fun invalid(): Nothing = throw ProtocolException("The server response was invalid.")
    fun objectFrom(raw: String): JsonObject = try {
        objectValue(json.parseToJsonElement(raw))
    } catch (_: Exception) { invalid() }
    fun objectValue(value: JsonElement?): JsonObject = value as? JsonObject ?: invalid()
    fun keys(value: JsonObject, vararg expected: String) {
        if (value.keys != expected.toSet()) invalid()
    }
    fun array(value: JsonObject, key: String): JsonArray = value[key] as? JsonArray ?: invalid()
    private fun primitive(value: JsonObject, key: String): JsonPrimitive =
        (value[key] as? JsonPrimitive)?.takeUnless { it == JsonNull } ?: invalid()
    fun string(value: JsonObject, key: String): String =
        primitive(value, key).let { if (!it.isString) invalid() else it.content }
    fun positiveInt(value: JsonObject, key: String): Int = integer(value, key).also { if (it < 1) invalid() }
    fun integer(value: JsonObject, key: String): Int = primitive(value, key).let {
        if (it.isString) invalid() else it.intOrNull ?: invalid()
    }
    fun boolean(value: JsonObject, key: String): Boolean = primitive(value, key).let {
        if (it.isString) invalid() else it.booleanOrNull ?: invalid()
    }
    fun finiteDouble(value: JsonObject, key: String): Double = primitive(value, key).let {
        if (it.isString) invalid()
        (it.doubleOrNull ?: invalid()).also { number -> if (!number.isFinite()) invalid() }
    }
    fun uuid(value: JsonObject, key: String): UUID = try {
        val raw = string(value, key)
        UUID.fromString(raw).also { if (!it.toString().equals(raw, ignoreCase = true)) invalid() }
    } catch (_: Exception) { invalid() }
    fun date(value: JsonObject, key: String): OffsetDateTime = try {
        OffsetDateTime.parse(string(value, key))
    } catch (_: Exception) { invalid() }
}
