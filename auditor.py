import asyncio
import json
import logging
import re
import socket
import ssl
from datetime import datetime

import httpx
from anthropic import AsyncAnthropic
from bs4 import BeautifulSoup

from config import ANTHROPIC_API_KEY, PAGESPEED_API_KEY

logger = logging.getLogger(__name__)

CRITERIA_NAMES = [
    "Скорость загрузки",
    "Мобильная версия",
    "SEO оптимизация",
    "Безопасность",
    "Удобство навигации",
    "Качество контента",
    "Наличие CTA",
    "Работоспособность форм",
    "Адаптивность дизайна",
    "Скорость отклика сервера",
    "Видимость в ИИ-поисковиках",
]

_URL_RE = re.compile(r"^https?://[^\s/$.?#].[^\s]*$")
_HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; SiteAuditBot/1.0)"}

_CTA_KEYWORDS = {
    "заказать", "купить", "записаться", "позвонить", "написать", "получить",
    "скачать", "подписаться", "связаться", "заявка", "консультация", "оставить",
    "отправить", "call", "order", "buy", "sign up", "subscribe", "contact", "get",
}


class Auditor:
    def __init__(self):
        self._claude = AsyncAnthropic(api_key=ANTHROPIC_API_KEY)

    # ─── public ─────────────────────────────────────────────────────────────

    def normalize_url(self, raw: str) -> str | None:
        url = raw.strip()
        if not url.startswith(("http://", "https://")):
            url = "https://" + url
        return url if _URL_RE.match(url) else None

    async def run_audit(self, url: str, progress_msg=None) -> dict | None:
        async def _step(text: str):
            if progress_msg:
                await progress_msg.edit_text(text)

        await _step(
            "Шаг 1/4 — Загружаю страницу\n"
            "Ищу заголовки, SEO-теги, формы, CTA-кнопки..."
        )
        crawl = await self._crawl(url)
        if crawl is None:
            return None

        h1_count = len(crawl.get("h1", []))
        cta_count = len(crawl.get("cta_buttons", []))
        forms_count = len(crawl.get("forms", []))
        ssl_ok = "да" if crawl.get("is_https") else "нет"
        robots_ok_str = "есть" if crawl.get("robots_txt") else "нет"
        spa_note = "\nВнимание: сайт на JS (React/Vue) — часть данных недоступна" if crawl.get("is_likely_spa") else ""

        await _step(
            f"Нашёл: H1 — {h1_count}, CTA-кнопок — {cta_count}, форм — {forms_count}\n"
            f"HTTPS: {ssl_ok}, robots.txt: {robots_ok_str}{spa_note}\n\n"
            "Шаг 2/4 — Проверяю скорость через PageSpeed (до 40 сек)..."
        )
        pagespeed = await self._pagespeed(url)

        await _step("Шаг 3/4 — Проверяю SSL-сертификат...")
        security = self._check_security(url, crawl)

        if pagespeed:
            perf = pagespeed.get("performance", "?")
            seo_score = pagespeed.get("seo", "?")
            fcp = pagespeed.get("fcp", "?")
            step4 = (
                f"PageSpeed: скорость {perf}/10, SEO {seo_score}/10, FCP {fcp}\n\n"
                "Шаг 4/4 — ИИ читает контент и считает оценки (10-20 сек)..."
            )
        else:
            step4 = (
                "PageSpeed не ответил — продолжаю без данных о скорости\n\n"
                "Шаг 4/4 — ИИ читает контент и считает оценки (10-20 сек)..."
            )
        await _step(step4)

        result = await self._claude_analysis(url, crawl, pagespeed, security)
        return result

    # ─── crawl ──────────────────────────────────────────────────────────────

    async def _crawl(self, url: str) -> dict | None:
        try:
            async with httpx.AsyncClient(
                follow_redirects=True, timeout=30, headers=_HEADERS
            ) as client:
                resp = await client.get(url)

            soup = BeautifulSoup(resp.text, "html.parser")

            title = (soup.title.string or "").strip() if soup.title else ""

            meta_desc = meta_viewport = ""
            for m in soup.find_all("meta"):
                name = (m.get("name") or "").lower()
                if name == "description":
                    meta_desc = m.get("content", "")
                elif name == "viewport":
                    meta_viewport = m.get("content", "")

            h1 = [h.get_text(strip=True) for h in soup.find_all("h1")]
            h2 = [h.get_text(strip=True) for h in soup.find_all("h2")][:8]

            nav_links = [
                a.get_text(strip=True)
                for nav in soup.find_all("nav")
                for a in nav.find_all("a")
            ]

            forms = []
            for f in soup.find_all("form"):
                inputs = [i.get("type", "text") for i in f.find_all("input")]
                forms.append(
                    {
                        "action": f.get("action", ""),
                        "method": f.get("method", "get").upper(),
                        "input_types": inputs,
                        "has_submit": any(
                            t in ("submit", "button") for t in inputs
                        )
                        or bool(f.find("button")),
                    }
                )

            cta_buttons = [
                el.get_text(strip=True)
                for el in soup.find_all(["button", "a"])
                if any(kw in el.get_text(strip=True).lower() for kw in _CTA_KEYWORDS)
            ][:15]

            tel_links = [
                a["href"]
                for a in soup.find_all("a", href=True)
                if a["href"].startswith("tel:")
            ]

            imgs = soup.find_all("img")
            imgs_no_alt = sum(1 for i in imgs if not i.get("alt"))

            has_mq = any(
                "@media" in (s.get_text() or "") for s in soup.find_all("style")
            )

            # AEO: extract JSON-LD BEFORE removing scripts
            json_ld_types = []
            has_speakable = False
            for script in soup.find_all("script", type="application/ld+json"):
                raw = script.string or ""
                try:
                    data = json.loads(raw)
                    if isinstance(data, dict):
                        for item in data.get("@graph", [data]):
                            if isinstance(item, dict) and "@type" in item:
                                json_ld_types.append(item["@type"])
                    if "speakable" in raw.lower():
                        has_speakable = True
                except Exception:
                    pass

            # Remove script/style so Claude gets only visible text, not JS code
            for tag in soup(["script", "style"]):
                tag.decompose()

            # AEO: FAQ detection (after decompose — visible text only)
            has_faq = bool(
                soup.find("details") or
                soup.find("summary") or
                any(
                    "вопрос" in h.get_text().lower() or "faq" in h.get_text().lower()
                    for h in soup.find_all(["h2", "h3"])
                )
            )

            # AEO: meta robots max-snippet
            has_ai_snippet_meta = False
            for m in soup.find_all("meta", attrs={"name": "robots"}):
                if "max-snippet" in (m.get("content") or "").lower():
                    has_ai_snippet_meta = True

            base = f"{url.split('//')[0]}//{url.split('/')[2]}"
            robots_ok = sitemap_ok = False
            ai_bots_allowed = False
            async with httpx.AsyncClient(timeout=5, headers=_HEADERS) as c:
                try:
                    r = await c.get(f"{base}/robots.txt")
                    robots_ok = r.status_code == 200
                    if robots_ok:
                        robots_text = r.text.lower()
                        ai_bots_allowed = any(
                            bot in robots_text
                            for bot in ["gptbot", "claudebot", "perplexitybot", "anthropic-ai"]
                        )
                except Exception:
                    pass
                try:
                    sitemap_ok = (await c.get(f"{base}/sitemap.xml")).status_code == 200
                except Exception:
                    pass

            text = " ".join(soup.get_text().split())[:3000]
            is_likely_spa = len(text) < 300 and not h1

            return {
                "status_code": resp.status_code,
                "title": title,
                "meta_description": meta_desc,
                "meta_viewport": meta_viewport,
                "h1": h1,
                "h2": h2,
                "nav_links": nav_links[:10],
                "forms": forms,
                "cta_buttons": cta_buttons,
                "tel_links": tel_links,
                "images_total": len(imgs),
                "images_no_alt": imgs_no_alt,
                "has_media_queries": has_mq,
                "robots_txt": robots_ok,
                "sitemap_xml": sitemap_ok,
                "is_https": url.startswith("https://"),
                "text_content": text,
                "is_likely_spa": is_likely_spa,
                "json_ld_types": json_ld_types,
                "has_speakable": has_speakable,
                "has_faq": has_faq,
                "has_ai_snippet_meta": has_ai_snippet_meta,
                "ai_bots_allowed": ai_bots_allowed,
            }

        except (httpx.ConnectError, httpx.TimeoutException):
            return None
        except Exception as exc:
            logger.error("Crawl error: %s", exc)
            return None

    # ─── pagespeed ───────────────────────────────────────────────────────────

    async def _pagespeed(self, url: str) -> dict:
        params = {
            "url": url,
            "strategy": "mobile",
            "category": ["performance", "seo", "accessibility", "best-practices"],
        }
        if PAGESPEED_API_KEY:
            params["key"] = PAGESPEED_API_KEY

        try:
            async with httpx.AsyncClient(timeout=60) as client:
                r = await client.get(
                    "https://www.googleapis.com/pagespeedonline/v5/runPagespeed",
                    params=params,
                )
            if r.status_code != 200:
                return {}

            lhr = r.json().get("lighthouseResult", {})
            cats = lhr.get("categories", {})
            au = lhr.get("audits", {})

            def sc(key: str) -> float:
                return round((cats.get(key, {}).get("score") or 0) * 10, 1)

            return {
                "performance": sc("performance"),
                "seo": sc("seo"),
                "accessibility": sc("accessibility"),
                "best_practices": sc("best-practices"),
                "fcp": au.get("first-contentful-paint", {}).get("displayValue", "N/A"),
                "speed_index": au.get("speed-index", {}).get("displayValue", "N/A"),
                "tti": au.get("interactive", {}).get("displayValue", "N/A"),
                "ttfb_ms": au.get("server-response-time", {}).get("numericValue") or 0,
                "lcp": au.get("largest-contentful-paint", {}).get("displayValue", "N/A"),
                "cls": au.get("cumulative-layout-shift", {}).get("displayValue", "N/A"),
            }
        except Exception as exc:
            logger.error("PageSpeed error: %s", exc)
            return {}

    # ─── security ────────────────────────────────────────────────────────────

    def _check_security(self, url: str, crawl: dict) -> dict:
        is_https = crawl.get("is_https", False)
        ssl_valid = ssl_expiry = False

        if is_https:
            hostname = url.split("/")[2]
            try:
                ctx = ssl.create_default_context()
                with socket.create_connection((hostname, 443), timeout=5) as sock:
                    with ctx.wrap_socket(sock, server_hostname=hostname) as ssock:
                        cert = ssock.getpeercert()
                        ssl_valid = True
                        ssl_expiry = cert.get("notAfter", "")
            except Exception:
                pass

        return {
            "is_https": is_https,
            "ssl_valid": ssl_valid,
            "ssl_expiry": ssl_expiry,
        }

    # ─── helpers ─────────────────────────────────────────────────────────────

    @staticmethod
    def letter_grade(avg: float) -> str:
        if avg >= 9:
            return "A"
        if avg >= 8:
            return "B+"
        if avg >= 7:
            return "B"
        if avg >= 6:
            return "C+"
        if avg >= 5:
            return "C"
        if avg >= 4:
            return "D+"
        if avg >= 3:
            return "D"
        return "F"

    # ─── claude ──────────────────────────────────────────────────────────────

    async def _claude_analysis(
        self, url: str, crawl: dict, ps: dict, sec: dict
    ) -> dict:
        prompt = f"""Ты эксперт по веб-разработке, SEO и конверсионному маркетингу.
Твоя задача — провести аудит сайта {url} и выдать оценки понятным языком для владельца малого бизнеса.

## Технические данные (краулер):
- Title: {crawl.get('title') or 'Не найден'}
- Meta description: {(crawl.get('meta_description') or 'Отсутствует')[:200]}
- Meta viewport: {crawl.get('meta_viewport') or 'Отсутствует'}
- H1: {crawl.get('h1', [])}
- H2 (первые 5): {crawl.get('h2', [])[:5]}
- Навигация: {crawl.get('nav_links', [])}
- Формы: {json.dumps(crawl.get('forms', []), ensure_ascii=False)[:500]}
- CTA-кнопки найдены: {crawl.get('cta_buttons', [])}
- Телефонные ссылки tel:: {crawl.get('tel_links', [])}
- Изображений: {crawl.get('images_total', 0)}, без alt: {crawl.get('images_no_alt', 0)}
- robots.txt: {crawl.get('robots_txt')}, sitemap.xml: {crawl.get('sitemap_xml')}
- HTTPS: {crawl.get('is_https')}, media queries: {crawl.get('has_media_queries')}

## PageSpeed (мобильная):
- Производительность: {ps.get('performance', 'N/A')}/10
- SEO: {ps.get('seo', 'N/A')}/10
- Доступность: {ps.get('accessibility', 'N/A')}/10
- Best practices: {ps.get('best_practices', 'N/A')}/10
- FCP: {ps.get('fcp', 'N/A')}, LCP: {ps.get('lcp', 'N/A')}, CLS: {ps.get('cls', 'N/A')}
- TTI: {ps.get('tti', 'N/A')}, TTFB: {ps.get('ttfb_ms', 0):.0f} мс

## Безопасность:
- HTTPS: {sec.get('is_https')}, SSL: {sec.get('ssl_valid')}, истекает: {sec.get('ssl_expiry') or 'N/A'}

## AEO (Видимость в ИИ-поисковиках — ChatGPT, Perplexity, Claude):
- JSON-LD типы найдены: {crawl.get('json_ld_types') or 'нет'}
- Speakable specification: {crawl.get('has_speakable', False)}
- FAQ-секция на странице: {crawl.get('has_faq', False)}
- Meta robots max-snippet: {crawl.get('has_ai_snippet_meta', False)}
- AI-боты в robots.txt (GPTBot, ClaudeBot, PerplexityBot): {crawl.get('ai_bots_allowed', False)}
- sitemap.xml: {crawl.get('sitemap_xml', False)}

## Контент страницы (читай внимательно — это основа для оценки качества контента):
{crawl.get('text_content', '')[:2500]}

---

ВАЖНО: пиши на русском языке для владельца бизнеса, не для SEO-специалиста.
- В поле "problem" — объясни ущерб для бизнеса (потеря клиентов, денег, позиций). 1-2 предложения.
- В поле "recommendation" — конкретные шаги как исправить. 3-5 предложений с примерами.
- Если проблем нет — напиши что всё хорошо.

Для критерия "Качество контента" оцени:
  — Понятно ли за 5 секунд что предлагает сайт (оффер)
  — Убедителен ли заголовок/H1
  — Есть ли конкретные выгоды или только общие слова («профессионально», «качественно»)
  — Есть ли доверительные сигналы (отзывы, кейсы, гарантии, контакты)
  — Грамотность и читаемость текста

Для критерия "Видимость в ИИ-поисковиках" оцени:
  — Есть ли JSON-LD разметка (Person, Service, FAQPage, HowTo, Organization)?
  — Есть ли FAQ-секция с конкретными фактами (ценами, сроками)?
  — Разрешён ли доступ для AI-краулеров (GPTBot, ClaudeBot) в robots.txt?
  — Есть ли sitemap.xml и meta robots max-snippet?
  — Достаточно ли конкретных данных (цены, сроки, контакты) для цитирования ИИ?
  Оценка 10 = всё есть. Оценка 1 = ChatGPT/Perplexity не смогут найти и процитировать сайт.

Для критерия "Наличие CTA" оцени:
  — Есть ли призыв к действию выше линии сгиба (первый экран)
  — Конкретен ли CTA («Записаться на консультацию») или абстрактен («Узнать больше»)
  — Сколько CTA-элементов, не перегружен ли экран

Верни ТОЛЬКО валидный JSON без лишнего текста:

{{
  "express_summary": "<3-4 предложения общего впечатления о сайте для владельца. Что бросается в глаза сразу. Начни с самой острой проблемы.>",
  "top3_priority": [<id критерия>, <id критерия>, <id критерия>],
  "scores": [
    {{"id": 1, "name": "Скорость загрузки", "score": <1-10>, "problem": "<для клиента>", "recommendation": "<для исполнителя>"}},
    {{"id": 2, "name": "Мобильная версия", "score": <1-10>, "problem": "...", "recommendation": "..."}},
    {{"id": 3, "name": "SEO оптимизация", "score": <1-10>, "problem": "...", "recommendation": "..."}},
    {{"id": 4, "name": "Безопасность", "score": <1-10>, "problem": "...", "recommendation": "..."}},
    {{"id": 5, "name": "Удобство навигации", "score": <1-10>, "problem": "...", "recommendation": "..."}},
    {{"id": 6, "name": "Качество контента", "score": <1-10>, "problem": "...", "recommendation": "..."}},
    {{"id": 7, "name": "Наличие CTA", "score": <1-10>, "problem": "...", "recommendation": "..."}},
    {{"id": 8, "name": "Работоспособность форм", "score": <1-10>, "problem": "...", "recommendation": "..."}},
    {{"id": 9, "name": "Адаптивность дизайна", "score": <1-10>, "problem": "...", "recommendation": "..."}},
    {{"id": 10, "name": "Скорость отклика сервера", "score": <1-10>, "problem": "...", "recommendation": "..."}},
    {{"id": 11, "name": "Видимость в ИИ-поисковиках", "score": <1-10>, "problem": "<что мешает ChatGPT/Perplexity найти сайт>", "recommendation": "<конкретные шаги: JSON-LD, FAQ, robots.txt для AI-ботов>"}}
  ]
}}

Шкала оценок: 9-10 отлично, 7-8 хорошо, 5-6 удовлетворительно, 3-4 плохо, 1-2 критично."""

        response = await self._claude.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=4000,
            messages=[{"role": "user", "content": prompt}],
        )

        content = response.content[0].text.strip()
        if content.startswith("```"):
            parts = content.split("```")
            content = parts[1]
            if content.startswith("json"):
                content = content[4:]
        content = content.strip()

        data = json.loads(content)
        scores = data["scores"]
        avg = round(sum(s["score"] for s in scores) / len(scores), 1)

        return {
            "url": url,
            "date": datetime.now().strftime("%d.%m.%Y %H:%M"),
            "scores": scores,
            "average_score": avg,
            "letter_grade": self.letter_grade(avg),
            "express_summary": data.get("express_summary", ""),
            "top3_priority": data.get("top3_priority", []),
        }
