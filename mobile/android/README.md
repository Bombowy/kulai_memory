# KulAI Memory Android Alpha

This is the native debug client for WebSocket protocol v1. It requires a local
KulAI Memory backend and a debug-authorized physical Android device connected
over USB.

```text
gradlew.bat testDebugUnitTest
gradlew.bat assembleDebug
adb reverse tcp:8000 tcp:8000
adb reverse --list
adb install -r app\build\outputs\apk\debug\app-debug.apk
```

Start the backend separately from the repository root:

```text
.venv\Scripts\python.exe scripts\dev.py
```

The debug endpoint is `ws://127.0.0.1:8000/ws/memory`. The backend continues to
bind only to `127.0.0.1`. Debug cleartext support is not present in the main or
release manifest.
