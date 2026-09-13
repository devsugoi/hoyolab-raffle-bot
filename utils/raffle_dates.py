"""Validate Gemini's extracted facts; scheduling policy lives here, not in AI."""
from dataclasses import dataclass
from datetime import datetime, time, timedelta, timezone
import re
from zoneinfo import ZoneInfo


@dataclass(frozen=True)
class RaffleDates:
    end_at: datetime | None
    winner_at: datetime | None
    reason: str

    @property
    def reminder_at(self):
        return self.winner_at or self.end_at


def validate_dates(data, *, published_at, zone, default_hour):
    if not isinstance(data, dict) or data.get('confidence') not in ('high', 'medium', 'low'):
        raise ValueError('Invalid date analysis schema')
    if not isinstance(data.get('reason'), str):
        raise ValueError('Invalid date reason')
    tz = ZoneInfo(zone)

    def parse(value):
        if value is None:
            return None
        if not isinstance(value, str):
            raise ValueError('Date must be ISO text or null')
        if re.fullmatch(r'\d{4}-\d{2}-\d{2}', value):
            result = datetime.combine(datetime.strptime(value, '%Y-%m-%d').date(), time(default_hour), tz)
        elif re.fullmatch(r'\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(?::\d{2})?(?:Z|[+-]\d{2}:\d{2})?', value):
            result = datetime.fromisoformat(value.replace('Z', '+00:00'))
            if result.tzinfo is None:
                result = result.replace(tzinfo=tz)
        else:
            raise ValueError('Invalid ISO date')
        # Reject nonexistent local times (DST gaps).
        if result.astimezone(timezone.utc).astimezone(result.tzinfo).replace(tzinfo=None) != result.replace(tzinfo=None):
            raise ValueError('Nonexistent local time')
        if published_at and not published_at - timedelta(days=7) <= result <= published_at + timedelta(days=730):
            raise ValueError('Date implausible relative to publication')
        return result

    for key in ('start_at', 'end_at', 'winner_at', 'winner_offset_days'):
        if key not in data:
            raise ValueError('Missing date field')
    start, end, winner = (parse(data[k]) for k in ('start_at', 'end_at', 'winner_at'))
    offset = data['winner_offset_days']
    if offset is not None:
        if type(offset) is not int or not 0 <= offset <= 365 or end is None:
            raise ValueError('Invalid winner offset')
        try:
            calculated = end + timedelta(days=offset)
        except OverflowError as exc:
            raise ValueError('Winner offset overflows date range') from exc
        if published_at and calculated > published_at + timedelta(days=730):
            raise ValueError('Winner offset implausible relative to publication')
        if winner and winner.date() != calculated.date():
            raise ValueError('Conflicting winner dates')
        winner = winner or calculated
    if (start and end and end < start) or (winner and (end or start) and winner < (end or start)):
        raise ValueError('Invalid event chronology')
    if data['confidence'] == 'low':
        return RaffleDates(None, None, 'Ambiguous dates')
    return RaffleDates(end, winner, data['reason'][:500])
