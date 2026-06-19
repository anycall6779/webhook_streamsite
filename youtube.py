import logging
import os
import threading
import time
from collections import deque
from pathlib import Path

import requests
from selenium import webdriver
from selenium.common.exceptions import TimeoutException
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait

# ==========================================
# 사용자 설정
CHANNEL_STREAMS_URL = "https://www.youtube.com/@_25252/streams"
DISCORD_WEBHOOK_URL = ""

# 동작 설정
POLL_INTERVAL = 0.5
SCAN_INTERVAL = 5
RECENT_CHAT_COUNT = 30
MAX_PROCESSED_IDS = 500
PAGE_LOAD_WAIT = 30
INITIAL_LOAD_DELAY = 3
CHANNEL_LOAD_DELAY = 5
MAX_CHAT_ERRORS = 10
WDM_STALE_LOCK_SECONDS = 180
# ==========================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("youtube-bot")

active_streams = set()
active_streams_lock = threading.Lock()


def hide_webdriver_flag(driver):
    driver.execute_cdp_cmd(
        "Page.addScriptToEvaluateOnNewDocument",
        {
            "source": """
                Object.defineProperty(navigator, 'webdriver', {
                    get: () => undefined
                })
            """
        },
    )
    return driver


def build_chrome_options(is_headless=True):
    options = webdriver.ChromeOptions()
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
    options.add_argument("--lang=ko-KR")
    options.add_argument(
        "user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
    )
    options.add_experimental_option("prefs", {"intl.accept_languages": "ko-KR,ko"})
    return options


def find_cached_chromedriver():
    candidates = []

    env_path = os.environ.get("CHROMEDRIVER_PATH")
    if env_path:
        path = Path(env_path)
        if path.is_file():
            candidates.append(path)

    cache_root = Path.home() / ".wdm" / "drivers" / "chromedriver" / "win64"
    if cache_root.exists():
        candidates.extend(cache_root.rglob("chromedriver.exe"))

    if not candidates:
        return None

    return max(candidates, key=lambda p: p.stat().st_mtime)


def clear_stale_wdm_locks(max_age_seconds=WDM_STALE_LOCK_SECONDS):
    wdm_root = Path.home() / ".wdm"
    if not wdm_root.exists():
        return

    now = time.time()
    for lock_file in wdm_root.glob(".wdm-lock-chromedriver*"):
        try:
            age = now - lock_file.stat().st_mtime
            if age >= max_age_seconds:
                lock_file.unlink()
                log.warning(f"오래된 webdriver-manager lock 파일 제거: {lock_file}")
            else:
                log.info(f"webdriver-manager lock 사용 중으로 판단: {lock_file} ({age:.0f}초)")
        except OSError as e:
            log.warning(f"webdriver-manager lock 파일 정리 실패: {e}")


def get_driver(is_headless=True):
    options = build_chrome_options(is_headless=is_headless)
    errors = []

    cached_driver = find_cached_chromedriver()
    if cached_driver:
        try:
            log.info(f"캐시된 ChromeDriver 사용: {cached_driver}")
            driver = webdriver.Chrome(service=Service(str(cached_driver)), options=options)
            return hide_webdriver_flag(driver)
        except Exception as e:
            errors.append(f"cached={e}")
            log.warning(f"캐시된 ChromeDriver 실행 실패, 다음 방법 시도: {e}")

    try:
        log.info("Selenium Manager로 ChromeDriver 자동 감지 시도")
        driver = webdriver.Chrome(options=options)
        return hide_webdriver_flag(driver)
    except Exception as e:
        errors.append(f"selenium-manager={e}")
        log.warning(f"Selenium Manager 실패, webdriver-manager fallback 시도: {e}")

    clear_stale_wdm_locks()
    try:
        from webdriver_manager.chrome import ChromeDriverManager

        log.info("webdriver-manager로 ChromeDriver 준비")
        driver = webdriver.Chrome(service=Service(ChromeDriverManager().install()), options=options)
        return hide_webdriver_flag(driver)
    except Exception as e:
        errors.append(f"webdriver-manager={e}")
        raise RuntimeError("ChromeDriver 생성 실패: " + " | ".join(errors)) from e


def send_discord(nickname, content):
    if not content.strip():
        return

    data = {"username": f"[YouTube] {nickname}", "content": content}

    for attempt in range(3):
        try:
            resp = requests.post(DISCORD_WEBHOOK_URL, json=data, timeout=10)
            if resp.status_code == 429:
                retry_after = resp.json().get("retry_after", 1)
                log.warning(f"Discord rate-limit, {retry_after:.1f}초 대기")
                time.sleep(retry_after)
                continue

            resp.raise_for_status()
            return
        except requests.RequestException as e:
            log.error(f"Discord 전송 실패 (시도 {attempt + 1}/3): {e}")
            time.sleep(1)


def wait_for_youtube_chat(driver, video_id):
    try:
        WebDriverWait(driver, PAGE_LOAD_WAIT).until(
            EC.presence_of_element_located((By.TAG_NAME, "yt-live-chat-renderer"))
        )
        return True
    except TimeoutException:
        title = driver.title
        body_text = driver.find_element(By.TAG_NAME, "body").text[:300] if driver.find_elements(By.TAG_NAME, "body") else ""
        log.error(f"[{video_id}] 채팅창 접속 실패 (타임아웃) title={title!r} body={body_text!r}")
        return False


def monitor_chat_thread(video_id):
    driver = None
    try:
        driver = get_driver(is_headless=True)

        popout_url = f"https://www.youtube.com/live_chat?is_popout=1&v={video_id}"
        driver.get(popout_url)
        log.info(f"[{video_id}] 채팅창 접속 중...")

        if not wait_for_youtube_chat(driver, video_id):
            return

        log.info(f"[{video_id}] 채팅창 접속 성공!")
        time.sleep(INITIAL_LOAD_DELAY)
        processed_ids = deque(maxlen=MAX_PROCESSED_IDS)

        initial_items = driver.find_elements(By.TAG_NAME, "yt-live-chat-text-message-renderer")
        for item in initial_items:
            processed_ids.append(item.id)

        log.info(f"[{video_id}] 기존 메시지 {len(initial_items)}개 건너뜀")
        log.info(f"[{video_id}] 실시간 감시 시작")

        consecutive_errors = 0

        while True:
            try:
                if not driver.window_handles:
                    log.warning(f"[{video_id}] 브라우저 창이 닫혔습니다.")
                    break

                chat_items = driver.find_elements(By.TAG_NAME, "yt-live-chat-text-message-renderer")
                for item in chat_items[-RECENT_CHAT_COUNT:]:
                    try:
                        if item.id in processed_ids:
                            continue

                        nick_el = item.find_element(By.ID, "author-name")
                        nickname = nick_el.get_attribute("textContent").strip()

                        msg_el = item.find_element(By.ID, "message")
                        content_parts = []

                        raw_text = msg_el.get_attribute("textContent")
                        if raw_text and raw_text.strip():
                            content_parts.append(raw_text.strip())

                        for emoji in msg_el.find_elements(By.TAG_NAME, "img"):
                            emoji_name = emoji.get_attribute("alt")
                            content_parts.append(emoji_name if emoji_name else "(이모티콘)")

                        final_content = " ".join(content_parts).strip()
                        if not final_content:
                            continue

                        log.info(f"[{video_id}] {nickname}: {final_content}")
                        send_discord(nickname, final_content)
                        processed_ids.append(item.id)
                    except Exception:
                        continue

                consecutive_errors = 0
                time.sleep(POLL_INTERVAL)
            except Exception as e:
                consecutive_errors += 1
                log.warning(f"[{video_id}] 루프 오류 ({consecutive_errors}회): {e}")

                if consecutive_errors >= MAX_CHAT_ERRORS:
                    log.error(f"[{video_id}] 연속 오류 초과, 감시 종료")
                    break

                time.sleep(1)
    except Exception as e:
        log.error(f"[{video_id}] 스레드 오류: {e}")
    finally:
        with active_streams_lock:
            active_streams.discard(video_id)
        log.info(f"[{video_id}] 감시 종료")
        if driver:
            try:
                driver.quit()
            except Exception:
                pass


def find_live_video_ids(driver):
    live_badges = driver.find_elements(
        By.XPATH,
        "//div[contains(@class, 'yt-badge-shape__text') and "
        "(normalize-space()='LIVE' or normalize-space()='라이브')]",
    )

    video_ids = []
    for badge in live_badges:
        try:
            link_element = badge.find_element(By.XPATH, "./ancestor::a[@id='thumbnail']")
            href = link_element.get_attribute("href")
            if href and "watch?v=" in href:
                video_ids.append(href.split("v=")[1].split("&")[0])
        except Exception as e:
            log.warning(f"라이브 배지 처리 중 오류: {e}")
    return video_ids


def main():
    log.info("유튜브 채팅 봇을 시작합니다...")
    log.info(f"감시 채널: {CHANNEL_STREAMS_URL}")

    driver = None
    try:
        driver = get_driver(is_headless=True)

        while True:
            try:
                driver.get(CHANNEL_STREAMS_URL)
                time.sleep(CHANNEL_LOAD_DELAY)

                video_ids = find_live_video_ids(driver)
                if video_ids:
                    log.info(f"라이브 배지 {len(video_ids)}개 발견")

                for video_id in video_ids:
                    with active_streams_lock:
                        if video_id in active_streams:
                            continue
                        active_streams.add(video_id)

                    log.info(f"새 라이브 발견: {video_id}")
                    t = threading.Thread(
                        target=monitor_chat_thread,
                        args=(video_id,),
                        daemon=True,
                        name=f"chat-{video_id}",
                    )
                    t.start()

                with active_streams_lock:
                    active_count = len(active_streams)
                if active_count > 0:
                    log.info(f"현재 감시 중인 방송: {active_count}개")

                time.sleep(SCAN_INTERVAL)
            except Exception as e:
                log.warning(f"스캔 루프 오류: {e}")
                time.sleep(5)
    except KeyboardInterrupt:
        log.info("사용자에 의해 종료합니다.")
    finally:
        if driver:
            try:
                driver.quit()
            except Exception:
                pass


if __name__ == "__main__":
    main()
