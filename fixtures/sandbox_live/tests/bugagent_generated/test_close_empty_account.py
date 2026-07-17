from banklib import Account


def test_closing_empty_account_is_safe() -> None:
    Account().close()
