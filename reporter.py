import os
from datetime import datetime
from pathlib import Path

from jinja2 import Environment, FileSystemLoader

REPORTS_DIR = Path("/tmp/audit_reports")


def _color(score: int) -> str:
    if score >= 8:
        return "#22c55e"
    if score >= 5:
        return "#f59e0b"
    return "#ef4444"


def _priority(score: int) -> tuple[str, str]:
    if score <= 4:
        return "Критично", "#ef4444"
    if score <= 6:
        return "Важно", "#f59e0b"
    return "Рекомендуется", "#3b82f6"


class Reporter:
    def __init__(self):
        REPORTS_DIR.mkdir(parents=True, exist_ok=True)
        tpl_dir = Path(__file__).parent / "templates"
        self._env = Environment(loader=FileSystemLoader(str(tpl_dir)))

    def _slug(self, url: str) -> str:
        return (
            url.replace("https://", "")
            .replace("http://", "")
            .split("/")[0]
            .replace(".", "_")
        )

    def generate_client_report(self, result: dict) -> str:
        scores = [
            {**s, "color": _color(s["score"])}
            for s in result["scores"]
        ]
        html = self._env.get_template("client_report.html").render(
            url=result["url"],
            date=result["date"],
            scores=scores,
            average=result["average_score"],
            average_color=_color(int(result["average_score"])),
            owner_username=os.environ.get("OWNER_TELEGRAM_USERNAME", ""),
        )
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        path = REPORTS_DIR / f"client_{self._slug(result['url'])}_{ts}.html"
        path.write_text(html, encoding="utf-8")
        return str(path)

    def generate_owner_report(self, result: dict, user) -> str:
        scores = []
        for s in result["scores"]:
            prio_label, prio_color = _priority(s["score"])
            scores.append(
                {**s, "color": _color(s["score"]),
                 "priority": prio_label, "priority_color": prio_color}
            )
        html = self._env.get_template("owner_report.html").render(
            url=result["url"],
            date=result["date"],
            scores=scores,
            average=result["average_score"],
            average_color=_color(int(result["average_score"])),
            client_name=user.full_name or user.first_name or "Не указано",
            client_telegram=(
                f"@{user.username}" if user.username else f"ID: {user.id}"
            ),
        )
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        path = REPORTS_DIR / f"owner_{self._slug(result['url'])}_{ts}.html"
        path.write_text(html, encoding="utf-8")
        return str(path)
