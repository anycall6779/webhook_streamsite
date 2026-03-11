import time
import logging
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
CHZZK_CHAT_URL = "https://chzzk.naver.com/live/75abbd8aea5b57ee1922b421a270e6fc/chat"
DISCORD_WEBHOOK_URL = "YOUTH_DISCORD_WEBHOOK_URL_HERE"

# [동작 설정]
POLL_INTERVAL = 0.3        # 채팅 확인 주기 (초)
RECENT_CHAT_COUNT = 30     # 루프마다 확인할 최신 채팅 수
MAX_PROCESSED_IDS = 500    # 중복 방지용 ID 보관 수
PAGE_LOAD_WAIT = 15        # 페이지 로딩 타임아웃 (초)
INITIAL_LOAD_DELAY = 3     # 초기 로딩 대기 (초)
MAX_RECONNECT_ATTEMPTS = 5 # 최대 재연결 시도 횟수
RECONNECT_DELAY = 10       # 재연결 대기 시간 (초)
# ==========================================

# 로깅 설정
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("chzzk-bot")


def get_driver():
    """봇 탐지 우회가 적용된 Chrome WebDriver를 생성합니다."""
    options = webdriver.ChromeOptions()

    # 봇 탐지 우회
    options.add_argument("--disable-blink-features=AutomationControlled")
    options.add_experimental_option("excludeSwitches", ["enable-automation"])
    options.add_experimental_option("useAutomationExtension", False)

    options.add_argument("--headless=new")
    options.add_argument("--window-size=1000,800")
    options.add_argument("--log-level=3")
    options.add_argument("--disable-gpu")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument(
        "user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
    )

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

    data = {"username": f"[치지직] {nickname}", "content": content}

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


def connect_and_monitor(driver):
    """채팅창에 접속하고 모니터링 루프를 실행합니다."""
    driver.get(CHZZK_CHAT_URL)

    try:
        WebDriverWait(driver, PAGE_LOAD_WAIT).until(
            EC.presence_of_element_located((By.CLASS_NAME, "live_chatting_list_wrapper__a5XTV"))
        )
        log.info("✅ 채팅창 접속 성공!")
    except Exception:
        log.error("❌ 접속 실패 (타임아웃)")
        return False

    time.sleep(INITIAL_LOAD_DELAY)

    processed_ids = deque(maxlen=MAX_PROCESSED_IDS)

    # 기존 메시지 스킵
    initial_items = driver.find_elements(By.CLASS_NAME, "live_chatting_list_item__0SGhw")
    for item in initial_items:
        processed_ids.append(item.id)
    log.info(f"⏩ 기존 메시지 {len(initial_items)}개 건너뜀")
    log.info("🔴 [실시간 감시 시작]")

    consecutive_errors = 0

    while True:
        try:
            if not driver.window_handles:
                log.warning("⚠️ 브라우저 창이 닫혔습니다.")
                return False

            chat_items = driver.find_elements(By.CLASS_NAME, "live_chatting_list_item__0SGhw")

            for item in chat_items[-RECENT_CHAT_COUNT:]:
                try:
                    if item.id in processed_ids:
                        continue

                    # 닉네임 추출
                    nick_el = item.find_element(By.CLASS_NAME, "name_text__yQG50")
                    nickname = nick_el.get_attribute("textContent").strip()

                    # 메시지 내용 조합
                    content_parts = []

                    text_els = item.find_elements(By.CLASS_NAME, "live_chatting_message_text__DyleH")
                    for t in text_els:
                        t_text = t.get_attribute("textContent")
                        if t_text and t_text.strip():
                            content_parts.append(t_text.strip())

                    emoji_els = item.find_elements(By.CLASS_NAME, "live_chatting_message_button__WY3rb")
                    if emoji_els:
                        content_parts.append(f"(이모티콘 {len(emoji_els)}개)")

                    final_content = " ".join(content_parts)

                    # 내용이 비어있으면 다음 루프에서 재시도
                    if not final_content:
                        continue

                    log.info(f"📨 {nickname}: {final_content}")
                    send_discord(nickname, final_content)
                    processed_ids.append(item.id)

                except Exception:
                    continue

            consecutive_errors = 0
            time.sleep(POLL_INTERVAL)

        except Exception as e:
            consecutive_errors += 1
            log.warning(f"⚠️ 루프 오류 ({consecutive_errors}회 연속): {e}")

            if consecutive_errors >= 10:
                log.error("❌ 연속 오류 10회 초과 — 재연결을 시도합니다.")
                return False

            time.sleep(1)

    return True


def main():
    log.info("🔄 치지직 채팅 봇을 시작합니다...")

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
                log.info(
                    f"🔄 재연결 시도 ({reconnect_count}/{MAX_RECONNECT_ATTEMPTS}) "
                    f"— {RECONNECT_DELAY}초 후 재시도..."
                )
                time.sleep(RECONNECT_DELAY)

        except KeyboardInterrupt:
            log.info("👋 사용자에 의해 종료됩니다.")
            break

        except Exception as e:
            reconnect_count += 1
            log.error(f"❌ 예상치 못한 오류: {e}")
            if reconnect_count < MAX_RECONNECT_ATTEMPTS:
                time.sleep(RECONNECT_DELAY)

        finally:
            if driver:
                try:
                    driver.quit()
                except Exception:
                    pass

    if reconnect_count >= MAX_RECONNECT_ATTEMPTS:
        log.error(f"🛑 최대 재연결 횟수({MAX_RECONNECT_ATTEMPTS}회) 초과 — 봇을 종료합니다.")


if __name__ == "__main__":
    main()