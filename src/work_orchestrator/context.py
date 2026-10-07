from .runtime import Runtime


def compile_context(project, limit=20, max_chars=12000):
    return Runtime(project).compile_context(limit=limit, max_chars=max_chars)
