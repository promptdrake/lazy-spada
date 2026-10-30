import base64
import json
import os
import re
import sys
import time
from getpass import getpass
from urllib.parse import parse_qs, urlencode, urlparse, urlunparse

from selenium import webdriver
from selenium.common.exceptions import (
    NoSuchElementException,
    StaleElementReferenceException,
    TimeoutException,
    WebDriverException,
)
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import Select, WebDriverWait

LOGIN_URL = "https://spada12.ums.ac.id/auth/login_ums"
DASHBOARD_URL = "https://spada12.ums.ac.id/"
COURSE_KEYWORD = "Struktur Diskret"
COURSE_CARD_SELECTOR = "a[href*='/course/view.php?id=']"

USERNAME_SELECTOR = "#username"
PASSWORD_SELECTOR = "#password"
SUBMIT_SELECTOR = (
    "body > div > div.bg-white.p-8.rounded-lg.space-y-6 > div:nth-child(2) > form > "
    "div.flex.flex-col.pt-4.space-y-2 > button"
)

FORUM_LINK_SELECTOR = "li.courseindex-item a.courseindex-link[href*='/mod/forum/']"
DISCUSSION_LINK_SELECTOR = "a[href*='/mod/forum/discuss.php?d=']"
RATING_MENU_ID = "menurating{post_id}"
CLICKABLE_SELECTOR = "a, button, [role='menuitem'], [role='option'], label, li, span"

RATING_VALUE = "100"
SKIP_LOCKED = False
HEADLESS = False
KEEP_BROWSER_OPEN = True
WAIT_TIMEOUT = 25
OPTION_TIMEOUT = 2.5
MAX_FORUM_PAGES = 50
RESPONSE_PREVIEW = 300
RATING_URL_HINTS = ("rate", "rating", "penilaian")
IGNORED_HOSTS = ("google-analytics.com", "googletagmanager.com", "doubleclick.net", "gstatic.com")
DEBUG_DIR = "debug"
POST_AUTHOR_SELECTOR = "a[href*='user/view.php']"
POST_BODY_SELECTOR = ".post-content, .posting .content, .content, .posting"


def build_driver():
    options = webdriver.ChromeOptions()
    if HEADLESS:
        options.add_argument("--headless=new")
    options.add_argument("--start-maximized")
    options.add_argument("--disable-notifications")
    options.add_argument("--disable-infobars")
    options.add_experimental_option("excludeSwitches", ["enable-logging"])
    options.set_capability(
        "goog:loggingPrefs", {"performance": "ALL", "browser": "ALL"}
    )
    driver = webdriver.Chrome(options=options)
    driver.set_page_load_timeout(180)
    try:
        driver.execute_cdp_cmd("Network.enable", {})
    except WebDriverException:
        print("[!] Network domain tidak aktif, log response dibatasi.")
    return driver


def normalize(text):
    return " ".join((text or "").split())


def url_with_page(url, page):
    parts = urlparse(url)
    query = parse_qs(parts.query)
    query["page"] = [str(page)]
    return urlunparse(parts._replace(query=urlencode(query, doseq=True)))


def query_value(url, key):
    values = parse_qs(urlparse(url).query).get(key)
    return values[0] if values else None


def drain_performance_log(driver):
    entries = []
    try:
        for entry in driver.get_log("performance"):
            try:
                payload = json.loads(entry["message"])["message"]
            except (KeyError, ValueError):
                continue
            entries.append((entry.get("timestamp", 0), payload))
    except WebDriverException:
        pass
    return entries


def response_body(driver, request_id):
    try:
        result = driver.execute_cdp_cmd("Network.getResponseBody", {"requestId": request_id})
        body = result.get("body", "")
        if result.get("base64Encoded"):
            body = base64.b64decode(body).decode("utf-8", "replace")
        return body
    except WebDriverException:
        return ""


def describe_response(driver, entries, since_ms):
    described = []
    for timestamp, payload in entries:
        if timestamp < since_ms:
            continue
        if payload.get("method") != "Network.responseReceived":
            continue

        params = payload.get("params", {})
        response = params.get("response", {})
        url = response.get("url", "")
        resource_type = params.get("type", "")
        if resource_type not in ("XHR", "Fetch", "Document"):
            continue
        if any(host in url for host in IGNORED_HOSTS):
            continue

        is_rating = any(hint in url.lower() for hint in RATING_URL_HINTS)
        body = response_body(driver, params.get("requestId", "")) if is_rating else ""
        described.append(
            {
                "url": url,
                "status": response.get("status"),
                "type": resource_type,
                "body": normalize(body)[:RESPONSE_PREVIEW],
                "is_rating": is_rating,
            }
        )
    return described


def find_post_container(driver, post_id, menu):
    for selector in (
        f"article[data-post-id='{post_id}']",
        f"article#p{post_id}",
        f"#p{post_id}",
        f"[data-post-id='{post_id}']",
    ):
        elements = driver.find_elements(By.CSS_SELECTOR, selector)
        for element in elements:
            if element.is_displayed():
                return element

    container = menu
    for _ in range(12):
        try:
            container = container.find_element(By.XPATH, "..")
        except (NoSuchElementException, StaleElementReferenceException):
            break
        try:
            text = normalize(container.text)
        except StaleElementReferenceException:
            break
        if len(text) > 60 and container.find_elements(By.CSS_SELECTOR, POST_AUTHOR_SELECTOR):
            return container
    return None


def post_info(driver, post_id, menu):
    author = "?"
    body = ""
    container = find_post_container(driver, post_id, menu)

    if container is not None:
        for element in container.find_elements(By.CSS_SELECTOR, POST_AUTHOR_SELECTOR):
            try:
                text = normalize(element.text)
            except StaleElementReferenceException:
                continue
            if text:
                author = text
                break

        for element in container.find_elements(By.CSS_SELECTOR, POST_BODY_SELECTOR):
            try:
                text = normalize(element.text)
            except StaleElementReferenceException:
                continue
            if len(text) > len(body):
                body = text
        if not body:
            body = normalize(container.text)

    return author, body[:120]


def accept_alert(driver, timeout=1.5):
    try:
        WebDriverWait(driver, timeout).until(EC.alert_is_present()).accept()
        return True
    except (TimeoutException, WebDriverException):
        return False


def dismiss_alerts(driver):
    for _ in range(3):
        if not accept_alert(driver, 0.4):
            return


def login(driver, wait):
    print("[+] Membuka halaman login...")
    driver.get(LOGIN_URL)

    username = wait.until(EC.element_to_be_clickable((By.CSS_SELECTOR, USERNAME_SELECTOR)))
    username.clear()
    username.send_keys(NIM)

    password = wait.until(EC.element_to_be_clickable((By.CSS_SELECTOR, PASSWORD_SELECTOR)))
    password.clear()
    password.send_keys(PASSWORD)

    submit = wait.until(EC.element_to_be_clickable((By.CSS_SELECTOR, SUBMIT_SELECTOR)))
    submit.click()

    try:
        WebDriverWait(driver, WAIT_TIMEOUT).until(lambda d: "login_ums" not in d.current_url)
    except TimeoutException:
        print("[!] Masih di halaman login. Cek NIM/password atau selector tombol.")
    time.sleep(2)
    print(f"[+] Selesai login -> {driver.current_url}")


def find_course_url(driver, wait, keyword):
    print(f"[+] Mencari kartu mata kuliah '{keyword}' di dashboard...")

    candidates = []
    for attempt in range(2):
        if attempt:
            driver.get(DASHBOARD_URL)

        try:
            WebDriverWait(driver, WAIT_TIMEOUT).until(
                EC.presence_of_element_located((By.CSS_SELECTOR, COURSE_CARD_SELECTOR))
            )
        except TimeoutException:
            print("[!] Kartu mata kuliah belum muncul, coba muat ulang halaman...")
            continue

        candidates.clear()
        for anchor in driver.find_elements(By.CSS_SELECTOR, COURSE_CARD_SELECTOR):
            href = anchor.get_attribute("href")
            text = normalize(anchor.text)
            if not href:
                continue
            candidates.append((text, href))

        for text, href in candidates:
            if keyword.lower() in text.lower() and query_value(href, "token"):
                print(f"[+] Ketemu: {text}")
                print(f"    {href}")
                return href

        for text, href in candidates:
            if keyword.lower() in text.lower():
                print(f"[!] Kartu '{text}' tidak punya token, pakai apa adanya: {href}")
                return href

        if attempt == 0:
            print(f"[!] '{keyword}' belum terlihat, memindai ulang dari {DASHBOARD_URL}")

    titles = sorted({text for text, _ in candidates if text})
    if titles:
        print("[!] Kartu yang tersedia di dashboard:")
        for title in titles:
            print(f"    - {title}")
    return None


def collect_forum_links(driver, wait):
    wait.until(EC.presence_of_element_located((By.CSS_SELECTOR, "li.courseindex-item")))
    time.sleep(1)

    forums = []
    seen = set()
    candidates = driver.find_elements(By.CSS_SELECTOR, FORUM_LINK_SELECTOR)
    if not candidates:
        candidates = driver.find_elements(By.CSS_SELECTOR, "a[href*='/mod/forum/view.php']")

    for item in candidates:
        try:
            href = item.get_attribute("href")
            name = normalize(item.text) or href
        except (StaleElementReferenceException, NoSuchElementException):
            continue

        if not href or href in seen:
            continue

        if SKIP_LOCKED:
            try:
                parent = item.find_element(By.XPATH, "./ancestor::li[1]")
            except (StaleElementReferenceException, NoSuchElementException):
                parent = None
            if parent is not None and parent.find_elements(By.CSS_SELECTOR, ".courseindex-locked"):
                print(f"[!] Lewati (terkunci): {name}")
                continue

        seen.add(href)
        forums.append((name, href))

    return forums


def collect_post_ids(driver):
    post_ids = []
    for element in driver.find_elements(By.CSS_SELECTOR, "[id*='menurating']"):
        match = re.search(r"menurating(\d+)", element.get_attribute("id") or "", re.IGNORECASE)
        if match and match.group(1) not in post_ids:
            post_ids.append(match.group(1))
    return post_ids


def rating_context(element):
    node = element
    for _ in range(6):
        try:
            node = node.find_element(By.XPATH, "..")
        except (NoSuchElementException, StaleElementReferenceException):
            return False
        identity = (node.get_attribute("id") or "") + " " + (node.get_attribute("class") or "")
        identity = identity.lower()
        if "rating" in identity or "rate" in identity:
            return True
    return False


def match_rating_option(elements, require_context):
    loose = None
    for element in elements:
        try:
            if not element.is_displayed():
                continue
            text = normalize(element.text)
        except StaleElementReferenceException:
            continue

        if not text:
            continue

        candidates = {RATING_VALUE, RATING_VALUE + "%", RATING_VALUE + " / 100", RATING_VALUE + "/100"}
        if text not in candidates and not text.startswith(RATING_VALUE + " "):
            continue

        if not require_context or rating_context(element):
            return element
        if loose is None:
            loose = element

    return loose


RATING_CONTAINER_SELECTORS = (
    "[id*='rating'][id*='{post_id}']",
    "[id*='ratingmenu']",
    "[class*='rating']",
    ".dropdown-menu.show",
    ".popover:not([style*='display: none'])",
)

VALUE_ATTRIBUTES = ("data-value", "data-rating", "data-rate", "value")


def value_of(element):
    for attribute in VALUE_ATTRIBUTES:
        try:
            value = element.get_attribute(attribute)
        except StaleElementReferenceException:
            return None
        if value and value.strip() == RATING_VALUE:
            return value.strip()
    return None


def click_element(driver, element):
    driver.execute_script("arguments[0].scrollIntoView({block: 'center', inline: 'nearest'});", element)
    try:
        element.click()
    except WebDriverException:
        driver.execute_script("arguments[0].click();", element)


def find_value_option(driver, scope, post_id):
    for container in scope:
        try:
            if not container.is_displayed():
                continue
        except StaleElementReferenceException:
            continue

        for tag in ("input", "option", "label", "button", "a", "span", "li", "div"):
            for element in container.find_elements(By.TAG_NAME, tag):
                value = value_of(element)
                if value:
                    return element

        for select in container.find_elements(By.TAG_NAME, "select"):
            try:
                if not select.is_displayed():
                    continue
                for option in select.find_elements(By.TAG_NAME, "option"):
                    value = value_of(option) or normalize(option.text)
                    if value == RATING_VALUE:
                        return option
            except StaleElementReferenceException:
                continue

    return None


def rating_scope(driver, post_id):
    scope = []
    for template in RATING_CONTAINER_SELECTORS:
        selector = template.format(post_id=post_id)
        for element in driver.find_elements(By.CSS_SELECTOR, selector):
            try:
                if element.is_displayed():
                    scope.append(element)
            except StaleElementReferenceException:
                continue

    try:
        menu = driver.find_element(By.ID, RATING_MENU_ID.format(post_id=post_id))
        context = menu
        for _ in range(6):
            context = context.find_element(By.XPATH, "..")
            scope.append(context)
    except (NoSuchElementException, StaleElementReferenceException):
        pass

    return scope


def find_rating_option(driver, post_id):
    scope = rating_scope(driver, post_id)

    option = find_value_option(driver, scope, post_id)
    if option is not None:
        return option

    for container in scope:
        try:
            candidate = match_rating_option(
                container.find_elements(By.CSS_SELECTOR, CLICKABLE_SELECTOR),
                require_context=False,
            )
        except StaleElementReferenceException:
            continue
        if candidate:
            return candidate

    return match_rating_option(
        driver.find_elements(By.CSS_SELECTOR, CLICKABLE_SELECTOR),
        require_context=True,
    )


def numeric_value(*candidates):
    for candidate in candidates:
        if not candidate:
            continue
        match = re.search(r"\d+", candidate)
        if match:
            return int(match.group(0))
    return None


def choose_from_select(driver, select_element):
    select = Select(select_element)
    options = []
    for option in select.options:
        try:
            options.append((option.get_attribute("value") or "", normalize(option.text)))
        except StaleElementReferenceException:
            continue

    chosen = None
    for value, text in options:
        if value.strip() == RATING_VALUE or text == RATING_VALUE:
            chosen = (value, text)
            break

    if chosen is None:
        scored = []
        for value, text in options:
            number = numeric_value(value, text)
            if number is not None and number > 0:
                scored.append((number, value, text))
        if scored:
            number, value, text = max(scored)
            chosen = (value, text)
            print(f"      [!] opsi {RATING_VALUE} tidak ada, memakai maksimum {text or value}")

    if chosen is None:
        return None

    value, text = chosen
    try:
        if value.strip():
            select.select_by_value(value)
        else:
            select.select_by_visible_text(text)
    except WebDriverException:
        driver.execute_script(
            "arguments[0].value = arguments[1];"
            "arguments[0].dispatchEvent(new Event('change', {bubbles: true}));",
            select_element,
            value,
        )
    return text or value


def first_select_in(scope):
    for container in scope:
        for select in container.find_elements(By.TAG_NAME, "select"):
            return select
    return None


def apply_rating(driver, menu, post_id):
    tag = (menu.tag_name or "").lower()

    if tag == "select":
        chosen = choose_from_select(driver, menu)
        if chosen:
            return chosen

    if tag == "input":
        input_type = (menu.get_attribute("type") or "").lower()
        if input_type in ("radio", "checkbox"):
            click_element(driver, menu)
            return menu.get_attribute("value") or RATING_VALUE

    click_element(driver, menu)

    deadline = time.time() + OPTION_TIMEOUT
    while time.time() < deadline:
        select_element = first_select_in(rating_scope(driver, post_id))
        if select_element is not None:
            chosen = choose_from_select(driver, select_element)
            if chosen:
                return chosen

        option = find_rating_option(driver, post_id)
        if option is not None:
            if (option.tag_name or "").lower() == "option":
                try:
                    chosen = choose_from_select(driver, option.find_element(By.XPATH, ".."))
                except (NoSuchElementException, WebDriverException):
                    chosen = None
                if chosen:
                    return chosen
            click_element(driver, option)
            return normalize(option.text) or value_of(option) or RATING_VALUE

        time.sleep(0.05)

    return None


def dump_rating_debug(driver, post_id, scope):
    try:
        os.makedirs(DEBUG_DIR, exist_ok=True)
    except OSError:
        return None

    path = os.path.join(DEBUG_DIR, f"rating_{post_id}.html")
    chunks = []
    for index, container in enumerate(scope[:4]):
        try:
            chunks.append(container.get_attribute("outerHTML"))
        except (StaleElementReferenceException, WebDriverException):
            chunks.append(f"<!-- container {index} tidak terbaca -->")
    try:
        with open(path, "w", encoding="utf-8") as handle:
            handle.write("\n\n".join(chunks) or driver.page_source)
    except OSError:
        return None
    return path


def print_responses(responses):
    if not responses:
        print("      <- (tidak ada response XHR terdeteksi)")
        return

    for response in responses:
        marker = "RATING" if response["is_rating"] else "      "
        url = response["url"]
        if len(url) > 110:
            url = url[:107] + "..."
        print(
            f"      <- [{marker}] {response['status']} {response['type']} "
            f"{url} :: {response['body'] or '(body tidak tersedia)'}"
        )


def rate_post(driver, wait, discussion, post_id):
    menu = wait.until(
        EC.presence_of_element_located((By.ID, RATING_MENU_ID.format(post_id=post_id)))
    )
    author, snippet = post_info(driver, post_id, menu)
    drain_performance_log(driver)
    started_at = int(time.time() * 1000)

    chosen = apply_rating(driver, menu, post_id)
    if chosen is None:
        path = dump_rating_debug(driver, post_id, rating_scope(driver, post_id))
        raise NoSuchElementException(
            f"opsi rating {RATING_VALUE} tidak ditemukan untuk post {post_id} "
            f"(debug: {path or 'gagal menulis file'})"
        )

    time.sleep(0.2)
    dismiss_alerts(driver)
    responses = describe_response(driver, drain_performance_log(driver), started_at)

    try:
        driver.find_element(By.TAG_NAME, "body").send_keys(Keys.ESCAPE)
    except WebDriverException:
        pass

    print(
        f"      [v] diskusi \"{discussion}\" | owner post {post_id} | "
        f"{author}: \"{snippet}\" -> rating {chosen}"
    )
    print_responses(responses)

    return {
        "discussion": discussion,
        "post_id": post_id,
        "author": author,
        "snippet": snippet,
        "rating": chosen,
        "responses": responses,
    }


def starter_post_id(driver):
    try:
        WebDriverWait(driver, OPTION_TIMEOUT).until(
            EC.presence_of_element_located((By.CSS_SELECTOR, "[id*='menurating']"))
        )
    except TimeoutException:
        return None

    posts = driver.find_elements(By.CSS_SELECTOR, "article.forumpost")
    if not posts:
        posts = driver.find_elements(By.CSS_SELECTOR, ".forumpost")

    for post in posts:
        for menu in post.find_elements(By.CSS_SELECTOR, "[id*='menurating']"):
            match = re.search(r"menurating(\d+)", menu.get_attribute("id") or "", re.IGNORECASE)
            if match:
                return match.group(1)

    post_ids = collect_post_ids(driver)
    return post_ids[0] if post_ids else None


def rate_discussion(driver, wait, name, url):
    driver.get(url)
    wait.until(EC.presence_of_element_located((By.CSS_SELECTOR, "body")))
    dismiss_alerts(driver)

    post_id = starter_post_id(driver)
    if post_id is None:
        posts = driver.find_elements(By.CSS_SELECTOR, "article.forumpost, .forumpost, .post")
        print(f"    Diskusi: {name} | tidak ada tombol rating ({len(posts)} post terlihat)")
        print(f"      [!] url: {driver.current_url}")
        return []

    print(f"    Diskusi: {name} | owner post {post_id}")

    try:
        return [rate_post(driver, wait, name, post_id)]
    except (TimeoutException, NoSuchElementException, StaleElementReferenceException) as error:
        print(f"      [x] diskusi \"{name}\" | owner post {post_id} gagal: {error}")
    except WebDriverException as error:
        print(f"      [x] diskusi \"{name}\" | owner post {post_id} error browser: {error}")

    return []


def process_forum(driver, wait, name, url):
    print(f"[+] Forum: {name}")
    seen = set()
    results = []

    for page in range(MAX_FORUM_PAGES):
        driver.get(url_with_page(url, page))
        wait.until(EC.presence_of_element_located((By.CSS_SELECTOR, "body")))
        time.sleep(0.25)
        dismiss_alerts(driver)

        discussions = []
        for anchor in driver.find_elements(By.CSS_SELECTOR, DISCUSSION_LINK_SELECTOR):
            href = anchor.get_attribute("href")
            discussion_id = query_value(href or "", "d")
            if not discussion_id or discussion_id in seen:
                continue
            seen.add(discussion_id)
            discussions.append((normalize(anchor.text) or href, href))

        if not discussions:
            break

        for discussion_name, discussion_url in discussions:
            results.extend(rate_discussion(driver, wait, discussion_name, discussion_url))

    print(f"[+] Selesai forum: {name} ({len(results)} post dirating)")
    return results


def print_summary(results):
    if not results:
        print("\n[!] Tidak ada post yang berhasil dirating.")
        return

    print(f"\n{'=' * 78}")
    print(f"RINGKASAN {len(results)} POST DIRATING")
    print(f"{'=' * 78}")

    grouped = {}
    for result in results:
        grouped.setdefault(result["discussion"], []).append(result)

    for discussion, items in grouped.items():
        print(f"\nDiskusi: {discussion}")
        for item in items:
            status = "OK"
            for response in item["responses"]:
                if response["is_rating"] and isinstance(response["status"], int) and response["status"] >= 400:
                    status = f"HTTP {response['status']}"
            print(
                f"  - post {item['post_id']} | {item['author']} | "
                f"rating {item.get('rating', RATING_VALUE)} | {status} | \"{item['snippet']}\""
            )


def main():
    print("=== Budi Slayer Rating ums ===")
    global NIM, PASSWORD
    keyword = sys.argv[1] if len(sys.argv) > 1 else COURSE_KEYWORD
    NIM = input("[+] Masukkan NIM: ").strip()
    PASSWORD = getpass("[+] Masukkan Password: ")

    if not NIM or not PASSWORD:
        print("[!] NIM dan password wajib diisi.")
        return 1

    driver = build_driver()
    wait = WebDriverWait(driver, WAIT_TIMEOUT)

    try:
        login(driver, wait)

        course_url = find_course_url(driver, wait, keyword)
        if not course_url:
            print(f"[!] Mata kuliah '{keyword}' tidak ditemukan di dashboard.")
            return 1

        print("[+] Membuka halaman mata kuliah...")
        driver.get(course_url)
        wait.until(EC.presence_of_element_located((By.CSS_SELECTOR, "body")))
        time.sleep(2)

        forums = collect_forum_links(driver, wait)
        if not forums:
            print("[!] Tidak ada forum diskusi yang ditemukan di course index.")
            return 1

        print(f"[+] {len(forums)} forum diskusi ditemukan")

        all_results = []
        for index, (forum_name, forum_url) in enumerate(forums, start=1):
            print(f"\n[{index}/{len(forums)}] {forum_name}")
            try:
                all_results.extend(process_forum(driver, wait, forum_name, forum_url))
            except WebDriverException as error:
                print(f"[!] Forum gagal diproses: {type(error).__name__}")

        print(f"\n[+] Total post dirating: {len(all_results)}")
        print_summary(all_results)
        return 0

    except KeyboardInterrupt:
        print("\n[!] Dihentikan oleh pengguna.")
        return 130
    finally:
        if KEEP_BROWSER_OPEN:
            try:
                input("[+] Selesai. Tekan Enter untuk menutup browser...")
            except (EOFError, KeyboardInterrupt):
                pass
        driver.quit()


if __name__ == "__main__":
    sys.exit(main())
