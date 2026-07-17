__all__ = ["YangjibaoClient"]


def __getattr__(name: str):
    if name != "YangjibaoClient":
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    from kunjin.adapters.yangjibao import YangjibaoClient

    globals()[name] = YangjibaoClient
    return YangjibaoClient
