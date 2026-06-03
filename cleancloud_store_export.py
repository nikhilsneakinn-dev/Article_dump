from __future__ import annotations

import argparse
import os
import re
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

import pandas as pd
from selenium import webdriver
from selenium.common.exceptions import TimeoutException, WebDriverException
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.remote.webdriver import WebDriver
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait


STORE_URL = "https://cleancloudapp.com/store"
LOGIN_URL_PART = "/login"
DEBUG_DIR = Path("cleancloud_debug")
CLEAN_ITEM_COLUMNS = [
    "source_tab",
    "order_id",
    "article_number",
    "article_row_id",
    "item_id",
    "item_note",
    "brand",
    "color",
    "size",
    "service",
    "service_tags",
    "photo_marker",
    "customer",
    "contact",
    "placed",
    "ready_by",
    "order_pieces",
    "order_total",
    "order_total_amount",
    "item_bill_amount",
    "bill_source",
    "order_notes",
    "article_label",
    "article_description",
    "article_note",
    "article_raw",
    "order_summary",
]


@dataclass
class ExtractedTab:
    name: str
    rows: list[dict[str, str]]


@dataclass
class DetailItem:
    order_id: str
    item_id: str
    item_note: str
    service: str = ""
    item_bill_amount: str = ""


def clean_text(value: str | None) -> str:
    if not value:
        return ""
    return re.sub(r"\s+", " ", value).strip()


def first_value(row: dict[str, str], *keys: str) -> str:
    for key in keys:
        value = clean_text(row.get(key))
        if value:
            return value
    return ""


def parse_money(value: str | None) -> str:
    text = clean_text(value)
    if not text:
        return ""
    match = re.search(r"-?\d+(?:,\d{2,3})*(?:\.\d+)?|-?\d+(?:\.\d+)?", text)
    if not match:
        return ""
    return match.group(0).replace(",", "")


def wait_for_manual_login(driver: WebDriver, timeout_seconds: int) -> None:
    print("Opening CleanCloud. Please log in in the browser window if prompted.")
    driver.get(STORE_URL)

    if LOGIN_URL_PART not in driver.current_url:
        return

    print(f"Waiting up to {timeout_seconds} seconds for login to complete...")
    try:
        WebDriverWait(driver, timeout_seconds).until(
            lambda d: LOGIN_URL_PART not in d.current_url
        )
    except TimeoutException:
        raise RuntimeError(
            "Login did not complete before the timeout. Run again with a larger "
            "--login-timeout value, for example --login-timeout 300."
        )

    driver.get(STORE_URL)
    wait_for_page_ready(driver)


def otp_prompt_visible(driver: WebDriver) -> bool:
    try:
        return bool(
            driver.execute_script(
                """
                const clean = (value) => (value || '').replace(/\\s+/g, ' ').trim();
                const visible = (el) => {
                    const style = window.getComputedStyle(el);
                    const rect = el.getBoundingClientRect();
                    return style.display !== 'none' &&
                        style.visibility !== 'hidden' &&
                        rect.width > 0 &&
                        rect.height > 0;
                };
                const text = clean(document.body ? document.body.innerText : '').toLowerCase();
                const inputs = Array.from(document.querySelectorAll('input')).filter(visible);
                const otpInput = inputs.find((input) => {
                    const type = (input.type || '').toLowerCase();
                    const name = `${input.name || ''} ${input.id || ''} ${input.placeholder || ''}`.toLowerCase();
                    return type !== 'password' && /otp|pin|code|verification|auth/.test(name);
                });
                return !!otpInput || /otp|pin|verification code|security code|authentication code/.test(text);
                """
            )
        )
    except WebDriverException:
        return False


def wait_for_otp_code(otp_file: str | None, otp_request_file: str | None, timeout_seconds: int) -> str:
    if not otp_file:
        raise RuntimeError("CleanCloud requested a PIN, but no --otp-file was provided.")
    if otp_request_file:
        Path(otp_request_file).write_text("requested", encoding="utf-8")
    print("CleanCloud requested an email PIN. Waiting for PIN input...")
    deadline = time.time() + timeout_seconds
    otp_path = Path(otp_file)
    while time.time() < deadline:
        if otp_path.exists():
            code = clean_text(otp_path.read_text(encoding="utf-8", errors="ignore"))
            if code:
                return code
        time.sleep(1)
    raise RuntimeError("Timed out waiting for CleanCloud email PIN.")


def submit_otp_code(driver: WebDriver, otp_code: str) -> None:
    submitted = bool(
        driver.execute_script(
            """
            const visible = (el) => {
                const style = window.getComputedStyle(el);
                const rect = el.getBoundingClientRect();
                return style.display !== 'none' &&
                    style.visibility !== 'hidden' &&
                    rect.width > 0 &&
                    rect.height > 0;
            };
            const code = arguments[0];
            const inputs = Array.from(document.querySelectorAll('input')).filter(visible);
            const otpInput = inputs.find((input) => {
                const type = (input.type || '').toLowerCase();
                const name = `${input.name || ''} ${input.id || ''} ${input.placeholder || ''}`.toLowerCase();
                return type !== 'password' && /otp|pin|code|verification|auth/.test(name);
            }) || inputs.find((input) => {
                const type = (input.type || '').toLowerCase();
                return type === 'text' || type === 'tel' || type === 'number';
            });
            if (!otpInput) return false;
            otpInput.focus();
            otpInput.value = code;
            otpInput.dispatchEvent(new Event('input', {bubbles: true}));
            otpInput.dispatchEvent(new Event('change', {bubbles: true}));
            const form = otpInput.closest('form');
            const button = form
                ? Array.from(form.querySelectorAll('button, input[type="submit"]')).filter(visible)[0]
                : Array.from(document.querySelectorAll('button, input[type="submit"]')).filter(visible)[0];
            if (button) {
                button.click();
                return true;
            }
            if (form) {
                form.requestSubmit ? form.requestSubmit() : form.submit();
                return true;
            }
            otpInput.dispatchEvent(new KeyboardEvent('keydown', {key: 'Enter', bubbles: true}));
            return true;
            """,
            otp_code,
        )
    )
    if not submitted:
        raise RuntimeError("Could not find CleanCloud PIN field.")


def login_with_credentials(
    driver: WebDriver,
    username: str,
    password: str,
    timeout_seconds: int,
    otp_file: str | None = None,
    otp_request_file: str | None = None,
    otp_timeout_seconds: int = 300,
) -> None:
    print("Opening CleanCloud and logging in with provided credentials.")
    driver.get(STORE_URL)
    wait_for_page_ready(driver)

    if LOGIN_URL_PART not in driver.current_url:
        return

    filled = bool(
        driver.execute_script(
            """
            const visible = (el) => {
                const style = window.getComputedStyle(el);
                const rect = el.getBoundingClientRect();
                return style.display !== 'none' &&
                    style.visibility !== 'hidden' &&
                    rect.width > 0 &&
                    rect.height > 0;
            };
            const username = arguments[0];
            const password = arguments[1];
            const inputs = Array.from(document.querySelectorAll('input')).filter(visible);
            const pass = inputs.find((input) => (input.type || '').toLowerCase() === 'password');
            const user = inputs.find((input) => {
                const type = (input.type || '').toLowerCase();
                const name = `${input.name || ''} ${input.id || ''} ${input.placeholder || ''}`.toLowerCase();
                return input !== pass && (type === 'email' || type === 'text' || /email|user|login|phone/.test(name));
            });
            if (!user || !pass) return false;
            user.focus();
            user.value = username;
            user.dispatchEvent(new Event('input', {bubbles: true}));
            user.dispatchEvent(new Event('change', {bubbles: true}));
            pass.focus();
            pass.value = password;
            pass.dispatchEvent(new Event('input', {bubbles: true}));
            pass.dispatchEvent(new Event('change', {bubbles: true}));
            const form = pass.closest('form') || user.closest('form');
            const button = form
                ? Array.from(form.querySelectorAll('button, input[type="submit"]')).filter(visible)[0]
                : Array.from(document.querySelectorAll('button, input[type="submit"]')).filter(visible)[0];
            if (button) {
                button.click();
                return true;
            }
            if (form) {
                form.requestSubmit ? form.requestSubmit() : form.submit();
                return true;
            }
            pass.dispatchEvent(new KeyboardEvent('keydown', {key: 'Enter', bubbles: true}));
            return true;
            """,
            username,
            password,
        )
    )
    if not filled:
        raise RuntimeError("Could not find CleanCloud login fields on the page.")

    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        if LOGIN_URL_PART not in driver.current_url:
            driver.get(STORE_URL)
            wait_for_page_ready(driver)
            return
        if otp_prompt_visible(driver):
            otp_code = wait_for_otp_code(otp_file, otp_request_file, otp_timeout_seconds)
            submit_otp_code(driver, otp_code)
            try:
                WebDriverWait(driver, timeout_seconds).until(
                    lambda d: LOGIN_URL_PART not in d.current_url
                )
            except TimeoutException:
                raise RuntimeError("CleanCloud login did not complete after PIN entry.")
            driver.get(STORE_URL)
            wait_for_page_ready(driver)
            return
        time.sleep(1)

    raise RuntimeError("CleanCloud login did not complete before timeout.")

    driver.get(STORE_URL)
    wait_for_page_ready(driver)


def wait_for_user_ready(driver: WebDriver) -> None:
    print("Opening CleanCloud Store.")
    driver.get(STORE_URL)
    input(
        "Log in if needed, open the Cleaning tab, wait until the data is visible, "
        "then press Enter here..."
    )


def wait_for_page_ready(driver: WebDriver, timeout_seconds: int = 30) -> None:
    WebDriverWait(driver, timeout_seconds).until(
        lambda d: d.execute_script("return document.readyState") in {"interactive", "complete"}
    )


def xpath_literal(value: str) -> str:
    if "'" not in value:
        return f"'{value}'"
    if '"' not in value:
        return f'"{value}"'
    return "concat(" + ', "\'", '.join(f"'{part}'" for part in value.split("'")) + ")"


def click_tab(driver: WebDriver, tab_name: str) -> None:
    tab_name_lower = tab_name.lower()
    tab_literal = xpath_literal(tab_name)
    tab_lower_literal = xpath_literal(tab_name_lower)
    before_signature = visible_content_signature(driver)
    candidates = [
        (
            "//*[self::a or self::button or @role='tab']"
            f"[contains(translate(normalize-space(.), 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), {tab_lower_literal})]"
        ),
        (
            "//*[self::a or self::button or @role='tab' or self::li]"
            "[contains(@class, 'tab') or contains(@class, 'nav') or contains(@class, 'menu') or contains(@class, 'active')]"
            f"[contains(translate(normalize-space(.), 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), {tab_lower_literal})]"
        ),
        f"//*[contains(@href, {tab_literal}) or contains(@data-tab, {tab_literal})]",
    ]

    last_error: Exception | None = None
    contexts = [None] + driver.find_elements(By.CSS_SELECTOR, "iframe, frame")
    for frame in contexts:
        driver.switch_to.default_content()
        if frame is not None:
            driver.switch_to.frame(frame)

        for xpath in candidates:
            try:
                element = first_visible_clickable_match(driver, xpath, timeout_seconds=5)
                driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", element)
                time.sleep(0.2)
                try:
                    element.click()
                except WebDriverException:
                    driver.execute_script("arguments[0].click();", element)
                wait_for_tab_to_settle(driver, before_signature)
                wait_for_page_ready(driver, timeout_seconds=10)
                return
            except Exception as exc:
                last_error = exc

    save_debug_artifacts(driver, f"tab_not_found_{tab_name}")
    raise RuntimeError(
        f"Could not find or click the '{tab_name}' tab. I saved a screenshot and HTML "
        f"under {DEBUG_DIR.resolve()} so you can see what Selenium saw."
    ) from last_error


def first_visible_clickable_match(driver: WebDriver, xpath: str, timeout_seconds: int):
    def find_match(drv: WebDriver):
        matches = drv.find_elements(By.XPATH, xpath)
        visible = [element for element in matches if element.is_displayed() and clean_text(element.text)]
        if not visible:
            return False
        return sorted(visible, key=lambda element: len(clean_text(element.text)))[0]

    return WebDriverWait(driver, timeout_seconds).until(find_match)


def visible_content_signature(driver: WebDriver) -> str:
    try:
        return driver.execute_script(
            """
            const clean = (value) => (value || '').replace(/\\s+/g, ' ').trim();
            const visible = (el) => {
                const style = window.getComputedStyle(el);
                const rect = el.getBoundingClientRect();
                return style.display !== 'none' &&
                    style.visibility !== 'hidden' &&
                    style.opacity !== '0' &&
                    rect.width > 0 &&
                    rect.height > 0 &&
                    clean(el.innerText);
            };
            return Array.from(document.querySelectorAll('table, [class*=order], [class*=ticket], [class*=card], [class*=item], li'))
                .filter(visible)
                .map((el) => clean(el.innerText))
                .join('\\n---\\n')
                .slice(0, 20000);
            """
        ) or ""
    except WebDriverException:
        return ""


def wait_for_tab_to_settle(driver: WebDriver, before_signature: str, timeout_seconds: int = 12) -> None:
    end_time = time.time() + timeout_seconds
    last_signature = before_signature
    while time.time() < end_time:
        time.sleep(0.5)
        current_signature = visible_content_signature(driver)
        if current_signature and current_signature != before_signature:
            time.sleep(1)
            return
        last_signature = current_signature
    if last_signature == before_signature:
        print("Warning: visible data did not change after clicking tab. Check if the correct tab is selected.")


def save_debug_artifacts(driver: WebDriver, label: str) -> None:
    safe_label = re.sub(r"[^A-Za-z0-9_-]+", "_", label).strip("_")
    DEBUG_DIR.mkdir(exist_ok=True)
    timestamp = time.strftime("%Y%m%d_%H%M%S")
    screenshot_path = DEBUG_DIR / f"{timestamp}_{safe_label}.png"
    html_path = DEBUG_DIR / f"{timestamp}_{safe_label}.html"

    try:
        driver.save_screenshot(str(screenshot_path))
    except WebDriverException:
        pass

    try:
        html_path.write_text(driver.page_source, encoding="utf-8")
    except WebDriverException:
        pass


def visible_elements(driver: WebDriver, css_selector: str):
    return [
        element
        for element in driver.find_elements(By.CSS_SELECTOR, css_selector)
        if element.is_displayed() and clean_text(element.text)
    ]


def extract_article_details_from_row(row) -> tuple[str, str]:
    values: list[str] = []
    for element in row.find_elements(By.CSS_SELECTOR, ".hide, .nHide, [id^='nHide']"):
        text = clean_text(element.get_attribute("textContent") or element.text)
        if not text:
            continue
        if not re.search(r"[A-Z]\s*[-/]|\{P\}|\([^)]+\)", text):
            continue
        if text not in values:
            values.append(text)

    cleaned = [clean_text(value.replace("/", " ")) for value in values]
    return "\n".join(values), "\n".join(value for value in cleaned if value)


def extract_visible_tables(driver: WebDriver) -> list[dict[str, str]]:
    js_rows = driver.execute_script(
        """
        const clean = (value) => (value || '').replace(/\\s+/g, ' ').trim();
        const visible = (el) => {
            const style = window.getComputedStyle(el);
            const rect = el.getBoundingClientRect();
            return style.display !== 'none' &&
                style.visibility !== 'hidden' &&
                style.opacity !== '0' &&
                rect.width > 0 &&
                rect.height > 0 &&
                clean(el.innerText);
        };
        const inViewport = (el) => {
            const rect = el.getBoundingClientRect();
            return rect.bottom > 140 &&
                rect.top < window.innerHeight &&
                rect.right > 0 &&
                rect.left < window.innerWidth;
        };
        const scoreTable = (table) => {
            const rect = table.getBoundingClientRect();
            const text = clean(table.innerText).toLowerCase();
            let score = 0;
            if (inViewport(table)) score += 10000;
            if (text.includes('customer') && text.includes('order') && text.includes('pcs')) score += 1000;
            if (table.querySelector("tbody tr[id^='checkin_'], tbody tr[id^='pickup_'], tbody tr[id*='pickup'], tbody tr[id*='checkin']")) score += 500;
            score -= Math.max(0, rect.top);
            return score;
        };

        const output = [];
        const tables = Array.from(document.querySelectorAll('table'))
            .filter((table) => visible(table) && inViewport(table))
            .sort((a, b) => scoreTable(b) - scoreTable(a));
        const table = tables[0];
        if (!table) return output;

        let headers = Array.from(table.querySelectorAll('thead th, thead td')).map((cell) => clean(cell.innerText));
        if (!headers.length) {
            const firstRow = table.querySelector('tr');
            if (firstRow) headers = Array.from(firstRow.querySelectorAll('th, td')).map((cell) => clean(cell.innerText));
        }
        const hiddenArticleDetails = (row) => {
            const values = [];
            const orderCells = Array.from(row.querySelectorAll('td.trP, td.colO, [onclick*="trackOrderProgress"]'));
            const roots = orderCells.length ? orderCells : [row];
            for (const root of roots) {
                for (const el of Array.from(root.querySelectorAll('.hide, .nHide, [id^="nHide"]'))) {
                    const raw = clean(el.textContent || el.innerText);
                    if (!raw) continue;
                    if (!/(?:\\{P\\}\\s*\\/?)?\\s*[A-Z]\\s*[-/]/.test(raw)) continue;
                    values.push(raw);
                }
            }
            if (values.length) return Array.from(new Set(values));
            for (const el of Array.from(row.querySelectorAll('.hide, .nHide, [id^="nHide"]'))) {
                const raw = clean(el.textContent || el.innerText);
                if (!raw) continue;
                if (!/[A-Z]\\s*[-/]|\\{P\\}|\\([^)]+\\)/.test(raw)) continue;
                values.push(raw);
            }
            return Array.from(new Set(values));
        };
        const itemIds = (row) => clean(row.getAttribute('data-hs') || '')
            .split(',')
            .map((value) => clean(value))
            .filter(Boolean);
        const cleanArticleDetails = (values) => values
            .map((value) => clean(value.replace(/\\s*\\/\\s*/g, ' ').replace(/\\s+/g, ' ')))
            .filter(Boolean);

        let rows = Array.from(table.querySelectorAll('tbody tr'));
        if (!rows.length) rows = Array.from(table.querySelectorAll('tr'));

        for (const row of rows) {
            const cells = Array.from(row.querySelectorAll('th, td')).map((cell) => clean(cell.innerText));
            if (!cells.some(Boolean)) continue;
            if (headers.length && cells.join('|') === headers.join('|')) continue;
            const articleDetails = hiddenArticleDetails(row);
            const obj = {};
            if (headers.length && headers.length === cells.length) {
                headers.forEach((header, index) => obj[header || `Column ${index + 1}`] = cells[index]);
            } else {
                cells.forEach((value, index) => obj[`Column ${index + 1}`] = value);
            }
            obj['Article Details'] = articleDetails.join('\\n');
            obj['Clean Article Details'] = cleanArticleDetails(articleDetails).join('\\n');
            obj['Item IDs'] = itemIds(row).join('\\n');
            output.push(obj);
        }
        return output;
        """
    )
    if js_rows:
        return dedupe_rows(js_rows)

    rows: list[dict[str, str]] = []
    for table in visible_elements(driver, "table"):
        headers = [
            clean_text(cell.text)
            for cell in table.find_elements(By.CSS_SELECTOR, "thead th, thead td")
        ]

        body_rows = table.find_elements(By.CSS_SELECTOR, "tbody tr")
        if not body_rows:
            body_rows = table.find_elements(By.CSS_SELECTOR, "tr")

        for row in body_rows:
            cells = [clean_text(cell.text) for cell in row.find_elements(By.CSS_SELECTOR, "th, td")]
            if not any(cells):
                continue
            article_details, clean_article_details = extract_article_details_from_row(row)
            if headers and len(headers) == len(cells):
                record = dict(zip(headers, cells, strict=False))
            else:
                record = {f"Column {index + 1}": value for index, value in enumerate(cells)}
            record["Article Details"] = article_details
            record["Clean Article Details"] = clean_article_details
            rows.append(record)

    return dedupe_rows(rows)


def extract_visible_cards(driver: WebDriver) -> list[dict[str, str]]:
    js_rows = driver.execute_script(
        """
        const clean = (value) => (value || '').replace(/\\s+/g, ' ').trim();
        const visible = (el) => {
            const style = window.getComputedStyle(el);
            const rect = el.getBoundingClientRect();
            return style.display !== 'none' &&
                style.visibility !== 'hidden' &&
                style.opacity !== '0' &&
                rect.width > 0 &&
                rect.height > 0 &&
                clean(el.innerText);
        };
        const selectors = [
            "[class*='order']",
            "[class*='ticket']",
            "[class*='card']",
            "[class*='item']",
            "li"
        ];
        const seen = new Set();
        const output = [];
        for (const selector of selectors) {
            for (const el of Array.from(document.querySelectorAll(selector)).filter(visible)) {
                const text = clean(el.innerText);
                if (text.length < 3 || seen.has(text)) continue;
                seen.add(text);
                output.push({Text: text});
            }
        }
        return output;
        """
    )
    if js_rows:
        parsed_rows = []
        for row in js_rows:
            parsed = parse_label_value_lines(row.get("Text", ""))
            parsed_rows.append(parsed if parsed else row)
        return dedupe_rows(parsed_rows)

    selectors = [
        "[class*='order']",
        "[class*='ticket']",
        "[class*='card']",
        "[class*='item']",
        "li",
    ]

    rows: list[dict[str, str]] = []
    seen_text: set[str] = set()

    for selector in selectors:
        for element in visible_elements(driver, selector):
            text = clean_text(element.text)
            if len(text) < 3 or text in seen_text:
                continue
            seen_text.add(text)
            parsed = parse_label_value_lines(element.text)
            if parsed:
                rows.append(parsed)
            else:
                rows.append({"Text": text})

    return dedupe_rows(rows)


def parse_label_value_lines(text: str) -> dict[str, str]:
    parsed: dict[str, str] = {}
    for line in text.splitlines():
        line = clean_text(line)
        if not line:
            continue
        if ":" in line:
            label, value = line.split(":", 1)
            label = clean_text(label)
            value = clean_text(value)
            if label and value:
                parsed[label] = value

    return parsed


def dedupe_rows(rows: Iterable[dict[str, str]]) -> list[dict[str, str]]:
    deduped: list[dict[str, str]] = []
    seen: set[tuple[tuple[str, str], ...]] = set()

    for row in rows:
        normalized = tuple(sorted((key, value) for key, value in row.items() if value))
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        deduped.append(row)

    return deduped


NON_ARTICLE_RE = re.compile(
    r"(^|\b)(delivery|pickup|postal|courier|shipping|in-?store|rounding|discount|credit|tax|tip)\b",
    re.IGNORECASE,
)
QUANTITY_RE = re.compile(r"^(?P<name>.+?)\s+x\s*(?P<qty>\d+(?:\.\d+)?)\b", re.IGNORECASE)
SERVICE_QUANTITY_RE = re.compile(r"(?P<name>.+?)\s+x\s*(?P<qty>\d+(?:\.\d+)?)\b", re.IGNORECASE)
ARTICLE_DETAIL_RE = re.compile(r"^(?P<photo>\{P\})?\s*/?\s*(?P<label>[A-Z])\s*[-.\s]\s*(?P<detail>.+)$")
PHOTO_DETAIL_RE = re.compile(r"^(?P<photo>\{P\})\s*/?\s*(?P<detail>.+)$")
SERVICE_PREFIX_RE = re.compile(
    r"(?=[A-Za-z][A-Za-z0-9 .&/()_-]*:\s*(?:\{P\})?\s*[A-Z]\s*[-.\s])"
)
LEADING_SERVICE_RE = re.compile(r"^[A-Za-z][A-Za-z0-9 .&/()_-]*:\s*")
SERVICE_DETAIL_RE = re.compile(
    r"(?P<service>[A-Za-z][^:\n]+?)\s*:\s*(?P<photo>\{P\})?\s*(?P<label>[A-Z])\s*[-.\s]\s*(?P<detail>.*?)(?=(?:[A-Za-z][^:\n]+?\s*:\s*(?:\{P\})?\s*[A-Z]\s*[-.\s])|$)"
)
COLOR_WORDS = [
    "Black",
    "White",
    "Grey",
    "Gray",
    "Brown",
    "Blue",
    "Navy",
    "Green",
    "Red",
    "Pink",
    "Yellow",
    "Orange",
    "Purple",
    "Tan",
    "Beige",
    "Cream",
    "Wine",
    "Maroon",
    "Burgundy",
    "Gold",
    "Silver",
    "OffWhite",
    "Off White",
    "LightGrey",
    "Light Grey",
    "DarkGrey",
    "Dark Grey",
]


def split_cell_lines(value: str | None) -> list[str]:
    if value is None:
        return []
    text = str(value).replace("\r", "")
    if not text:
        return []
    return [clean_text(part) for part in re.split(r"\n+|<br\s*/?>", text, flags=re.IGNORECASE) if clean_text(part)]


def order_service_sequence(order_text: str, pieces: str | None = None) -> list[str]:
    services: list[str] = []
    text = clean_text(order_text)
    text = re.sub(r"\bDetails\b|\bOrder History\b", "", text, flags=re.IGNORECASE).strip()
    if not text:
        return services

    matches = list(SERVICE_QUANTITY_RE.finditer(text))
    for index, match in enumerate(matches):
        service = clean_text(match.group("name"))
        if index > 0:
            previous_tail = text[matches[index - 1].end():match.start()]
            previous_tail = re.sub(r"\([^)]*\)", "", previous_tail).strip()
            if previous_tail and service.lower().startswith(previous_tail.lower()):
                service = clean_text(service[len(previous_tail):])
        if not service or NON_ARTICLE_RE.search(service):
            continue
        qty = max(1, int(float(match.group("qty"))))
        services.extend([service] * qty)

    if services:
        return services

    fallback_qty = 1
    try:
        fallback_qty = max(1, int(float(str(pieces or "1"))))
    except ValueError:
        pass
    return [text] * fallback_qty


def expand_article_lines(lines: list[str], services: list[str]) -> list[str]:
    expanded: list[str] = []
    service_names: set[str] = set()
    for service in services:
        cleaned = clean_text(service)
        if not cleaned:
            continue
        service_names.add(cleaned)
        without_tags = clean_text(re.sub(r"\([^)]*\)", "", cleaned))
        without_tags = clean_text(re.sub(r"^(?:\([^)]*\)\s*)+", "", without_tags))
        if without_tags:
            service_names.add(without_tags)
    sorted_services = sorted(service_names, key=len, reverse=True)

    for line in lines:
        parts = [line]
        for service in sorted_services:
            pattern = re.compile(rf"{re.escape(service)}\s*:\s*", re.IGNORECASE)
            next_parts: list[str] = []
            for part in parts:
                next_parts.extend(pattern.sub("\n", part).splitlines())
            parts = next_parts

        for part in parts:
            part = clean_text(part)
            if part:
                expanded.append(part)

    return expanded


def parse_article_candidate(line: str) -> dict[str, str] | None:
    line = clean_text(line)
    if not line:
        return None
    match = ARTICLE_DETAIL_RE.match(line)
    label = ""
    if not match:
        match = PHOTO_DETAIL_RE.match(line)
        if not match:
            return None
    else:
        label = match.group("label")

    raw_detail = clean_text(match.group("detail"))
    article_note = ""
    if "/" in raw_detail:
        raw_detail, article_note = raw_detail.split("/", 1)
        raw_detail = clean_text(raw_detail)
        article_note = clean_text(article_note.strip("()"))

    detail_without_tags = re.sub(r"\([^)]*\)", "", raw_detail).strip()
    service_tags = " ".join(re.findall(r"\(([^)]+)\)", raw_detail))
    brand, color, size = parse_brand_color_size(detail_without_tags)
    return {
        "photo_marker": "Yes" if match.group("photo") else "",
        "article_label": label,
        "article_raw": line,
        "article_description": detail_without_tags,
        "brand": brand,
        "color": color,
        "size": size,
        "article_note": article_note,
        "service_tags_from_detail": clean_text(service_tags),
    }


def service_map_from_article_lines(lines: list[str]) -> dict[str, dict[str, str]]:
    mapped: dict[str, dict[str, str]] = {}
    joined = "\n".join(lines)
    for match in SERVICE_DETAIL_RE.finditer(joined):
        label = match.group("label")
        service = clean_text(match.group("service"))
        detail = clean_text(match.group("detail"))
        tags = " ".join(re.findall(r"\(([^)]+)\)", detail))
        if label and service:
            mapped[label] = {
                "service": service,
                "service_tags": clean_text(tags),
            }
    return mapped


def parse_brand_color_size(description: str) -> tuple[str, str, str]:
    description = clean_text(description)
    if not description:
        return "", "", ""

    size = ""
    base = description
    size_match = re.match(r"^(?P<base>.+)-(?P<size>[A-Za-z0-9.]+)$", description)
    if size_match:
        base = clean_text(size_match.group("base"))
        size = clean_text(size_match.group("size"))

    color = ""
    brand = base
    compact_base = base.replace(" ", "")
    for color_word in sorted(COLOR_WORDS, key=len, reverse=True):
        compact_color = color_word.replace(" ", "")
        index = compact_base.lower().find(compact_color.lower())
        if index > 0:
            brand = compact_base[:index]
            color = compact_base[index:]
            break

    return clean_text(brand), clean_text(color), size


def item_note_from_article(article: dict[str, str]) -> str:
    marker = "{P} " if article.get("photo_marker") else ""
    label = article.get("article_label", "")
    description = article.get("article_description", "")
    if label and description:
        note = f"{marker}{label}-{description}"
    else:
        note = f"{marker}{description or article.get('article_raw', '')}"

    if article.get("article_note"):
        note = f"{note} ({article['article_note']})"
    return clean_text(note)


def parse_article_from_item_note(item_note: str) -> dict[str, str]:
    parsed = parse_article_candidate(item_note)
    if parsed:
        return parsed
    brand, color, size = parse_brand_color_size(item_note)
    return {
        "photo_marker": "",
        "article_label": "",
        "article_raw": item_note,
        "article_description": item_note,
        "brand": brand,
        "color": color,
        "size": size,
        "article_note": "",
        "service_tags_from_detail": "",
    }


def detail_items_from_text(order_id: str, detail_text: str, item_ids: list[str] | None = None) -> list[DetailItem]:
    item_ids = item_ids or []
    details: list[DetailItem] = []
    for line in split_cell_lines(detail_text):
        service = ""
        note = line
        if ":" in line:
            service, note = line.split(":", 1)
            service = clean_text(service)
            note = clean_text(note)
        if not note or is_non_article_service(service):
            continue
        parsed = parse_article_candidate(note)
        if not parsed:
            continue
        item_id = item_ids[len(details)] if len(details) < len(item_ids) else ""
        details.append(
            DetailItem(
                order_id=str(order_id),
                item_id=item_id,
                item_note=item_note_from_article(parsed),
                service=service,
            )
        )
    return details


def label_for_index(index: int) -> str:
    if index < 26:
        return chr(ord("A") + index)
    return f"X{index + 1}"


def detail_note_for_parser(item_note: str, index: int) -> str:
    note = clean_text(item_note)
    if not note:
        return ""
    if re.match(r"^(?:\{P\}\s*)?[A-Z]\s*[-.\s]", note):
        return note
    return f"{label_for_index(index)}-{note}"


def is_non_article_service(service: str) -> bool:
    return bool(
        re.search(
            r"\b(delivery|pickup|in-?store|postal|courier|shipping)\b",
            clean_text(service),
            flags=re.IGNORECASE,
        )
    )


def article_rows_for_order(row: dict[str, str]) -> list[dict[str, str]]:
    order_id = clean_text(row.get("ID") or row.get("Order id") or row.get("order_id"))
    item_ids = split_cell_lines(row.get("Item IDs"))
    services = order_service_sequence(row.get("ORDER", ""), row.get("PCS"))
    article_lines = split_cell_lines(row.get("Clean Article Details") or row.get("Article Details"))
    article_lines = expand_article_lines(article_lines, services)
    articles = []
    seen_articles: set[str] = set()
    for line in article_lines:
        parsed = parse_article_candidate(line)
        if not parsed:
            continue
        key = f"{parsed['article_label']}|{parsed['article_description']}|{parsed['article_note']}"
        if key in seen_articles:
            continue
        seen_articles.add(key)
        articles.append(parsed)

    if not articles and services:
        articles = [
            {
                "photo_marker": "",
                "article_label": "",
                "article_raw": "",
                "article_description": "",
                "brand": "",
                "color": "",
                "size": "",
                "article_note": "",
                "service_tags_from_detail": "",
            }
            for _ in services
        ]

    article_rows = []
    for index, article in enumerate(articles, start=1):
        service = services[index - 1] if index - 1 < len(services) else (services[-1] if services else "")
        service_tags = clean_text(" ".join(re.findall(r"\(([^)]+)\)", service))) or article["service_tags_from_detail"]
        service_clean = clean_text(re.sub(r"\([^)]*\)", "", service).strip(" -"))
        order_total = first_value(row, "TOTAL", "total", "order_total")
        article_label = clean_text(article["article_label"]) or label_for_index(index - 1)
        article_rows.append(
            {
                "source_tab": first_value(row, "Source Tab", "source_tab"),
                "order_id": order_id,
                "article_number": index,
                "article_row_id": f"{order_id}-{article_label}" if order_id else article_label,
                "item_id": item_ids[index - 1] if index - 1 < len(item_ids) else "",
                "item_note": item_note_from_article(article),
                "brand": article["brand"],
                "color": article["color"],
                "size": article["size"],
                "service": service_clean or service,
                "service_tags": service_tags or article["service_tags_from_detail"],
                "photo_marker": article["photo_marker"],
                "customer": first_value(row, "CUSTOMER", "customer"),
                "contact": first_value(row, "CONTACT", "phone", "contact"),
                "placed": first_value(row, "PLACED", "createdDate", "placed"),
                "ready_by": first_value(row, "READY BY", "ready_by"),
                "order_pieces": first_value(row, "PCS", "pieces", "order_pieces"),
                "order_total": order_total,
                "order_total_amount": parse_money(order_total),
                "item_bill_amount": "",
                "bill_source": "not_visible_in_store_list",
                "order_notes": first_value(row, "NOTES", "notes"),
                "article_label": article_label,
                "article_description": article["article_description"],
                "article_note": article["article_note"],
                "article_raw": article["article_raw"],
                "order_summary": first_value(row, "ORDER", "summary", "order_summary"),
            }
        )
    return article_rows


def article_rows_for_tabs(tabs: list[ExtractedTab]) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    for tab in tabs:
        for order_row in tab.rows:
            rows.extend(article_rows_for_order(order_row))
    return rows


def extract_tab(driver: WebDriver, tab_name: str) -> ExtractedTab:
    click_tab(driver, tab_name)
    table_rows = extract_visible_tables(driver)
    rows = table_rows if table_rows else extract_visible_cards(driver)
    rows = tag_rows(rows, tab_name)
    save_debug_artifacts(driver, f"extracted_{tab_name}")
    return ExtractedTab(name=tab_name, rows=rows)


def extract_tab_manually(driver: WebDriver, tab_name: str) -> ExtractedTab:
    input(f"Click the '{tab_name}' tab in Chrome, wait for it to load, then press Enter here...")
    table_rows = extract_visible_tables(driver)
    rows = table_rows if table_rows else extract_visible_cards(driver)
    rows = tag_rows(rows, tab_name)
    save_debug_artifacts(driver, f"extracted_{tab_name}")
    return ExtractedTab(name=tab_name, rows=rows)


def tag_rows(rows: list[dict[str, str]], tab_name: str) -> list[dict[str, str]]:
    tagged_rows = []
    for row in rows:
        tagged = {"Source Tab": tab_name}
        tagged.update(row)
        tagged_rows.append(tagged)
    return tagged_rows


def visible_modal_signature(driver: WebDriver) -> str:
    try:
        return driver.execute_script(
            """
            const clean = (value) => (value || '').replace(/\\s+/g, ' ').trim();
            const visible = (el) => {
                const style = window.getComputedStyle(el);
                const rect = el.getBoundingClientRect();
                return style.display !== 'none' &&
                    style.visibility !== 'hidden' &&
                    style.opacity !== '0' &&
                    rect.width > 0 &&
                    rect.height > 0 &&
                    clean(el.innerText);
            };
            return Array.from(document.querySelectorAll('#clean_track_box, #cleanTrackBoxMain, .bigbox.hBox, .modal, [role="dialog"], .ui-dialog, .bootbox, .swal2-container'))
                .filter(visible)
                .map((el) => clean(el.innerText))
                .join('\\n---\\n');
            """
        ) or ""
    except WebDriverException:
        return ""


def open_order_detail(driver: WebDriver, order_id: str, tab_name: str = "") -> bool:
    if tab_name.lower() == "ready":
        return open_ready_order_detail(driver, order_id)
    return open_cleaning_order_detail(driver, order_id)


def open_cleaning_order_detail(driver: WebDriver, order_id: str) -> bool:
    script = """
        const orderId = arguments[0];
        const row = document.querySelector(`#checkin_${orderId}, #pickup_${orderId}, tr[id$='_${orderId}']`);
        if (!row) return false;
        row.scrollIntoView({block: 'center'});
        const details = row.querySelector(`[onclick*="sN(${orderId})"]`);
        if (details) {
            details.click();
            return true;
        }
        if (typeof sN === 'function') {
            sN(orderId);
            return true;
        }
        return false;
    """
    try:
        opened = bool(driver.execute_script(script, str(order_id)))
        if not opened:
            return False
        WebDriverWait(driver, 12).until(
            lambda d: d.execute_script(
                "const el=document.querySelector('#nHide'+arguments[0]); return !!(el && (el.innerText||el.textContent||'').trim());",
                str(order_id),
            )
            or visible_modal_signature(d)
        )
        time.sleep(1)
        return True
    except WebDriverException:
        return False
    except TimeoutException:
        return False


def wait_for_track_box_order(driver: WebDriver, order_id: str, timeout: int = 12) -> bool:
    try:
        WebDriverWait(driver, timeout).until(
            lambda d: bool(
                d.execute_script(
                    """
                    const clean = (value) => (value || '').replace(/\\s+/g, ' ').trim();
                    const visible = (el) => {
                        if (!el) return false;
                        const style = window.getComputedStyle(el);
                        const rect = el.getBoundingClientRect();
                        return style.display !== 'none' &&
                            style.visibility !== 'hidden' &&
                            style.opacity !== '0' &&
                            rect.width > 0 &&
                            rect.height > 0;
                    };
                    const box = document.querySelector('#clean_track_box');
                    const progress = document.querySelector('#progressOrderID');
                    const progressId = clean(progress ? progress.innerText : '').replace(/^#/, '');
                    return visible(box) && progressId === arguments[0];
                    """,
                    str(order_id),
                )
                or re.search(
                    rf"Order\\s*#\\s*{re.escape(str(order_id))}",
                    visible_modal_signature(d),
                    flags=re.IGNORECASE,
                )
            )
        )
        return True
    except TimeoutException:
        return False
    except WebDriverException:
        return False


def open_ready_order_detail(driver: WebDriver, order_id: str) -> bool:
    direct_script = """
        const orderId = arguments[0];
        if (typeof trackOrderProgress === 'function') {
            trackOrderProgress(Number(orderId));
            return {opened: true, step: 'direct_track_order_progress'};
        }
        const row = document.querySelector(`#checkin_${orderId}, #pickup_${orderId}, tr[id$='_${orderId}']`);
        if (!row) return {opened: false, step: 'row_not_found'};
        row.scrollIntoView({block: 'center'});
        const orderCell = row.querySelector('.oS .rowlink')?.closest('.oS') || row.querySelector('.oS');
        if (orderCell) {
            orderCell.click();
            return {opened: true, step: 'direct_order_cell_clicked'};
        }
        return {opened: false, step: 'direct_order_cell_not_found'};
    """
    script = """
        const clean = (value) => (value || '').replace(/\\s+/g, ' ').trim();
        const orderId = arguments[0];
        const row = document.querySelector(`#checkin_${orderId}, #pickup_${orderId}, tr[id$='_${orderId}']`);
        if (!row) return {opened: false, step: 'row_not_found'};
        row.scrollIntoView({block: 'center'});

        const customerCell = row.querySelector('.colN, td:nth-child(4)');
        if (customerCell) {
            customerCell.click();
            return {opened: true, step: 'customer_cell_clicked'};
        }
        return {opened: false, step: 'customer_cell_not_found'};
    """
    try:
        close_order_detail(driver)
        time.sleep(0.5)
        direct_result = driver.execute_script(direct_script, str(order_id)) or {}
        if direct_result.get("opened") and wait_for_track_box_order(driver, order_id):
            time.sleep(1)
            return True

        result = driver.execute_script(script, str(order_id)) or {}
        if not result.get("opened"):
            return False
        WebDriverWait(driver, 12).until(
            lambda d: d.execute_script(
                """
                const text = (document.querySelector('.central-crm, #central-crm, .crm-active-orders, body')?.innerText || '');
                return text.includes(arguments[0]) || /Active Orders/i.test(text);
                """,
                str(order_id),
            )
        )
        time.sleep(1)
        driver.execute_script(
            """
            const clean = (value) => (value || '').replace(/\\s+/g, ' ').trim();
            const orderId = arguments[0];
            const clickableAncestor = (node) => {
                let current = node;
                while (current && current !== document.body) {
                    const onclick = current.getAttribute && (current.getAttribute('onclick') || '');
                    if (current.matches && (current.matches('a, button, [onclick], td.trP') || /trackOrderProgress/i.test(onclick))) {
                        return current;
                    }
                    current = current.parentElement;
                }
                return null;
            };
            const roots = Array.from(document.querySelectorAll('.crm-active-orders, #crmActiveOrders, .central-crm, body'));
            for (const root of roots) {
                const matches = Array.from(root.querySelectorAll('tr, li, .crm-order, .order-row, td.trP, div'))
                    .filter((el) => clean(el.innerText).includes(orderId));
                for (const el of matches) {
                    const historyNode = Array.from(el.querySelectorAll('.rowlink, a, button, [onclick], div, span'))
                        .find((link) => /order history|history/i.test(clean(link.innerText)));
                    const clickable = (historyNode && clickableAncestor(historyNode)) ||
                        (el.matches && el.matches('td.trP, [onclick]') ? el : null) ||
                        el.querySelector('[onclick*="trackOrderProgress"]');
                    if (clickable) {
                        clickable.scrollIntoView({block: 'center'});
                        clickable.click();
                        return true;
                    }
                }
            }
            if (typeof trackOrderProgress === 'function') {
                trackOrderProgress(Number(orderId));
                return true;
            }
            return false;
            """,
            str(order_id),
        )
        if not wait_for_track_box_order(driver, order_id):
            return False
        time.sleep(1)
        return True
    except WebDriverException:
        return False
    except TimeoutException:
        return False


def close_order_detail(driver: WebDriver) -> None:
    try:
        for _ in range(3):
            closed = bool(
                driver.execute_script(
                    """
                    const trackBox = document.querySelector('#clean_track_box');
                    if (trackBox) {
                        const style = window.getComputedStyle(trackBox);
                        if (style.display !== 'none' && style.visibility !== 'hidden') {
                            if (typeof hideTrackBox === 'function') {
                                hideTrackBox();
                                return true;
                            }
                            const closeIcon = trackBox.querySelector('.hBoxCI');
                            if (closeIcon) {
                                closeIcon.click();
                                return true;
                            }
                        }
                    }
                    for (const selector of ['#clean_track_box .hBoxCI', '.modal.show .close', '.modal.in .close', '.modal .btn-close', '.ui-dialog-titlebar-close', '[data-dismiss="modal"]', '.central-crm .close']) {
                        const elements = Array.from(document.querySelectorAll(selector)).filter((el) => {
                            const style = window.getComputedStyle(el);
                            const rect = el.getBoundingClientRect();
                            const text = (el.innerText || el.textContent || '').replace(/\\s+/g, ' ').trim();
                            return style.display !== 'none' &&
                                style.visibility !== 'hidden' &&
                                rect.width > 0 &&
                                rect.height > 0 &&
                                !/^(update|payment|batch ready|send receipt|report issue)$/i.test(text);
                        });
                        const el = elements[elements.length - 1];
                        if (el) {
                            el.click();
                            return true;
                        }
                    }
                    document.dispatchEvent(new KeyboardEvent('keydown', {key: 'Escape', bubbles: true}));
                    return false;
                    """
                )
            )
            time.sleep(0.4)
            if not closed:
                break
    except WebDriverException:
        pass


def scrape_detail_items(driver: WebDriver, order_id: str) -> list[DetailItem]:
    result = driver.execute_script(
        """
        const clean = (value) => (value || '').replace(/\\s+/g, ' ').trim();
        const lines = (value) => clean(value).split(/\\n+/).map(clean).filter(Boolean);
        const visible = (el) => {
            const style = window.getComputedStyle(el);
            const rect = el.getBoundingClientRect();
            return style.display !== 'none' &&
                style.visibility !== 'hidden' &&
                style.opacity !== '0' &&
                rect.width > 0 &&
                rect.height > 0;
        };
        const orderId = arguments[0];
        const candidates = Array.from(document.querySelectorAll('#clean_track_box, #cleanTrackBoxMain, .bigbox.hBox, .modal, [role="dialog"], .ui-dialog, .swal2-container, body'))
            .filter(visible)
            .filter((el) => el === document.body || clean(el.innerText).includes(`Order #${orderId}`) || /Item\\s+ID\\s+Notes/i.test(el.innerText || ''))
            .sort((a, b) => {
                const at = clean(a.innerText);
                const bt = clean(b.innerText);
                const as = (at.includes(`Order #${orderId}`) ? 100 : 0) + (/Item\\s+ID\\s+Notes/i.test(at) ? 50 : 0) - (a === document.body ? 1000 : 0);
                const bs = (bt.includes(`Order #${orderId}`) ? 100 : 0) + (/Item\\s+ID\\s+Notes/i.test(bt) ? 50 : 0) - (b === document.body ? 1000 : 0);
                return bs - as;
            });
        const output = [];
        const textBlocks = [];
        const itemIds = [];

        const listRow = document.querySelector(`#checkin_${orderId}, #pickup_${orderId}, tr[id$='_${orderId}']`);
        if (listRow) {
            clean(listRow.getAttribute('data-hs') || '')
                .split(',')
                .map(clean)
                .filter(Boolean)
                .forEach((id) => itemIds.push(id));
            const nHide = listRow.querySelector(`#nHide${orderId}, .nHide`);
            if (nHide) textBlocks.push(nHide.innerText || nHide.textContent || '');
            for (const hidden of Array.from(listRow.querySelectorAll('.hide, [id^="nHide"]'))) {
                textBlocks.push(hidden.innerText || hidden.textContent || '');
            }
        }

        for (const root of candidates) {
            const tables = Array.from(root.querySelectorAll('table')).filter(visible);
            for (const table of tables) {
                const headerCells = Array.from(table.querySelectorAll('thead th, thead td')).map((cell) => clean(cell.innerText).toLowerCase());
                let headers = headerCells;
                const bodyRows = Array.from(table.querySelectorAll('tbody tr'));
                const allRows = bodyRows.length ? bodyRows : Array.from(table.querySelectorAll('tr')).slice(headers.length ? 0 : 1);
                if (!headers.length) {
                    const first = table.querySelector('tr');
                    headers = first ? Array.from(first.querySelectorAll('th, td')).map((cell) => clean(cell.innerText).toLowerCase()) : [];
                }
                const hasItemShape = headers.some((h) => h === 'item' || h.includes('item')) &&
                    headers.some((h) => h === 'id' || h.includes('barcode')) &&
                    headers.some((h) => h.includes('note'));
                if (!hasItemShape) continue;
                const itemIndex = headers.findIndex((h) => h === 'item' || h.includes('item'));
                const idIndex = headers.findIndex((h) => h === 'id' || h.includes('barcode'));
                const notesIndex = headers.findIndex((h) => h.includes('note'));
                const billIndex = headers.findIndex((h) => h.includes('price') || h.includes('amount') || h.includes('total'));

                for (const row of allRows) {
                    const cells = Array.from(row.querySelectorAll('th, td')).map((cell) => clean(cell.innerText || cell.textContent));
                    if (!cells.some(Boolean)) continue;
                    if (headers.length && cells.map((cell) => cell.toLowerCase()).join('|') === headers.join('|')) continue;
                    const item = cells[itemIndex] || '';
                    const id = cells[idIndex] || clean(row.getAttribute('data-id') || row.getAttribute('data-hs') || '');
                    const notes = cells[notesIndex] || '';
                    const bill = billIndex >= 0 ? (cells[billIndex] || '') : '';
                    if (notes) output.push({item, id, notes, bill});
                }
            }
            if (output.length) break;
        }

        const crmText = Array.from(document.querySelectorAll('#clean_track_box, #cleanTrackBoxMain, .crm-active-orders, #crmActiveOrders, .central-crm, .modal, [role="dialog"]'))
            .filter(visible)
            .map((el) => el.innerText || el.textContent || '')
            .join('\\n');
        if (crmText.includes(orderId)) textBlocks.push(crmText);

        return {
            tableRows: output,
            detailText: Array.from(new Set(textBlocks.map(clean).filter(Boolean))).join('\\n'),
            itemIds,
        };
        """
        ,
        str(order_id),
    ) or {}
    detail_items: list[DetailItem] = []
    for row in result.get("tableRows", []):
        item_id = clean_text(str(row.get("id", "")))
        item_note = clean_text(str(row.get("notes", "")))
        service = clean_text(str(row.get("item", "")))
        bill = parse_money(str(row.get("bill", "")))
        if item_note and not is_non_article_service(service):
            detail_items.append(
                DetailItem(
                    order_id=str(order_id),
                    item_id=item_id,
                    item_note=item_note,
                    service=service,
                    item_bill_amount=bill,
                )
            )
    if detail_items:
        return detail_items
    return detail_items_from_text(
        str(order_id),
        str(result.get("detailText", "")),
        [clean_text(str(item_id)) for item_id in result.get("itemIds", []) if clean_text(str(item_id))],
    )


def fetch_detail_items_for_orders(driver: WebDriver, order_ids: list[str], tab_name: str = "") -> dict[str, list[DetailItem]]:
    details_by_order: dict[str, list[DetailItem]] = {}
    for index, order_id in enumerate(order_ids, start=1):
        print(f"Detail fallback {index}/{len(order_ids)}: opening order {order_id}")
        close_order_detail(driver)
        opened = open_order_detail(driver, order_id, tab_name)
        if not opened:
            details_by_order[order_id] = []
            save_debug_artifacts(driver, f"detail_open_failed_{order_id}")
            continue
        try:
            details = scrape_detail_items(driver, order_id)
            details_by_order[order_id] = details
            if not details:
                save_debug_artifacts(driver, f"detail_no_items_{order_id}")
        finally:
            close_order_detail(driver)
    return details_by_order


def merge_detail_items(tabs: list[ExtractedTab], details_by_order: dict[str, list[DetailItem]]) -> list[ExtractedTab]:
    if not details_by_order:
        return tabs
    merged_tabs: list[ExtractedTab] = []
    for tab in tabs:
        merged_rows = []
        for row in tab.rows:
            order_id = first_value(row, "ID", "Order id", "order_id")
            details = details_by_order.get(order_id, [])
            if details:
                detail_text = "\n".join(
                    (
                        f"{detail.service}: {detail_note_for_parser(detail.item_note, index)}"
                        if detail.service
                        else detail_note_for_parser(detail.item_note, index)
                    )
                    for index, detail in enumerate(details)
                    if detail_note_for_parser(detail.item_note, index)
                )
                item_ids = "\n".join(detail.item_id for detail in details if detail.item_id)
                enriched = dict(row)
                if detail_text:
                    enriched["Article Details"] = detail_text
                    enriched["Clean Article Details"] = detail_text
                    enriched["Detail Fallback Source"] = "order_detail_page"
                if item_ids:
                    enriched["Item IDs"] = item_ids
                merged_rows.append(enriched)
            else:
                merged_rows.append(row)
        merged_tabs.append(ExtractedTab(name=tab.name, rows=merged_rows))
    return merged_tabs


def order_ids_missing_item_notes(tabs: list[ExtractedTab], limit: int) -> list[str]:
    missing: list[str] = []
    seen: set[str] = set()
    for row in article_rows_for_tabs(tabs):
        order_id = str(row.get("order_id", ""))
        if not order_id or order_id in seen:
            continue
        if clean_text(row.get("item_note")):
            continue
        seen.add(order_id)
        missing.append(order_id)
        if limit and len(missing) >= limit:
            break
    return missing


def order_ids_missing_item_notes_for_tab(tab: ExtractedTab, limit: int) -> list[str]:
    return order_ids_missing_item_notes([tab], limit)


def parse_only_order_ids(values: list[str] | None) -> set[str]:
    order_ids: set[str] = set()
    for value in values or []:
        for part in re.split(r"[\s,]+", str(value)):
            cleaned = clean_text(part)
            if cleaned:
                order_ids.add(cleaned)
    return order_ids


def filter_tabs_by_order_ids(tabs: list[ExtractedTab], only_order_ids: set[str]) -> list[ExtractedTab]:
    if not only_order_ids:
        return tabs
    filtered_tabs: list[ExtractedTab] = []
    for tab in tabs:
        filtered_rows = [
            row
            for row in tab.rows
            if first_value(row, "ID", "Order id", "order_id") in only_order_ids
        ]
        filtered_tabs.append(ExtractedTab(name=tab.name, rows=filtered_rows))
    return filtered_tabs


def write_excel(tabs: list[ExtractedTab], output_path: Path) -> None:
    with pd.ExcelWriter(output_path, engine="openpyxl") as writer:
        article_rows = article_rows_for_tabs(tabs)
        clean_items = pd.DataFrame(article_rows)
        if clean_items.empty:
            clean_items = pd.DataFrame(columns=CLEAN_ITEM_COLUMNS)
        else:
            for column in CLEAN_ITEM_COLUMNS:
                if column not in clean_items.columns:
                    clean_items[column] = ""
            clean_items = clean_items[CLEAN_ITEM_COLUMNS]
        clean_items.to_excel(writer, sheet_name="Clean_Items", index=False)

        for tab in tabs:
            sheet_name = re.sub(r"[\[\]:*?/\\]", "", tab.name)[:31] or "Sheet"
            df = pd.DataFrame(tab.rows)
            if df.empty:
                df = pd.DataFrame([{"Message": f"No visible rows found for {tab.name}."}])
            df.to_excel(writer, sheet_name=sheet_name, index=False)

        articles = clean_items.copy()
        if articles.empty:
            articles = pd.DataFrame(columns=CLEAN_ITEM_COLUMNS)
        articles.to_excel(writer, sheet_name="Articles", index=False)


def build_driver(headless: bool, user_data_dir: str | None) -> WebDriver:
    options = Options()
    options.add_argument("--start-maximized")
    options.add_argument("--disable-notifications")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--disable-gpu")
    chrome_binary = os.environ.get("CHROME_BIN") or os.environ.get("GOOGLE_CHROME_BIN")
    if chrome_binary:
        options.binary_location = chrome_binary
    if user_data_dir:
        options.add_argument(f"--user-data-dir={user_data_dir}")
    if headless:
        options.add_argument("--headless=new")
        options.add_argument("--window-size=1600,1200")
    return webdriver.Chrome(options=options)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Export CleanCloud Store Cleaning and Ready tab data to Excel."
    )
    parser.add_argument(
        "--output",
        default="cleancloud_store_export.xlsx",
        help="Excel file path to create. Default: cleancloud_store_export.xlsx",
    )
    parser.add_argument(
        "--tabs",
        nargs="+",
        default=["Cleaning", "Ready"],
        help="Tab names to export. Default: Cleaning Ready",
    )
    parser.add_argument(
        "--login-timeout",
        type=int,
        default=180,
        help="Seconds to wait while you manually log in. Default: 180",
    )
    parser.add_argument(
        "--headless",
        action="store_true",
        help="Run Chrome in headless mode. Only use this if you are already logged in via --user-data-dir.",
    )
    parser.add_argument(
        "--user-data-dir",
        help="Optional Chrome profile directory to reuse login cookies.",
    )
    parser.add_argument(
        "--manual-tabs",
        action="store_true",
        help="You click each tab yourself; the script only extracts the visible data.",
    )
    parser.add_argument(
        "--start-when-ready",
        action="store_true",
        help="Open the browser, let you get to the Cleaning tab yourself, then start scraping after Enter.",
    )
    parser.add_argument(
        "--fill-missing-item-notes",
        action="store_true",
        default=True,
        help="Open order detail pages for rows where the list page has item IDs but blank item notes. Enabled by default.",
    )
    parser.add_argument(
        "--skip-missing-item-notes",
        action="store_false",
        dest="fill_missing_item_notes",
        help="Skip order detail fallback and only use article details visible on the list page.",
    )
    parser.add_argument(
        "--detail-limit",
        type=int,
        default=0,
        help="Maximum orders to open for missing item notes. Use 0 for no limit. Default: 0",
    )
    parser.add_argument(
        "--only-order-ids",
        nargs="+",
        help="Only export and run detail fallback for these order IDs. Accepts space or comma separated values.",
    )
    parser.add_argument(
        "--username",
        default=os.environ.get("CLEAN_CLOUD_USERNAME") or os.environ.get("CLEANCLOUD_USERNAME"),
        help="CleanCloud username/email. Prefer environment variable CLEAN_CLOUD_USERNAME for hosted runs.",
    )
    parser.add_argument(
        "--password",
        default=os.environ.get("CLEAN_CLOUD_PASSWORD") or os.environ.get("CLEANCLOUD_PASSWORD"),
        help="CleanCloud password. Prefer environment variable CLEAN_CLOUD_PASSWORD for hosted runs.",
    )
    parser.add_argument(
        "--otp-file",
        help="Path to a file where the web UI writes the CleanCloud email PIN when requested.",
    )
    parser.add_argument(
        "--otp-request-file",
        help="Path to a marker file created when CleanCloud asks for an email PIN.",
    )
    parser.add_argument(
        "--otp-timeout",
        type=int,
        default=300,
        help="Seconds to wait for CleanCloud email PIN input. Default: 300",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    output_path = Path(args.output).expanduser().resolve()

    driver = build_driver(headless=args.headless, user_data_dir=args.user_data_dir)
    try:
        if args.start_when_ready:
            wait_for_user_ready(driver)
        elif args.username and args.password:
            login_with_credentials(
                driver,
                args.username,
                args.password,
                args.login_timeout,
                args.otp_file,
                args.otp_request_file,
                args.otp_timeout,
            )
        else:
            wait_for_manual_login(driver, args.login_timeout)
        WebDriverWait(driver, 30).until(
            EC.presence_of_element_located((By.TAG_NAME, "body"))
        )

        if args.manual_tabs:
            exported_tabs = [extract_tab_manually(driver, tab_name) for tab_name in args.tabs]
        else:
            exported_tabs = [extract_tab(driver, tab_name) for tab_name in args.tabs]
        only_order_ids = parse_only_order_ids(args.only_order_ids)
        if only_order_ids:
            exported_tabs = filter_tabs_by_order_ids(exported_tabs, only_order_ids)
            print(f"Filtered export to {len(only_order_ids)} requested order IDs.")
        if args.fill_missing_item_notes:
            all_detail_items: dict[str, list[DetailItem]] = {}
            remaining_limit = args.detail_limit
            for tab in exported_tabs:
                tab_limit = remaining_limit if args.detail_limit else 0
                missing_order_ids = order_ids_missing_item_notes_for_tab(tab, tab_limit)
                if not missing_order_ids:
                    continue
                if not args.manual_tabs:
                    click_tab(driver, tab.name)
                print(f"{tab.name}: opening {len(missing_order_ids)} order detail pages to fill blank item notes.")
                all_detail_items.update(fetch_detail_items_for_orders(driver, missing_order_ids, tab.name))
                if args.detail_limit:
                    remaining_limit -= len(missing_order_ids)
                    if remaining_limit <= 0:
                        break
            if all_detail_items:
                exported_tabs = merge_detail_items(exported_tabs, all_detail_items)
            else:
                print("No blank item notes found that need detail-page fallback.")
        write_excel(exported_tabs, output_path)

        for tab in exported_tabs:
            print(f"{tab.name}: exported {len(tab.rows)} rows")
        print(f"Excel file created: {output_path}")
        return 0
    except Exception as exc:
        try:
            save_debug_artifacts(driver, "export_failed")
        except Exception:
            pass
        print(f"Export failed: {exc}", file=sys.stderr)
        return 1
    finally:
        try:
            driver.quit()
        except WebDriverException:
            pass


if __name__ == "__main__":
    raise SystemExit(main())
