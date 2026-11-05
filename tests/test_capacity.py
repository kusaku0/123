import pytest

from pricebot.search import relevant


@pytest.mark.parametrize("title", ["Apple iPhone 15 128GB", "Apple iPhone 15 128 ГБ", "iPhone 15 128 Gb"])
def test_capacity_spacing_and_units(title):
    assert relevant("iphone 15 128 GB", title)


def test_capacity_must_still_match():
    assert not relevant("iphone 15 128 GB", "Apple iPhone 15 256GB")
    assert not relevant("iphone 15 128 GB", "Apple iPhone 15 128MB")
