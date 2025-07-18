from contextlib import AbstractContextManager

from localpost._utils import unwrap_exc


class DebugState(AbstractContextManager[None, None]):
    def __init__(self):
        self._entered = 0

    def __bool__(self) -> bool:
        return self._entered > 0

    def __enter__(self) -> None:
        self._entered += 1

    async def __aenter__(self) -> None:
        return self.__enter__()

    def __exit__(self, exc_type, exc_value: BaseException | None, traceback) -> None:
        self._entered -= 1
        if exc_value and self._entered == 0:
            source_exc = unwrap_exc(exc_value)
            if source_exc != exc_value:
                # Re-raise the original exception for better debugging
                raise source_exc from source_exc.__cause__

    async def __aexit__(self, exc_type, exc_value: BaseException | None, traceback) -> None:
        return self.__exit__(exc_type, exc_value, traceback)


debug = DebugState()
