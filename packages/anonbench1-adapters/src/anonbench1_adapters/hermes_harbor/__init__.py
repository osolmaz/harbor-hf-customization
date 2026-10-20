"""Native campaign plugin: common reasoning evidence and optional task backend."""


def register(ctx):
    from importlib import import_module

    register_reasoning = import_module(f"{__package__}.reasoning").register_reasoning

    if register_reasoning(ctx):
        from offline_hermes_native import register as register_task_backend

        register_task_backend(ctx)
