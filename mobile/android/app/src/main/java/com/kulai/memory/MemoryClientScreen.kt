package com.kulai.memory

import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.*
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.text.selection.SelectionContainer
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.ui.Modifier
import androidx.compose.ui.unit.dp
import androidx.lifecycle.compose.collectAsStateWithLifecycle
import com.kulai.memory.models.MemoryDetail
import java.time.format.DateTimeFormatter
import java.util.Locale

@Composable
fun MemoryClientScreen(viewModel: VoiceMemoryViewModel, requestMicrophonePermission: () -> Unit) {
    val tab by viewModel.root.tab.collectAsStateWithLifecycle()
    val note by viewModel.state.collectAsStateWithLifecycle()
    val ask by viewModel.assistant.state.collectAsStateWithLifecycle()
    val history by viewModel.history.state.collectAsStateWithLifecycle()
    val capture by viewModel.microphone.owner.collectAsStateWithLifecycle()
    val speech by viewModel.speech.state.collectAsStateWithLifecycle()
    val controls = memoryClientControls(note, ask, capture)
    Scaffold(bottomBar = {
        NavigationBar {
            MemoryTab.entries.forEach { destination ->
                NavigationBarItem(selected = tab == destination,
                    onClick = { viewModel.root.selectTab(destination) },
                    icon = { Text(when (destination) { MemoryTab.NOTE -> "●"; MemoryTab.ASK -> "?"; MemoryTab.MEMORY -> "≡" }) },
                    label = { Text(destination.name) })
            }
        }
    }) { padding ->
        Box(Modifier.fillMaxSize().padding(padding)) {
            when (tab) {
                MemoryTab.NOTE -> VoiceMemoryContent(note, viewModel::record, viewModel::stop,
                    viewModel::retrySave, viewModel::retryNote, requestMicrophonePermission, controls.noteRecordEnabled)
                MemoryTab.ASK -> AssistantContent(ask, controls, viewModel.assistant::setQuery,
                    viewModel.root::ask, viewModel.root::startVoiceQuestion,
                    viewModel.assistant::stopVoiceQuestion, viewModel.assistant::cancelQuestion, requestMicrophonePermission,
                    speech, viewModel.root::speakAnswer, viewModel.root::stopAudio, viewModel.speech::setAutoSpeak,
                    { viewModel.root.answerRendered(ask.answerVersion) }, capture == null)
                MemoryTab.MEMORY -> HistoryContent(history, viewModel.history::setFilter,
                    viewModel.history::refresh, viewModel.history::loadMore, viewModel.history::selectMemory,
                    viewModel.history::clearDetail)
            }
        }
    }
}

@Composable
internal fun AssistantContent(
    state: MemoryAssistantState, controls: MemoryClientControls,
    onQuery: (String) -> Unit, onAsk: () -> Unit, onVoice: () -> Unit, onStop: () -> Unit,
    onCancel: () -> Unit, onPermission: () -> Unit,
    speech: MemorySpeechState = MemorySpeechState(), onSpeak: () -> Unit = {}, onStopAudio: () -> Unit = {},
    onAutoSpeak: (Boolean) -> Unit = {}, onAnswerRendered: () -> Unit = {},
    speechAllowed: Boolean = true,
) {
    // Common post-render trigger for text/voice/insufficient; root consumes each answerVersion once.
    LaunchedEffect(state.answerVersion, state.result) { if (state.result != null) onAnswerRendered() }
    Column(Modifier.fillMaxSize().verticalScroll(rememberScrollState()).padding(20.dp),
        verticalArrangement = Arrangement.spacedBy(12.dp)) {
        Text("Ask Memory", style = MaterialTheme.typography.headlineMedium)
        OutlinedTextField(value = state.query, onValueChange = onQuery, modifier = Modifier.fillMaxWidth(),
            enabled = !state.voiceBusy, label = { Text("Ask something about your memories...") },
            isError = state.query.length > 10000, maxLines = 6)
        Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
            Button(onClick = onAsk, enabled = controls.textAskEnabled) { Text("ASK") }
            Button(onClick = if (state.phase == AssistantPhase.RECORDING) onStop else onVoice,
                enabled = state.phase == AssistantPhase.RECORDING || controls.voiceAskEnabled) {
                Text(if (state.phase == AssistantPhase.RECORDING) "STOP QUESTION" else "ASK BY VOICE")
            }
        }
        if (state.voiceBusy || state.phase == AssistantPhase.ASKING) {
            OutlinedButton(onClick = onCancel) { Text("CANCEL QUESTION") }
        }
        if (!state.permissionGranted) OutlinedButton(onClick = onPermission) { Text("ALLOW MICROPHONE") }
        Text(assistantStatusLabel(state.phase))
        state.error?.let { Text(it, color = MaterialTheme.colorScheme.error) }
        if (state.voiceTranscript.isNotEmpty()) {
            Text("Recognized question", style = MaterialTheme.typography.titleMedium)
            SelectionContainer { Text(state.voiceTranscript) }
        }
        state.result?.let { result ->
            Text("Answer", style = MaterialTheme.typography.titleMedium)
            SelectionContainer { Text(result.answer) }
            if (result.citations.isNotEmpty()) {
                Text("Sources", style = MaterialTheme.typography.titleMedium)
                result.citations.forEach { citation ->
                    Text("${citation.rank}. ${citation.memoryId}\nScore: ${String.format(Locale.ROOT, "%.4f", citation.score)}")
                }
            }
        }
        Text("Polish / English speech", style = MaterialTheme.typography.labelMedium)
        Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
            Button(onClick = onSpeak, enabled = state.result != null && speech.available && !speech.busy && speechAllowed) { Text("SPEAK") }
            OutlinedButton(onClick = onStopAudio, enabled = speech.busy) { Text("STOP AUDIO") }
        }
        Row {
            Checkbox(checked = speech.autoSpeak, onCheckedChange = onAutoSpeak)
            Text("Speak answers automatically", modifier = Modifier.padding(top = 12.dp))
        }
        Text(when (speech.phase) {
            SpeechPhase.UNAVAILABLE -> "TTS unavailable"
            SpeechPhase.READY -> "Speech ready"
            SpeechPhase.PLANNING -> "Preparing speech..."
            SpeechPhase.SPEAKING -> "Speaking..."
            SpeechPhase.STOPPING -> "Stopping audio..."
            SpeechPhase.ERROR -> "Could not speak answer."
        }, style = MaterialTheme.typography.bodySmall)
        speech.voices?.let { voices ->
            Text("PL (offline): ${voices.polish.id}\nEN (offline): ${voices.english.id}", style = MaterialTheme.typography.bodySmall)
        }
    }
}

internal fun assistantStatusLabel(phase: AssistantPhase): String = when (phase) {
    AssistantPhase.IDLE -> "Ready"
    AssistantPhase.ASKING -> "Asking memory..."
    AssistantPhase.CONNECTING -> "Connecting..."
    AssistantPhase.RECORDING -> "Listening..."
    AssistantPhase.TRANSCRIBING -> "Transcribing question..."
    AssistantPhase.SEARCHING -> "Searching memory..."
    AssistantPhase.ANSWERED -> "Answered"
    AssistantPhase.INSUFFICIENT_CONTEXT -> "Not enough information"
    AssistantPhase.NO_SPEECH -> "No speech detected"
    AssistantPhase.ERROR -> "Question could not be completed"
}

@Composable
internal fun HistoryContent(state: MemoryHistoryState, onFilter: (MemoryFilter) -> Unit,
    onRefresh: () -> Unit, onMore: () -> Unit, onDetail: (java.util.UUID) -> Unit, onBack: () -> Unit) {
    val detail = state.selectedDetail
    if (detail != null) { MemoryDetailContent(detail, onBack); return }
    Column(Modifier.fillMaxSize().padding(20.dp), verticalArrangement = Arrangement.spacedBy(12.dp)) {
        Text("Memory", style = MaterialTheme.typography.headlineMedium)
        Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
            MemoryFilter.entries.forEach { filter ->
                FilterChip(selected = state.filter == filter, onClick = { onFilter(filter) }, label = { Text(filter.name) })
            }
            OutlinedButton(onClick = onRefresh, enabled = !state.refreshing) { Text("REFRESH") }
        }
        state.error?.let { Text(it, color = MaterialTheme.colorScheme.error) }
        if (state.loading || state.detailLoading) LinearProgressIndicator(Modifier.fillMaxWidth())
        LazyColumn(Modifier.weight(1f), verticalArrangement = Arrangement.spacedBy(10.dp)) {
            items(state.items, key = { it.id.toString() }) { item ->
                Card(Modifier.fillMaxWidth().clickable { onDetail(item.id) }) {
                    Column(Modifier.padding(14.dp), verticalArrangement = Arrangement.spacedBy(6.dp)) {
                        Text(item.createdAt.format(DateTimeFormatter.ISO_OFFSET_DATE_TIME), style = MaterialTheme.typography.bodySmall)
                        Text("Revision ${item.revision} · ${if (item.archived) "Archived" else "Active"}")
                        Text(item.contentPreview)
                    }
                }
            }
            if (!state.loading && state.items.isEmpty()) item { Text("No memories in this view.") }
            if (state.nextCursor != null) item {
                Button(onClick = onMore, enabled = !state.loadingMore && !state.loading, modifier = Modifier.fillMaxWidth()) {
                    Text(if (state.loadingMore) "Loading..." else "LOAD MORE")
                }
            }
        }
    }
}

@Composable
private fun MemoryDetailContent(detail: MemoryDetail, onBack: () -> Unit) {
    Column(Modifier.fillMaxSize().verticalScroll(rememberScrollState()).padding(20.dp),
        verticalArrangement = Arrangement.spacedBy(12.dp)) {
        OutlinedButton(onClick = onBack) { Text("BACK") }
        Text("Memory detail", style = MaterialTheme.typography.headlineMedium)
        Text(detail.id.toString())
        Text(detail.createdAt.format(DateTimeFormatter.ISO_OFFSET_DATE_TIME))
        Text("Revision ${detail.revision} · ${if (detail.archived) "Archived" else "Active"}")
        SelectionContainer { Text(detail.content) }
    }
}
