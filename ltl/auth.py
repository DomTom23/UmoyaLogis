"""Proste wspólne hasło do aplikacji (zmienna środowiskowa APP_PASSWORD) - ochrona na sieć biurową."""
from __future__ import annotations

import hmac


def password_ok(entered: str, expected: str) -> bool:
    """Porównanie w stałym czasie; puste oczekiwane hasło = brak ochrony (wtedy nie wywołujemy tej funkcji)."""
    return bool(expected) and hmac.compare_digest(entered.encode(), expected.encode())
