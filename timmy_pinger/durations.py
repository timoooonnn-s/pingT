"""How durations are written on screen, in the log and in the summary."""


def duration(seconds: float) -> str:
    """4:12, 1:35:00"""
    seconds = int(seconds)
    hours, rest = divmod(seconds, 3600)
    minutes, secs = divmod(rest, 60)
    return f"{hours}:{minutes:02d}:{secs:02d}" if hours else f"{minutes}:{secs:02d}"


def short_duration(seconds: float) -> str:
    """At most 5 characters, for tight screen cells: 4:12, 59:59, 1h35, 99h59, 5d."""
    seconds = int(seconds)
    if seconds < 3600:
        return f"{seconds // 60}:{seconds % 60:02d}"
    if seconds < 100 * 3600:
        return f"{seconds // 3600}h{seconds % 3600 // 60:02d}"
    return f"{seconds // 86400}d"
