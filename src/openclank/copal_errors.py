"""Storage-independent Copal failures and guarded mutation outcomes."""


class CopalBridgeError(RuntimeError):
    def __init__(self, message: str, *, mutation_outcome: str | None = None):
        super().__init__(message)
        # Only an explicit provider guarantee can establish non-commit.
        self.mutation_outcome = mutation_outcome if mutation_outcome == "NotCommitted" else "Unknown"
