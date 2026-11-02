"""Quiet days: fixed national holidays, Good Friday (it moves every year) and the city's holidays."""
from datetime import date, datetime, time, timezone

import pytest
from pydantic import ValidationError

from agentplatform.core.config.schema import SendingConfig
from agentplatform.core.outbox.dispatcher import DispatchPolicy, _easter, is_national_holiday


@pytest.mark.parametrize("year,month,day", [(2024, 3, 31), (2025, 4, 20), (2026, 4, 5), (2027, 3, 28), (2028, 4, 16), (2030, 4, 21), (2038, 4, 25)])
def test_easter_matches_the_published_dates(year, month, day):
    assert _easter(year) == date(year, month, day)


def test_good_friday_is_a_holiday_and_moves_with_the_year():
    assert is_national_holiday(date(2027, 3, 26)) and is_national_holiday(date(2028, 4, 14))
    assert not is_national_holiday(date(2027, 3, 25)) and not is_national_holiday(date(2028, 3, 26))
    assert is_national_holiday(date(2027, 9, 7))


def test_nothing_proactive_goes_out_on_good_friday():
    policy = DispatchPolicy()
    noon_utc = datetime(2027, 3, 26, 15, 0, tzinfo=timezone.utc)      # 12:00 in Sao Paulo, outside quiet hours
    assert policy.is_quiet(noon_utc)
    assert policy.next_allowed(datetime(2027, 3, 26, 2, 0, tzinfo=timezone.utc)).date() == date(2027, 3, 27)
    assert not DispatchPolicy(holidays_national=False).is_quiet(noon_utc)


def test_city_holidays_every_year_and_once():
    policy = DispatchPolicy(holidays_national=False, holidays_local=("05-20", "2027-06-03"))
    assert policy.is_holiday(date(2027, 5, 20)) and policy.is_holiday(date(2031, 5, 20))
    assert policy.is_holiday(date(2027, 6, 3)) and not policy.is_holiday(date(2028, 6, 3))
    assert policy.next_allowed(datetime(2027, 5, 20, 2, 0, tzinfo=timezone.utc)).date() == date(2027, 5, 21)


def test_local_holidays_are_validated_and_versions_already_stored_still_load():
    assert SendingConfig(holidays_local=["02-29", "2027-06-03"]).holidays_local == ["02-29", "2027-06-03"]
    for bad in ("13-01", "dia 8", "2027-13-01"):
        with pytest.raises(ValidationError):
            SendingConfig(holidays_local=[bad])
    assert SendingConfig(holidays_local_calendar=None).quiet_hours.start == time(22, 0)
