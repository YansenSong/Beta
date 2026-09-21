class DurableRuntimeError(Exception):
    pass


class DurableStateConflict(DurableRuntimeError):
    pass


class DurableCorruption(DurableRuntimeError):
    pass


class DurableOperationBusy(DurableRuntimeError):
    pass


class DurableStaleOperation(DurableRuntimeError):
    pass


class UnsupportedDurableVersion(DurableRuntimeError):
    pass
