from datetime import datetime, timezone


def parse_dt(value: str | None) -> datetime | None:
    """把 ISO 字符串解析为 UTC aware datetime。"""
    if not value:
        return None
    dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def as_utc(dt: datetime | None) -> datetime | None:
    """把可能是 naive 的 datetime 视为 UTC，保证相减/比较不出错。"""
    if dt is None:
        return None
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)
