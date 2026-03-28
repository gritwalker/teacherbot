import json
import os
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo


DEFAULT_NEWS_RSS_URL = "https://news.google.com/rss?hl=ko&gl=KR&ceid=KR:ko"


def load_dotenv(dotenv_path: str = ".env") -> None:
    try:
        with open(dotenv_path, "r", encoding="utf-8") as f:
            lines = f.readlines()
    except FileNotFoundError:
        return

    for raw_line in lines:
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue

        if "=" not in line:
            continue

        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip("'").strip('"')
        if key and key not in os.environ:
            os.environ[key] = value


def _http_get(url: str, timeout_s: int = 20) -> bytes:
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": "teacherbot-news/1.0",
            "Accept": "*/*",
        },
        method="GET",
    )
    with urllib.request.urlopen(req, timeout=timeout_s) as resp:
        return resp.read()


def _http_post_json(url: str, payload: dict, headers: dict, timeout_s: int = 40) -> bytes:
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=body,
        headers={**headers, "Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout_s) as resp:
        return resp.read()


def _http_post_form(url: str, payload: dict, timeout_s: int = 20) -> bytes:
    body = urllib.parse.urlencode(payload).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=body,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout_s) as resp:
        return resp.read()


def fetch_google_news_rss(rss_url: str, top_n: int) -> list[dict]:
    xml_bytes = _http_get(rss_url)
    root = ET.fromstring(xml_bytes)

    channel = root.find("channel")
    if channel is None:
        return []

    items: list[dict] = []
    for item in channel.findall("item"):
        title_el = item.find("title")
        link_el = item.find("link")
        pub_el = item.find("pubDate")
        title = (title_el.text or "").strip() if title_el is not None else ""
        link = (link_el.text or "").strip() if link_el is not None else ""
        pub_date = (pub_el.text or "").strip() if pub_el is not None else ""
        if not title:
            continue
        items.append({"title": title, "link": link, "pubDate": pub_date})
        if len(items) >= top_n:
            break

    return items


def deepseek_summarize_headlines(headlines: list[str], api_key: str, base_url: str) -> str | None:
    if not api_key:
        return None
    if not headlines:
        return None

    url = base_url.rstrip("/") + "/v1/chat/completions"
    joined = "\n".join(f"- {h}" for h in headlines)
    payload = {
        "model": os.environ.get("DEEPSEEK_MODEL", "deepseek-chat"),
        "temperature": 0.2,
        "messages": [
            {
                "role": "system",
                "content": "너는 한국어 뉴스 요약 도우미다. 사실로 단정하지 말고, 제목 기반으로 간결히 정리한다.",
            },
            {
                "role": "user",
                "content": "아래는 방금 수집한 주요 뉴스 제목이야. 한국어로 3~5줄 핵심만 요약해줘.\n\n"
                + joined,
            },
        ],
    }
    raw = _http_post_json(
        url,
        payload=payload,
        headers={"Authorization": f"Bearer {api_key}"},
        timeout_s=40,
    )
    data = json.loads(raw.decode("utf-8"))
    choices = data.get("choices") or []
    if not choices:
        return None
    msg = (choices[0].get("message") or {}).get("content")
    if not msg:
        return None
    return str(msg).strip()


def telegram_send_message(token: str, chat_id: str, text: str) -> None:
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    raw = _http_post_form(
        url,
        payload={
            "chat_id": chat_id,
            "text": text,
            "disable_web_page_preview": "true",
        },
    )
    data = json.loads(raw.decode("utf-8"))
    if not data.get("ok", False):
        raise RuntimeError(f"Telegram sendMessage failed: {data}")


def build_news_message(now: datetime, items: list[dict], summary: str | None) -> str:
    ts = now.strftime("%Y-%m-%d %H:%M")
    lines: list[str] = [f"[주요뉴스] {ts}"]
    if summary:
        lines.append("")
        lines.append(summary)
    lines.append("")
    for idx, item in enumerate(items, start=1):
        title = item.get("title", "")
        link = item.get("link", "")
        if link:
            lines.append(f"{idx}. {title}\n{link}")
        else:
            lines.append(f"{idx}. {title}")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def send_major_news_once() -> None:
    rss_url = os.environ.get("NEWS_RSS_URL", DEFAULT_NEWS_RSS_URL)
    top_n_str = os.environ.get("NEWS_TOP_N", "8")
    try:
        top_n = max(1, min(20, int(top_n_str)))
    except ValueError:
        top_n = 8

    tz_name = os.environ.get("TIMEZONE", "Asia/Seoul")
    tz = ZoneInfo(tz_name)
    now = datetime.now(tz=tz)

    items = fetch_google_news_rss(rss_url=rss_url, top_n=top_n)
    headlines = [it["title"] for it in items]

    summary: str | None = None
    deepseek_key = os.environ.get("DEEPSEEK_API_KEY", "")
    deepseek_base_url = os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com")
    if deepseek_key:
        try:
            summary = deepseek_summarize_headlines(headlines, api_key=deepseek_key, base_url=deepseek_base_url)
        except Exception:
            summary = None

    token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "")
    if not token or not chat_id:
        raise RuntimeError("TELEGRAM_BOT_TOKEN 및 TELEGRAM_CHAT_ID 환경변수가 필요합니다.")

    message = build_news_message(now=now, items=items, summary=summary)
    telegram_send_message(token=token, chat_id=chat_id, text=message)


def seconds_until_next_hour(now: datetime) -> float:
    next_hour = now.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
    return max(0.0, (next_hour - now).total_seconds())


def main() -> None:
    load_dotenv(".env")

    tz_name = os.environ.get("TIMEZONE", "Asia/Seoul")
    tz = ZoneInfo(tz_name)

    send_major_news_once()
    while True:
        now = datetime.now(tz=tz)
        time.sleep(seconds_until_next_hour(now))
        try:
            send_major_news_once()
        except Exception as e:
            print(f"[{datetime.now(tz=tz).isoformat()}] send failed: {e}")


if __name__ == "__main__":
    main()
