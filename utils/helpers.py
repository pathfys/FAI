import csv
import io
from datetime import datetime, timezone


def parse_recipients(text: str) -> list[str]:
    result: list[str] = []
    for line in text.splitlines():
        for part in line.split(","):
            value = part.strip().lstrip("@")
            if value:
                result.append(value)
    return result


def export_logs_csv(rows: list[dict]) -> str:
    if not rows:
        return ""
    output = io.StringIO()
    writer = csv.DictWriter(output, fieldnames=rows[0].keys())
    writer.writeheader()
    writer.writerows(rows)
    return output.getvalue()


def now_utc() -> datetime:
    return datetime.now(timezone.utc)
