"""Unix 时间戳 ↔ 日期时间转换。

把数字时间戳（秒/毫秒）转成人类可读时间，或把日期时间字符串转回时间戳。
支持在用户所在时区、北京时间、UTC 三套显示。除标准库外无额外依赖。
"""

from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional

WEEKDAYS_CN = ("星期一", "星期二", "星期三", "星期四", "星期五", "星期六", "星期日")

# China Standard Time is UTC+8 year-round (no DST), so a fixed offset is exact
# and avoids a dependency on the system/``tzdata`` tz database (Windows).
BEIJING_TZ = timezone(timedelta(hours=8))
UTC_TZ = timezone.utc

# Naive datetime strings we accept (parsed as the user's local timezone).
_DATETIME_FORMATS = (
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%d %H:%M",
    "%Y-%m-%d %H",
    "%Y-%m-%d",
    "%Y/%m/%d %H:%M:%S",
    "%Y/%m/%d %H:%M",
    "%Y/%m/%d",
    "%Y%m%d%H%M%S",
)


class TimestampError(ValueError):
    """Raised when input cannot be interpreted as a timestamp or date."""


def _fixed_offset(minutes: int) -> timezone:
    return timezone(timedelta(minutes=minutes))


def _detect_seconds(raw: str) -> Optional[float]:
    """Interpret a numeric string as a unix timestamp.

    Returns epoch seconds, or None if not numeric. Milliseconds (≈13 digits)
    are auto-detected and converted to seconds.
    """
    cleaned = raw.replace(",", "").replace(" ", "")
    if not cleaned or not cleaned.replace(".", "").isdigit():
        return None
    try:
        value = float(cleaned)
    except ValueError:
        return None
    # Negative / tiny values are not valid unix timestamps.
    if value < 0:
        return None
    # 13+ integer digits → milliseconds.
    integer_digits = len(cleaned.split(".")[0])
    if integer_digits >= 13:
        return value / 1000.0
    return value


def _parse_datetime(raw: str, tz_offset_min: int) -> Optional[float]:
    """Parse a human date string into epoch seconds (naive = user local).

    Accepts ``YYYY-MM-DD[ HH:MM[:SS]]``, ``/`` separators, compact
    ``YYYYMMDDHHMMSS``, ``now``/``现在``. ``Z``/``T`` (ISO) are normalized.
    """
    text = raw.strip()
    low = text.lower()
    if low in ("now", "现在", "当前"):
        return time.time()
    normalized = text.replace("T", " ").replace("Z", "").replace("z", "")
    # Drop a trailing UTC offset like "+08:00" / "+0800" if present.
    if len(normalized) > 6 and normalized[-6] in "+-" and normalized[-3] == ":":
        normalized = normalized[:-6]
    tz = _fixed_offset(tz_offset_min)
    for fmt in _DATETIME_FORMATS:
        try:
            dt = datetime.strptime(normalized.strip(), fmt)
            dt = dt.replace(tzinfo=tz)
            return dt.timestamp()
        except ValueError:
            continue
    return None


def _relative(ts: float) -> str:
    diff = ts - time.time()
    abs_diff = abs(diff)
    future = diff >= 0
    if abs_diff < 60:
        return "刚刚" if not future else "即将"
    units = (
        (365 * 24 * 3600, "年"),
        (30 * 24 * 3600, "个月"),
        (24 * 3600, "天"),
        (3600, "小时"),
        (60, "分钟"),
    )
    for seconds, label in units:
        if abs_diff >= seconds:
            n = int(abs_diff // seconds)
            return f"{n}{label}{'后' if future else '前'}"
    return "刚刚"


def convert(
    value: Any,
    *,
    tz_offset_min: int = 480,
) -> Dict[str, Any]:
    """Convert a timestamp or date string to rich, human-readable info.

    Parameters
    ----------
    value:
        Unix seconds/ms number, or a date/datetime string, or ``now``.
    tz_offset_min:
        Minutes east of UTC for the user's local timezone (used to interpret
        naive date input and to render the ``local`` display).

    Returns
    -------
    dict with ``kind`` (``timestamp`` | ``datetime``), ``epoch_seconds``,
    ``epoch_ms``, ``is_ms``, and the display fields ``iso`` / ``local`` /
    ``utc`` / ``beijing`` / ``date`` / ``time`` / ``weekday`` /
    ``weekday_en`` / ``day_of_year`` / ``relative``.
    """
    text = (str(value) if value is not None else "").strip()
    if not text:
        raise TimestampError("请输入时间戳或日期时间")

    seconds: Optional[float]
    is_ms = False
    kind = "timestamp"

    detected = _detect_seconds(text)
    if detected is not None:
        seconds = detected
        integer_digits = len(text.replace(",", "").replace(" ", "").split(".")[0])
        is_ms = integer_digits >= 13
    else:
        seconds = _parse_datetime(text, int(tz_offset_min))
        if seconds is None:
            raise TimestampError(
                "无法识别输入。请输入 Unix 时间戳（秒或毫秒），或日期时间，"
                "例如 2026-08-03 21:14:48 或 now。"
            )
        kind = "datetime"

    local_tz = _fixed_offset(int(tz_offset_min))
    local_dt = datetime.fromtimestamp(seconds, local_tz)
    utc_dt = datetime.fromtimestamp(seconds, UTC_TZ)
    bj_dt = utc_dt.astimezone(BEIJING_TZ)

    return {
        "kind": kind,
        "input": text,
        "epoch_seconds": int(seconds),
        "epoch_ms": int(seconds * 1000),
        "is_ms": is_ms,
        "iso": local_dt.isoformat(timespec="seconds"),
        "local": local_dt.strftime("%Y-%m-%d %H:%M:%S"),
        "utc": utc_dt.strftime("%Y-%m-%d %H:%M:%S"),
        "beijing": bj_dt.strftime("%Y-%m-%d %H:%M:%S"),
        "date": local_dt.strftime("%Y-%m-%d"),
        "time": local_dt.strftime("%H:%M:%S"),
        "weekday": WEEKDAYS_CN[local_dt.weekday()],
        "weekday_en": local_dt.strftime("%A"),
        "day_of_year": local_dt.timetuple().tm_yday,
        "relative": _relative(seconds),
    }


def now_snapshot(*, tz_offset_min: int = 480) -> Dict[str, Any]:
    """Return :func:`convert` info for the current moment."""
    return convert(time.time(), tz_offset_min=tz_offset_min)
