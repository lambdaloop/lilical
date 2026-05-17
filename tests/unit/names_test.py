import pytest

from lilical.utils.names import format_display_name


@pytest.mark.parametrize(
    "inp, expected",
    [
        (None, None),
        ("", ""),
        ("Schulze, Lisanne", "Lisanne Schulze"),
        ("Karashchuk, Lili", "Lili Karashchuk"),
        ("van der Berg, Jan", "Jan van der Berg"),
        ("Lili Karashchuk", "Lili Karashchuk"),          # already First Last
        ("foo@bar.com", "foo@bar.com"),                   # email — leave alone
        ("Smith, MD, John", "Smith, MD, John"),           # two commas — leave alone
        ("Tremblay,Marie", "Marie Tremblay"),             # no space after comma
        (",", ","),                                        # degenerate — leave alone
    ],
)
def test_format_display_name(inp, expected):
    assert format_display_name(inp) == expected
