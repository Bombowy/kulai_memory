package com.kulai.memory.transport

import com.kulai.memory.models.*
import com.kulai.memory.protocol.MemoryJson
import com.kulai.memory.protocol.ProtocolException
import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.suspendCancellableCoroutine
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import okhttp3.*
import okhttp3.HttpUrl.Companion.toHttpUrl
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.RequestBody.Companion.toRequestBody
import org.json.JSONObject
import java.io.IOException
import java.nio.ByteBuffer
import java.nio.charset.CodingErrorAction
import java.util.UUID
import java.util.concurrent.TimeUnit
import kotlin.coroutines.resume
import kotlin.coroutines.resumeWithException

interface MemoryApi {
    suspend fun listMemories(archived: Boolean, limit: Int = 20, cursor: MemoryHistoryCursor? = null): MemoryHistoryPage
    suspend fun getMemory(id: UUID): MemoryDetail
    suspend fun askMemory(query: String, topK: Int = 5): AssistantAnswer
}

enum class HttpFailure { UNAVAILABLE, INVALID_RESPONSE, NOT_FOUND, OPERATION_FAILED }
class MemoryHttpException(val failure: HttpFailure) : Exception(when (failure) {
    HttpFailure.UNAVAILABLE -> "Local server is unavailable."
    HttpFailure.INVALID_RESPONSE -> "The server response was invalid."
    HttpFailure.NOT_FOUND -> "Memory not found."
    HttpFailure.OPERATION_FAILED -> "The memory assistant could not complete the operation."
})

class MemoryHttpClient(
    baseUrl: String,
    private val client: OkHttpClient = OkHttpClient.Builder()
        .connectTimeout(10, TimeUnit.SECONDS).readTimeout(210, TimeUnit.SECONDS)
        .callTimeout(240, TimeUnit.SECONDS).build(),
) : MemoryApi {
    private val base = baseUrl.toHttpUrl()

    override suspend fun listMemories(archived: Boolean, limit: Int, cursor: MemoryHistoryCursor?): MemoryHistoryPage {
        require(limit in 1..100) { "Invalid history request." }
        val url = base.newBuilder().addPathSegments("api/memories")
            .addQueryParameter("archived", archived.toString()).addQueryParameter("limit", limit.toString())
        cursor?.let {
            url.addQueryParameter("before_created_at", it.beforeCreatedAt.toString())
                .addQueryParameter("before_id", it.beforeId.toString())
        }
        return parse(Request.Builder().url(url.build()).build(), MemoryJson::history)
    }

    override suspend fun getMemory(id: UUID): MemoryDetail = parse(
        Request.Builder().url(base.newBuilder().addPathSegments("api/memories/$id").build()).build(),
        MemoryJson::detail,
    )

    override suspend fun askMemory(query: String, topK: Int): AssistantAnswer {
        require(query.isNotBlank() && query.length <= 10000 && topK in 1..20) { "Invalid assistant request." }
        val body = JSONObject().put("query", query).put("top_k", topK).toString()
            .toRequestBody("application/json; charset=utf-8".toMediaType())
        return parse(Request.Builder().url(base.newBuilder().addPathSegments("api/assistant/ask").build())
            .post(body).build(), MemoryJson::answer)
    }

    private suspend fun <T> parse(request: Request, parse: (String) -> T): T {
        val bytes = body(request)
        return try {
            withContext(Dispatchers.IO) {
                val text = Charsets.UTF_8.newDecoder().onMalformedInput(CodingErrorAction.REPORT)
                    .onUnmappableCharacter(CodingErrorAction.REPORT).decode(ByteBuffer.wrap(bytes)).toString()
                parse(text)
            }
        } catch (cancel: CancellationException) { throw cancel }
        catch (_: Exception) { throw MemoryHttpException(HttpFailure.INVALID_RESPONSE) }
    }

    /** Read and close on OkHttp's worker; resume only with owned bytes, never a live Response. */
    private suspend fun body(request: Request): ByteArray = suspendCancellableCoroutine { continuation ->
        val call = client.newCall(request)
        continuation.invokeOnCancellation { call.cancel() }
        call.enqueue(object : Callback {
            override fun onFailure(call: Call, e: IOException) {
                if (continuation.isActive) continuation.resumeWithException(MemoryHttpException(HttpFailure.UNAVAILABLE))
            }
            override fun onResponse(call: Call, response: Response) {
                try {
                    val bytes = response.use {
                        if (!it.isSuccessful) throw MemoryHttpException(when (it.code) {
                            404 -> HttpFailure.NOT_FOUND
                            503 -> HttpFailure.UNAVAILABLE
                            else -> HttpFailure.OPERATION_FAILED
                        })
                        val body = it.body ?: throw ProtocolException("Invalid response.")
                        val source = body.source()
                        source.request(MAX_RESPONSE_BYTES + 1)
                        val bytes = source.buffer.readByteArray()
                        if (bytes.size > MAX_RESPONSE_BYTES) throw ProtocolException("Invalid response.")
                        bytes
                    }
                    if (continuation.isActive) continuation.resume(bytes)
                } catch (error: Exception) {
                    val safe = if (error is MemoryHttpException) error else
                        MemoryHttpException(if (error is IOException) HttpFailure.UNAVAILABLE else HttpFailure.INVALID_RESPONSE)
                    if (continuation.isActive) continuation.resumeWithException(safe)
                }
            }
        })
    }

    companion object { private const val MAX_RESPONSE_BYTES = 2L * 1024 * 1024 }
}
