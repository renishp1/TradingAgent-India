"""Official NSE F&O trading-holiday calendar for expiry classification.

Source: NSE circular NSE/FAOP/71777 dated 12 December 2025.
This is not the cash-session approximation in grow.market.session, and it is
not the F&O settlement-holiday list (NCL/CMPT/71923).
"""

from __future__ import annotations

from datetime import date

FO_CALENDAR_VERSION = "nse.fo.2026.v1"
FO_HOLIDAY_CIRCULAR = "NSE/FAOP/71777"

# Official 2026 NSE F&O trading holidays (NSE/FAOP/71777).
# Weekend-only observances are omitted: 2026-02-15, 2026-03-21, 2026-08-15,
# 2026-11-08 (Muhurat trading on 8 Nov). Jan 15 election is not in the circular.
# Settlement-holiday extras are omitted: 2026-02-19, 2026-03-19, 2026-04-01,
# 2026-08-26.
FO_HOLIDAYS_2026 = frozenset(
    {
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

# Documented non-trading / excluded dates. Not classified as F&O holidays.
FO_WEEKEND_OBSERVANCES_2026 = frozenset(
    {
        date(2026, 2, 15),  # Mahashivratri (Sunday)
        date(2026, 3, 21),  # Id-Ul-Fitr (Saturday)
        date(2026, 8, 15),  # Independence Day (Saturday)
        date(2026, 11, 8),  # Diwali Laxmi Pujan / Muhurat (Sunday)
    }
)
FO_EXCLUDED_DATES_2026 = frozenset(
    {
        date(2026, 1, 15),  # election — not in NSE/FAOP/71777
        date(2026, 2, 19),  # settlement holiday only
        date(2026, 3, 19),  # settlement holiday only
        date(2026, 4, 1),  # settlement holiday only
        date(2026, 8, 26),  # settlement holiday only
    }
)
