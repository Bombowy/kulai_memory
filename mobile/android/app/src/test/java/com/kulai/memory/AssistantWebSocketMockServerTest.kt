package com.kulai.memory

import com.kulai.memory.audio.MicrophoneGate
import com.kulai.memory.transport.*
import kotlinx.coroutines.*
import kotlinx.coroutines.flow.first
import okhttp3.*
import okhttp3.mockwebserver.*
import okio.ByteString
import org.json.JSONObject
import org.junit.Assert.*
import org.junit.Test
import java.util.concurrent.CopyOnWriteArrayList

class AssistantWebSocketMockServerTest {
    @Test fun `real OkHttp voice question exact ordered wire contract and answer`() = runBlocking {
        exercise(false, false)
    }
    @Test fun `real OkHttp insufficient question has no sources`() = runBlocking {
        exercise(true, false)
    }
    @Test fun `real OkHttp empty transcript closes normally without answer`() = runBlocking {
        exercise(false, true)
    }
    private suspend fun exercise(insufficient: Boolean, empty: Boolean) {
        val server = MockWebServer(); val commands = CopyOnWriteArrayList<String>(); val binary = CopyOnWriteArrayList<ByteArray>()
        val frameReceived = CompletableDeferred<Unit>()
        server.enqueue(MockResponse().withWebSocketUpgrade(object : WebSocketListener() {
            override fun onMessage(webSocket: WebSocket, text: String) {
                commands += text
                when (JSONObject(text).getString("type")) {
                    "question.start" -> webSocket.send(assistantEvent("session.ready", 1).value)
                    "question.stop" -> {
                        webSocket.send(assistantEvent("transcript.final", 2,
                            JSONObject().put("text", if (empty) "" else "Gdzie mieszka smok?").toString()).value)
                        if (!empty) {
                            webSocket.send(assistantEvent("rag.started", 3).value)
                            webSocket.send(assistantEvent("assistant.completed", 4, answerJson(if (insufficient) INSUFFICIENT else ANSWER)).value)
                        }
                        webSocket.close(1000, "complete")
                    }
                }
            }
            override fun onMessage(webSocket: WebSocket, bytes: ByteString) { binary += bytes.toByteArray(); frameReceived.complete(Unit) }
        }))
        server.start()
        val scope = CoroutineScope(SupervisorJob() + Dispatchers.Default)
        val client = OkHttpClient(); val api = FakeMemoryApi()
        val controller = MemoryAssistantController(scope, api, server.url("/ws/assistant").toString().replace("http:", "ws:"),
            OkHttpVoiceSocketFactory(client), QuestionRecorder(), MicrophoneGate())
        try {
            controller.setMicrophonePermission(true); controller.startVoiceQuestion()
            withTimeout(5000) { controller.state.first { it.phase == AssistantPhase.RECORDING }; frameReceived.await() }
            controller.stopVoiceQuestion()
            val expected = when { empty -> AssistantPhase.NO_SPEECH; insufficient -> AssistantPhase.INSUFFICIENT_CONTEXT; else -> AssistantPhase.ANSWERED }
            withTimeout(5000) { controller.state.first { it.phase == expected } }
            assertEquals(listOf("question.start", "question.stop"), commands.map { JSONObject(it).getString("type") })
            assertTrue(commands.none { it.contains("ingestion") || it.contains("retry") })
            assertArrayEquals(byteArrayOf(1, 2, 3, 4), binary.single())
            assertTrue(api.asks.isEmpty())
            if (empty) assertNull(controller.state.value.result)
            else assertEquals(if (insufficient) INSUFFICIENT else ANSWER, controller.state.value.result)
        } finally {
            controller.shutdown(); scope.cancel(); client.dispatcher.cancelAll(); client.connectionPool.evictAll()
            client.dispatcher.executorService.shutdown(); server.shutdown()
        }
    }
}
