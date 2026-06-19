import logging
import os
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
CHZZK_CHAT_URL = "https://chzzk.naver.com/live/75abbd8aea5b57ee1922b421a270e6fc/chat"
DISCORD_WEBHOOK_URL = ""

# 동작 설정
POLL_INTERVAL = 0.3
RECENT_CHAT_COUNT = 30
MAX_PROCESSED_IDS = 500
PAGE_LOAD_WAIT = 45
INITIAL_LOAD_DELAY = 5
MAX_RECONNECT_ATTEMPTS = 5
RECONNECT_DELAY = 10
WDM_STALE_LOCK_SECONDS = 180
# ==========================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("chzzk-bot")


CHAT_WRAPPER_SELECTORS = (
    "[class*='live_chatting_list_wrapper']",
    "[class*='chatting_list_wrapper']",
    "[class*='live_chatting_list']",
    "[class*='_container_sg7hy_']",
    "[class*='_wrapper_sg7hy_']",
    "[class*='_chatting_message_']",
)
CHAT_ITEM_SELECTOR = "[class*='live_chatting_list_item'], [class*='chatting_list_item'], [class*='_item_sg7hy_']"
NICKNAME_SELECTOR = "[class*='name_text'], [class*='nickname'], [class*='name'], [class*='_nickname_']"
MESSAGE_TEXT_SELECTOR = "[class*='live_chatting_message_text'], [class*='chatting_message_text'], [class*='message_text'], [class*='_text_1s877_']"
EMOJI_SELECTOR = "[class*='live_chatting_message_button'], [class*='emoji'], img"


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

    options.add_argument("--window-size=1280,900")
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


def get_driver():
    options = build_chrome_options(is_headless=True)
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

    data = {"username": f"[치지직] {nickname}", "content": content}

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


def wait_for_chat_loaded(driver):
    wait = WebDriverWait(driver, PAGE_LOAD_WAIT)
    wait.until(lambda d: d.execute_script("return document.readyState") in ("interactive", "complete"))

    def has_chat_surface(d):
        for selector in CHAT_WRAPPER_SELECTORS:
            if d.find_elements(By.CSS_SELECTOR, selector):
                return True
        if d.find_elements(By.CSS_SELECTOR, CHAT_ITEM_SELECTOR):
            return True
        return False

    try:
        wait.until(has_chat_surface)
        return True
    except TimeoutException:
        title = driver.title
        body_text = driver.find_element(By.TAG_NAME, "body").text[:300] if driver.find_elements(By.TAG_NAME, "body") else ""
        log.error(f"접속 실패 (타임아웃) title={title!r} body={body_text!r}")
        return False


def extract_texts(parent, selector):
    values = []
    for element in parent.find_elements(By.CSS_SELECTOR, selector):
        text = element.get_attribute("textContent")
        if text and text.strip():
            values.append(text.strip())
    return values


def extract_message_parts(item):
    message_elements = item.find_elements(By.CSS_SELECTOR, MESSAGE_TEXT_SELECTOR)
    content_parts = []
    emoji_count = 0

    for element in message_elements:
        text = element.get_attribute("textContent")
        if text and text.strip():
            content_parts.append(text.strip())

        emoji_count += len(element.find_elements(By.CSS_SELECTOR, "img"))

    if emoji_count:
        content_parts.append(f"(이모티콘 {emoji_count}개)")

    return content_parts


def connect_and_monitor(driver):
    driver.get(CHZZK_CHAT_URL)

    if not wait_for_chat_loaded(driver):
        return False

    log.info("채팅창 접속 성공!")
    time.sleep(INITIAL_LOAD_DELAY)

    processed_ids = deque(maxlen=MAX_PROCESSED_IDS)
    initial_items = driver.find_elements(By.CSS_SELECTOR, CHAT_ITEM_SELECTOR)
    for item in initial_items:
        processed_ids.append(item.id)

    log.info(f"기존 메시지 {len(initial_items)}개 건너뜀")
    log.info("[실시간 감시 시작]")

    consecutive_errors = 0

    while True:
        try:
            if not driver.window_handles:
                log.warning("브라우저 창이 닫혔습니다.")
                return False

            chat_items = driver.find_elements(By.CSS_SELECTOR, CHAT_ITEM_SELECTOR)
            for item in chat_items[-RECENT_CHAT_COUNT:]:
                try:
                    if item.id in processed_ids:
                        continue

                    nicknames = extract_texts(item, NICKNAME_SELECTOR)
                    nickname = nicknames[0] if nicknames else "Unknown"

                    content_parts = extract_message_parts(item)

                    final_content = " ".join(content_parts).strip()
                    if not final_content:
                        continue

                    log.info(f"{nickname}: {final_content}")
                    send_discord(nickname, final_content)
                    processed_ids.append(item.id)
                except Exception:
                    continue

            consecutive_errors = 0
            time.sleep(POLL_INTERVAL)
        except Exception as e:
            consecutive_errors += 1
            log.warning(f"루프 오류 ({consecutive_errors}회 연속): {e}")

            if consecutive_errors >= 10:
                log.error("연속 오류 10회 초과, 재연결을 시도합니다.")
                return False

            time.sleep(1)


def main():
    log.info("치지직 채팅 봇을 시작합니다...")
    reconnect_count = 0

    while reconnect_count < MAX_RECONNECT_ATTEMPTS:
        driver = None
        try:
            driver = get_driver()
            success = connect_and_monitor(driver)
            if success:
                break

            reconnect_count += 1
            if reconnect_count < MAX_RECONNECT_ATTEMPTS:
                log.info(f"재연결 시도 ({reconnect_count}/{MAX_RECONNECT_ATTEMPTS}) - {RECONNECT_DELAY}초 후 재시도...")
                time.sleep(RECONNECT_DELAY)
        except KeyboardInterrupt:
            log.info("사용자에 의해 종료합니다.")
            break
        except Exception as e:
            reconnect_count += 1
            log.error(f"예상치 못한 오류: {e}")
            if reconnect_count < MAX_RECONNECT_ATTEMPTS:
                time.sleep(RECONNECT_DELAY)
        finally:
            if driver:
                try:
                    driver.quit()
                except Exception:
                    pass

    if reconnect_count >= MAX_RECONNECT_ATTEMPTS:
        log.error(f"최대 재연결 횟수({MAX_RECONNECT_ATTEMPTS}) 초과, 봇을 종료합니다.")


if __name__ == "__main__":
    main()
