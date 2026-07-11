import feedsentry


def test_package_exposes_version() -> None:
    assert feedsentry.__version__ == "0.1.0"
