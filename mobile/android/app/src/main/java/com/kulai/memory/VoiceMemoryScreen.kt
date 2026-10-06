package com.kulai.memory

import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.text.selection.SelectionContainer
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.Button
import androidx.compose.material3.Card
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.Surface
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import androidx.lifecycle.compose.collectAsStateWithLifecycle
import java.util.Locale

@Composable
fun KulAIMemoryTheme(content: @Composable () -> Unit) {
    MaterialTheme(
        colorScheme = MaterialTheme.colorScheme.copy(
            primary = Color(0xFF795548),
            secondary = Color(0xFF5D4037),
            surface = Color(0xFFFFF8F5),
            background = Color(0xFFFFF8F5),
        ),
        content = content,
    )
}

@Composable
fun VoiceMemoryScreen(
    viewModel: VoiceMemoryViewModel,
    requestMicrophonePermission: () -> Unit,
) {
    val state by viewModel.state.collectAsStateWithLifecycle()
    VoiceMemoryContent(
        state = state,
        onRecord = viewModel::record,
        onStop = viewModel::stop,
        onRetrySave = viewModel::retrySave,
        onRetryNote = viewModel::retryNote,
        onRequestPermission = requestMicrophonePermission,
    )
}

@Composable
internal fun VoiceMemoryContent(
    state: VoiceMemoryUiState,
    onRecord: () -> Unit,
    onStop: () -> Unit,
    onRetrySave: () -> Unit,
    onRetryNote: () -> Unit,
    onRequestPermission: () -> Unit,
) {
    Surface(modifier = Modifier.fillMaxSize()) {
        Column(
            modifier = Modifier
                .fillMaxSize()
                .verticalScroll(rememberScrollState())
                .padding(24.dp),
            verticalArrangement = Arrangement.spacedBy(16.dp),
        ) {
            Text(
                text = "KulAI Memory",
                style = MaterialTheme.typography.headlineMedium,
                fontWeight = FontWeight.Bold,
            )
            Text(
                text = "Local server via ADB reverse",
                style = MaterialTheme.typography.bodyMedium,
                color = MaterialTheme.colorScheme.onSurfaceVariant,
            )

            Card(modifier = Modifier.fillMaxWidth()) {
                Column(
                    modifier = Modifier.padding(18.dp),
                    verticalArrangement = Arrangement.spacedBy(8.dp),
                ) {
                    Text("Status", fontWeight = FontWeight.SemiBold)
                    Text(statusLabel(state.phase))
                    state.statusDetail?.let { detail ->
                        Text(
                            detail,
                            color = MaterialTheme.colorScheme.onSurfaceVariant,
                            style = MaterialTheme.typography.bodySmall,
                        )
                    }
                    if (state.phase == VoiceMemoryPhase.RECORDING) {
                        Text(
                            text = formatElapsed(state.elapsedMillis),
                            style = MaterialTheme.typography.titleLarge,
                        )
                    }
                }
            }

            if (!state.permissionGranted) {
                OutlinedButton(
                    onClick = onRequestPermission,
                    modifier = Modifier.fillMaxWidth(),
                ) {
                    Text("ZEZWÓL NA MIKROFON")
                }
            }

            Row(
                modifier = Modifier.fillMaxWidth(),
                horizontalArrangement = Arrangement.spacedBy(12.dp),
                verticalAlignment = Alignment.CenterVertically,
            ) {
                Button(
                    onClick = onRecord,
                    enabled = state.canRecord,
                    modifier = Modifier.weight(1f).height(64.dp),
                ) {
                    Text("NAGRAJ")
                }
                Button(
                    onClick = onStop,
                    enabled = state.canStop,
                    modifier = Modifier.weight(1f).height(64.dp),
                ) {
                    Text("STOP")
                }
            }

            if (state.canRetrySave) {
                Button(onClick = onRetrySave, modifier = Modifier.fillMaxWidth()) {
                    Text("RETRY SAVE")
                }
            }
            if (state.canRetryNote) {
                Button(onClick = onRetryNote, modifier = Modifier.fillMaxWidth()) {
                    Text("RETRY NOTE")
                }
            }

            Text("Transcript", fontWeight = FontWeight.SemiBold)
            Card(modifier = Modifier.fillMaxWidth()) {
                SelectionContainer {
                    Text(
                        text = state.transcript.ifEmpty { "Transcript will appear here." },
                        modifier = Modifier.padding(18.dp).fillMaxWidth(),
                        color = if (state.transcript.isEmpty()) {
                            MaterialTheme.colorScheme.onSurfaceVariant
                        } else {
                            MaterialTheme.colorScheme.onSurface
                        },
                    )
                }
            }
            Spacer(modifier = Modifier.height(8.dp))
        }
    }
}

private fun statusLabel(phase: VoiceMemoryPhase): String = when (phase) {
    VoiceMemoryPhase.IDLE -> "Ready"
    VoiceMemoryPhase.CONNECTING -> "Connecting"
    VoiceMemoryPhase.READY -> "Ready"
    VoiceMemoryPhase.RECORDING -> "Recording"
    VoiceMemoryPhase.WAITING_FOR_TRANSCRIPT -> "Transcribing"
    VoiceMemoryPhase.SAVING -> "Saving"
    VoiceMemoryPhase.SAVED -> "Saved"
    VoiceMemoryPhase.NO_SPEECH -> "No speech detected"
    VoiceMemoryPhase.RETRY_SAVE -> "Retry save available"
    VoiceMemoryPhase.RETRY_NOTE -> "Retry note available"
    VoiceMemoryPhase.ERROR -> "Error"
}

private fun formatElapsed(milliseconds: Long): String {
    val totalSeconds = milliseconds / 1000
    return String.format(
        Locale.ROOT,
        "%02d:%02d",
        totalSeconds / 60,
        totalSeconds % 60,
    )
}
