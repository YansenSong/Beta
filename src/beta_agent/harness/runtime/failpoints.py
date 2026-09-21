from typing import Protocol

class InjectedCrash(RuntimeError):
    pass

class Failpoint(Protocol):
    async def hit(self, name: str) -> None: ...

class NoopFailpoint:
    async def hit(self, name: str) -> None:
        del name
