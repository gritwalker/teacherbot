import json
import os
import sys
import time
import urllib.parse
import urllib.request
from urllib.error import HTTPError
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo


DEFAULT_NEWS_RSS_URL = "https://news.google.com/rss?hl=ko&gl=KR&ceid=KR:ko"


def _safe_tz() -> ZoneInfo:
    tz_name = os.environ.get("TIMEZONE", "Asia/Seoul")
    try:
        return ZoneInfo(tz_name)
    except Exception:
        return ZoneInfo("UTC")


def log(message: str) -> None:
    ts = datetime.now(tz=_safe_tz()).isoformat(timespec="seconds")
    print(f"[{ts}] {message}", flush=True)


def load_dotenv(dotenv_path: str = ".env", override: bool = True) -> None:
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
        if not key:
            continue
        if override or key not in os.environ:
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
    try:
        raw = _http_post_form(
            url,
            payload={
                "chat_id": chat_id,
                "text": text,
                "disable_web_page_preview": "1",
            },
        )
    except HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Telegram sendMessage HTTP {e.code}: {body}") from e
    data = json.loads(raw.decode("utf-8"))
    if not data.get("ok", False):
        raise RuntimeError(f"Telegram sendMessage failed: {data}")


def normalize_telegram_chat_id(chat_id: str) -> str:
    cid = (chat_id or "").strip()
    if not cid:
        raise RuntimeError("TELEGRAM_CHAT_ID가 비어있습니다.")
    if cid.startswith("@"):
        return cid
    if cid.lstrip("-").isdigit():
        return cid
    raise RuntimeError(
        "TELEGRAM_CHAT_ID는 숫자 채팅 ID(예: 123456789 또는 -100...) 또는 @채널아이디 형태여야 합니다."
    )


def telegram_print_recent_chat_ids(token: str) -> None:
    log("telegram getUpdates 조회 중...")
    url = f"https://api.telegram.org/bot{token}/getUpdates?limit=50&timeout=0"
    raw = _http_get(url, timeout_s=20)
    data = json.loads(raw.decode("utf-8"))
    if not data.get("ok", False):
        raise RuntimeError(f"Telegram getUpdates failed: {data}")

    results = data.get("result") or []
    seen: set[int] = set()
    for upd in results:
        msg = upd.get("message") or upd.get("edited_message") or {}
        chat = msg.get("chat") or {}
        chat_id = chat.get("id")
        if not isinstance(chat_id, int):
            continue
        if chat_id in seen:
            continue
        seen.add(chat_id)
        chat_type = chat.get("type")
        title = chat.get("title")
        username = chat.get("username")
        name = title or username or chat_type or ""
        print(f"{chat_id}\t{name}")

    if not seen:
        print("아직 업데이트가 없습니다. 텔레그램에서 봇에게 /start 를 보내고 다시 실행하세요.")


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

    tz = _safe_tz()
    now = datetime.now(tz=tz)

    log(f"뉴스 수집 시작 (top_n={top_n})")
    items = fetch_google_news_rss(rss_url=rss_url, top_n=top_n)
    log(f"뉴스 수집 완료 (items={len(items)})")
    headlines = [it["title"] for it in items]

    summary: str | None = None
    deepseek_key = os.environ.get("DEEPSEEK_API_KEY", "")
    deepseek_base_url = os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com")
    if deepseek_key:
        try:
            log("DeepSeek 요약 생성 중...")
            summary = deepseek_summarize_headlines(headlines, api_key=deepseek_key, base_url=deepseek_base_url)
            if summary:
                log("DeepSeek 요약 생성 완료")
            else:
                log("DeepSeek 요약 생략/실패 (결과 없음)")
        except Exception:
            log("DeepSeek 요약 실패 (요약 없이 전송)")
            summary = None

    token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "")
    if not token or not chat_id:
        raise RuntimeError("TELEGRAM_BOT_TOKEN 및 TELEGRAM_CHAT_ID 환경변수가 필요합니다.")
    chat_id = normalize_telegram_chat_id(chat_id)

    message = build_news_message(now=now, items=items, summary=summary)
    log("텔레그램 전송 중...")
    telegram_send_message(token=token, chat_id=chat_id, text=message)
    log("텔레그램 전송 완료")


def seconds_until_next_hour(now: datetime) -> float:
    next_hour = now.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
    return max(0.0, (next_hour - now).total_seconds())


def main() -> None:
    base_dir = os.path.dirname(os.path.abspath(__file__))
    load_dotenv(os.path.join(base_dir, ".env"))
    log("프로그램 시작")

    token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    if "--print-chat-ids" in sys.argv:
        if not token:
            raise RuntimeError("TELEGRAM_BOT_TOKEN 환경변수가 필요합니다.")
        telegram_print_recent_chat_ids(token=token)
        return

    tz = _safe_tz()

    log("초기 뉴스 전송(즉시) 시작")
    send_major_news_once()
    log("초기 뉴스 전송(즉시) 완료")
    while True:
        now = datetime.now(tz=tz)
        sleep_s = seconds_until_next_hour(now)
        next_run = now.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
        log(f"다음 전송 시각: {next_run.strftime('%Y-%m-%d %H:%M:%S')} (대기 {int(sleep_s)}s)")
        remaining = sleep_s
        while remaining > 0:
            step = min(60.0, remaining)
            time.sleep(step)
            remaining -= step
            if remaining > 0:
                log(f"대기 중... 남은 시간 {int(remaining)}s")
        try:
            log("정각 뉴스 전송 시작")
            send_major_news_once()
            log("정각 뉴스 전송 완료")
        except Exception as e:
            log(f"전송 실패: {e}")


if __name__ == "__main__":
    main()
