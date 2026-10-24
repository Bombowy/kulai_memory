package com.kulai.memory

import android.Manifest
import android.content.pm.PackageManager
import android.os.Bundle
import android.view.WindowManager
import androidx.activity.ComponentActivity
import androidx.activity.compose.setContent
import androidx.activity.result.contract.ActivityResultContracts
import androidx.activity.viewModels
import androidx.core.content.ContextCompat

class MainActivity : ComponentActivity() {
    private val viewModel: VoiceMemoryViewModel by viewModels()

    private val microphonePermission =
        registerForActivityResult(ActivityResultContracts.RequestPermission()) { granted ->
            viewModel.setMicrophonePermission(granted)
        }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        window.setFlags(screenCaptureWindowFlags(BuildConfig.DEBUG), WindowManager.LayoutParams.FLAG_SECURE)
        viewModel.setMicrophonePermission(
            ContextCompat.checkSelfPermission(this, Manifest.permission.RECORD_AUDIO) ==
                PackageManager.PERMISSION_GRANTED,
        )
        setContent {
            KulAIMemoryTheme {
                MemoryClientScreen(
                    viewModel = viewModel,
                    requestMicrophonePermission = {
                        microphonePermission.launch(Manifest.permission.RECORD_AUDIO)
                    },
                )
            }
        }
    }

    override fun onResume() {
        super.onResume()
        viewModel.setMicrophonePermission(
            ContextCompat.checkSelfPermission(this, Manifest.permission.RECORD_AUDIO) ==
                PackageManager.PERMISSION_GRANTED,
        )
    }
}

internal fun screenCaptureWindowFlags(isDebug: Boolean): Int =
    if (isDebug) 0 else WindowManager.LayoutParams.FLAG_SECURE
