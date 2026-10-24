package com.kulai.memory

import android.view.WindowManager
import org.junit.Assert.assertEquals
import org.junit.Test

class ScreenCapturePolicyTest {
    @Test fun `debug allows standard screen capture`() {
        assertEquals(0, screenCaptureWindowFlags(isDebug = true))
    }

    @Test fun `release protects screen contents`() {
        assertEquals(WindowManager.LayoutParams.FLAG_SECURE, screenCaptureWindowFlags(isDebug = false))
    }

    @Test fun `current build type selects its capture policy`() {
        val expected = when (BuildConfig.BUILD_TYPE) {
            "debug" -> 0
            "release" -> WindowManager.LayoutParams.FLAG_SECURE
            else -> error("Unexpected build type")
        }
        assertEquals(expected, screenCaptureWindowFlags(BuildConfig.DEBUG))
    }
}
