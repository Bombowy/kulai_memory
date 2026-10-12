"""Access the single runtime established by the existing router lifespan."""
from kulai_memory.server.runtime import ServerRuntimeError


def server_runtime(app):
    state = getattr(app.state, "voice_memory_runtime", None)
    runtime = getattr(state, "runtime", None)
    if runtime is None:
        raise ServerRuntimeError()
    return runtime
