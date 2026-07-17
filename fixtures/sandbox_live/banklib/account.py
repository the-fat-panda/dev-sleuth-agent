class Account:
    def __init__(self) -> None:
        self.holds: list[str] = []

    def close(self) -> float:
        return 1 / len(self.holds)
