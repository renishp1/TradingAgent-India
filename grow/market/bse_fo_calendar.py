"""Official BSE equity-derivatives trading-holiday calendar for SENSEX expiry classification.

Source alignment: BSE equity / equity-derivative holiday list for 2026 as published
via exchange holiday calendars (cross-checked with Zerodha market holiday calendar
for NSE+BSE 2026). This is intentionally separate from NSE ``fo_calendar`` —
do not reuse NSE holidays for BSE underlyings.

Covers 2026 only. Unknown years must fail closed in the expiry classifier.
"""

from __future__ import annotations

from datetime import date

BSE_FO_CALENDAR_VERSION = "bse.fo.2026.v1"
BSE_FO_HOLIDAY_SOURCE = "BSE equity derivative holidays 2026"
BSE_FO_CALENDAR_YEARS = frozenset({2026})

# BSE equity + equity-derivative trading holidays for 2026.
# Includes 15 Jan (MCGM election) which is a BSE holiday; NSE F&O circular
# NSE/FAOP/71777 does not list that date — another reason calendars stay separate.
BSE_FO_HOLIDAYS_2026 = frozenset(
    {
        date(2026, 1, 15),  # Municipal Corporation Elections (Maharashtra)
        date(2026, 1, 26),  # Republic Day
        date(2026, 3, 3),  # Holi
        date(2026, 3, 26),  # Shri Ram Navami
        date(2026, 3, 31),  # Shri Mahavir Jayanti
        date(2026, 4, 3),  # Good Friday
        date(2026, 4, 14),  # Dr. Baba Saheb Ambedkar Jayanti
        date(2026, 5, 1),  # Maharashtra Day
        date(2026, 5, 28),  # Bakri Id
        date(2026, 6, 26),  # Muharram
        date(2026, 9, 14),  # Ganesh Chaturthi
        date(2026, 10, 2),  # Mahatma Gandhi Jayanti
        date(2026, 10, 20),  # Dussehra
        date(2026, 11, 10),  # Diwali-Balipratipada
        date(2026, 11, 24),  # Prakash Gurpurb Sri Guru Nanak Dev
        date(2026, 12, 25),  # Christmas
    }
)
