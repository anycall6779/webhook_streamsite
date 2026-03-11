import time
import logging
import threading
import requests
from selenium import webdriver
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from webdriver_manager.chrome import ChromeDriverManager
from collections import deque

# ==========================================
# [사용자 설정]
CHANNEL_STREAMS_URL = "https://www.youtube.com/@_25252/streams"
DISCORD_WEBHOOK_URL = "YOUTH_DISCORD_WEBHOOK_URL_HERE"

# [동작 설정]
POLL_INTERVAL = 0.5          # 채팅 확인 주기 (초)
SCAN_INTERVAL = 5           # 채널 스캔 주기 (초)
RECENT_CHAT_COUNT = 30       # 루프마다 확인할 최신 채팅 수
MAX_PROCESSED_IDS = 500      # 중복 방지용 ID 보관 수
PAGE_LOAD_WAIT = 20          # 페이지 로딩 타임아웃 (초)
INITIAL_LOAD_DELAY = 3       # 초기 로딩 대기 (초)
CHANNEL_LOAD_DELAY = 5       # 채널 페이지 로딩 대기 (초)
MAX_CHAT_ERRORS = 10         # 채팅 스레드 최대 연속 오류
# ==========================================

# 로깅 설정
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("youtube-bot")

active_streams = set()
active_streams_lock = threading.Lock()


def get_driver(is_headless=True):
    """봇 탐지 우회가 적용된 Chrome WebDriver를 생성합니다."""
    options = webdriver.ChromeOptions()

    # 봇 탐지 우회
    options.add_argument("--disable-blink-features=AutomationControlled")
    options.add_experimental_option("excludeSwitches", ["enable-automation"])
    options.add_experimental_option("useAutomationExtension", False)

    if is_headless:
        options.add_argument("--headless=new")

    options.add_argument("--window-size=1920,1080")
    options.add_argument("--log-level=3")
    options.add_argument("--disable-gpu")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument(
        "user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
    )

    prefs = {"intl.accept_languages": "ko-KR"}
    options.add_experimental_option("prefs", prefs)

    service = Service(ChromeDriverManager().install())
    driver = webdriver.Chrome(service=service, options=options)

    # navigator.webdriver 프로퍼티 숨기기
    driver.execute_cdp_cmd("Page.addScriptToEvaluateOnNewDocument", {
        "source": """
            Object.defineProperty(navigator, 'webdriver', {
                get: () => undefined
            })
        """
    })

    return driver


def send_discord(nickname, content):
    """디스코드 웹훅으로 메시지를 전송합니다. Rate-limit 시 자동 대기 후 재시도합니다."""
    if not content.strip():
        return

    data = {"username": f"[YouTube] {nickname}", "content": content}

    for attempt in range(3):
        try:
            resp = requests.post(DISCORD_WEBHOOK_URL, json=data, timeout=10)

            if resp.status_code == 429:  # Rate-limited
                retry_after = resp.json().get("retry_after", 1)
                log.warning(f"⏳ Discord rate-limit, {retry_after:.1f}초 대기")
                time.sleep(retry_after)
                continue

            resp.raise_for_status()
            return

        except requests.RequestException as e:
            log.error(f"❌ Discord 전송 실패 (시도 {attempt + 1}/3): {e}")
            time.sleep(1)


# ─────────────────────────────────────────
# [작업자] 개별 방송 채팅을 감시하는 함수
# ─────────────────────────────────────────
def monitor_chat_thread(video_id):
    """스레드로 실행되며, 특정 유튜브 라이브 채팅을 감시합니다."""
    driver = None
    try:
        driver = get_driver(is_headless=True)

        popout_url = f"https://www.youtube.com/live_chat?is_popout=1&v={video_id}"
        driver.get(popout_url)
        log.info(f"🔗 [{video_id}] 채팅창 접속 중...")

        try:
            WebDriverWait(driver, PAGE_LOAD_WAIT).until(
                EC.presence_of_element_located((By.TAG_NAME, "yt-live-chat-renderer"))
            )
            log.info(f"✅ [{video_id}] 채팅창 접속 성공!")
        except Exception:
            log.error(f"❌ [{video_id}] 채팅창 접속 실패 (타임아웃)")
            return

        time.sleep(INITIAL_LOAD_DELAY)
        processed_ids = deque(maxlen=MAX_PROCESSED_IDS)

        # 기존 채팅 스킵
        initial_items = driver.find_elements(By.TAG_NAME, "yt-live-chat-text-message-renderer")
        for item in initial_items:
            processed_ids.append(item.id)
        log.info(f"⏩ [{video_id}] 기존 메시지 {len(initial_items)}개 건너뜀")
        log.info(f"🔴 [{video_id}] 실시간 감시 시작")

        consecutive_errors = 0

        while True:
            try:
                if not driver.window_handles:
                    log.warning(f"⚠️ [{video_id}] 브라우저 창이 닫혔습니다.")
                    break

                chat_items = driver.find_elements(By.TAG_NAME, "yt-live-chat-text-message-renderer")

                for item in chat_items[-RECENT_CHAT_COUNT:]:
                    try:
                        if item.id in processed_ids:
                            continue

                        # 닉네임 추출
                        nick_el = item.find_element(By.ID, "author-name")
                        nickname = nick_el.get_attribute("textContent").strip()

                        # 메시지 내용 추출
                        msg_el = item.find_element(By.ID, "message")
                        content_parts = []

                        raw_text = msg_el.get_attribute("textContent")
                        if raw_text and raw_text.strip():
                            content_parts.append(raw_text.strip())

                        emoji_els = msg_el.find_elements(By.TAG_NAME, "img")
                        for emoji in emoji_els:
                            emoji_name = emoji.get_attribute("alt")
                            if emoji_name:
                                content_parts.append(emoji_name)
                            else:
                                content_parts.append("(콘)")

                        final_content = " ".join(content_parts)

                        # 내용이 비어있으면 다음 루프에서 재시도
                        if not final_content:
                            continue

                        log.info(f"📨 [{video_id}] {nickname}: {final_content}")
                        send_discord(nickname, final_content)
                        processed_ids.append(item.id)

                    except Exception:
                        continue

                consecutive_errors = 0
                time.sleep(POLL_INTERVAL)

            except Exception as e:
                consecutive_errors += 1
                log.warning(f"⚠️ [{video_id}] 루프 오류 ({consecutive_errors}회): {e}")

                if consecutive_errors >= MAX_CHAT_ERRORS:
                    log.error(f"❌ [{video_id}] 연속 오류 초과 — 감시 종료")
                    break

                time.sleep(1)

    except Exception as e:
        log.error(f"❌ [{video_id}] 스레드 오류: {e}")

    finally:
        with active_streams_lock:
            active_streams.discard(video_id)
        log.info(f"🔚 [{video_id}] 감시 종료")
        if driver:
            try:
                driver.quit()
            except Exception:
                pass


# ─────────────────────────────────────────
# [감시자] 메인 스캐너
# ─────────────────────────────────────────
def main():
    log.info("🔄 유튜브 채팅 봇을 시작합니다...")
    log.info(f"📡 감시 채널: {CHANNEL_STREAMS_URL}")

    driver = None
    try:
        driver = get_driver(is_headless=True)

        while True:
            try:
                driver.get(CHANNEL_STREAMS_URL)
                time.sleep(CHANNEL_LOAD_DELAY)

                live_badges = driver.find_elements(
                    By.XPATH,
                    "//div[contains(@class, 'yt-badge-shape__text') and (text()='라이브' or text()='LIVE')]"
                )

                if live_badges:
                    log.info(f"🔍 라이브 배지 {len(live_badges)}개 발견")

                for badge in live_badges:
                    try:
                        link_element = badge.find_element(By.XPATH, "./ancestor::a[@id='thumbnail']")
                        href = link_element.get_attribute("href")

                        if href and "watch?v=" in href:
                            video_id = href.split("v=")[1].split("&")[0]

                            with active_streams_lock:
                                if video_id in active_streams:
                                    continue
                                active_streams.add(video_id)

                            log.info(f"🆕 새 라이브 발견: {video_id}")

                            t = threading.Thread(
                                target=monitor_chat_thread,
                                args=(video_id,),
                                daemon=True,
                                name=f"chat-{video_id}",
                            )
                            t.start()

                    except Exception as e:
                        log.warning(f"⚠️ 배지 처리 중 오류: {e}")
                        continue

                with active_streams_lock:
                    active_count = len(active_streams)
                if active_count > 0:
                    log.info(f"📊 현재 감시 중인 방송: {active_count}개")

                time.sleep(SCAN_INTERVAL)

            except Exception as e:
                log.warning(f"⚠️ 스캔 루프 오류: {e}")
                time.sleep(5)

    except KeyboardInterrupt:
        log.info("👋 사용자에 의해 종료됩니다.")
    finally:
        if driver:
            try:
                driver.quit()
            except Exception:
                pass


if __name__ == "__main__":
    main()