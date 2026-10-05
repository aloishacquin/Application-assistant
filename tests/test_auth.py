import pytest

from jobapply.web.auth import LoginThrottle, hash_password, safe_next, verify_password


def test_hash_and_verify() -> None:
    stored = hash_password("correct horse battery")
    assert stored.startswith("scrypt$")
    assert verify_password("correct horse battery", stored)
    assert not verify_password("wrong password", stored)
    assert hash_password("correct horse battery") != stored  # random salt


@pytest.mark.parametrize("stored", ["", "plain", "bcrypt$1$2$3$4$5", "scrypt$x$8$1$aa$bb"])
def test_malformed_hash_never_verifies(stored: str) -> None:
    assert not verify_password("anything", stored)


def test_throttle() -> None:
    throttle = LoginThrottle(max_failures=3, window_s=60)
    for _ in range(2):
        throttle.record_failure("1.2.3.4")
    assert not throttle.is_blocked("1.2.3.4")
    throttle.record_failure("1.2.3.4")
    assert throttle.is_blocked("1.2.3.4")
    assert not throttle.is_blocked("5.6.7.8")
    throttle.reset("1.2.3.4")
    assert not throttle.is_blocked("1.2.3.4")


def test_throttle_window_expires() -> None:
    throttle = LoginThrottle(max_failures=1, window_s=0)
    throttle.record_failure("ip")
    assert not throttle.is_blocked("ip")


@pytest.mark.parametrize(
    ("target", "expected"),
    [
        ("/offers/3", "/offers/3"),
        (None, "/"),
        ("https://evil.example", "/"),
        ("//evil.example", "/"),
    ],
)
def test_safe_next(target, expected) -> None:
    assert safe_next(target) == expected
