from contextlib import AbstractContextManager


class DebugState(AbstractContextManager[None]):
    def __init__(self):
        self._entered = 0

    def __bool__(self):
        return self._entered > 0

    def __enter__(self) -> None:
        self._entered += 1

    def __exit__(self, _, __, ___) -> None:
        self._entered -= 1


debug = DebugState()
