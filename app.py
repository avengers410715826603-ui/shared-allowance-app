
import os
import re
import time
import uuid
from datetime import date, datetime
from io import BytesIO
from pathlib import Path
from typing import Optional

import gspread
import streamlit as st
from gspread.exceptions import WorksheetNotFound
from google.oauth2.service_account import Credentials
from PIL import Image, ImageFilter, ImageOps

# =========================
# 基本設定
# =========================
UPLOAD_DIR = Path("uploads")
UPLOAD_DIR.mkdir(exist_ok=True)
MONTHLY_BUDGET = 20000
USERS = ["アベン", "マンゴー"]
WARNING_THRESHOLD = 5000

# 旧名称との互換用
USER_NAME_MAP = {
    "自分": "アベン",
    "彼女": "マンゴー",
    "アベン": "アベン",
    "マンゴー": "マンゴー",
}

# Google Sheets 設定
CREDENTIALS_FILE = "credentials.json"
SPREADSHEET_NAME = "共同財布_記録"
TRANSACTIONS_WORKSHEET_NAME = "transactions"
FIXED_COSTS_WORKSHEET_NAME = "fixed_costs"
GOOGLE_SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]
TRANSACTION_HEADERS = [
    "id",
    "spend_date",
    "user_name",
    "amount",
    "memo",
    "source_type",
    "image_url",
    "ocr_raw_text",
    "created_at",
]
FIXED_COST_HEADERS = [
    "id",
    "user_name",
    "item_name",
    "amount",
    "start_month",
    "end_month",
    "is_active",
    "created_at",
    "updated_at",
]

# Tesseract本体の場所
TESSERACT_CANDIDATE_PATHS = [
    "C:/Program Files/Tesseract-OCR/tesseract.exe",
    "C:/Program Files (x86)/Tesseract-OCR/tesseract.exe",
]

st.set_page_config(page_title="共同財布 お小遣い管理", page_icon="💰", layout="wide")


# =========================
# 共通補助
# =========================
def normalize_user_name(name: str) -> str:
    return USER_NAME_MAP.get(str(name).strip(), str(name).strip())


def is_valid_month_key(value: str) -> bool:
    if not re.fullmatch(r"\d{4}-\d{2}", str(value).strip()):
        return False
    year, month = value.split("-")
    try:
        y = int(year)
        m = int(month)
        return 2000 <= y <= 2100 and 1 <= m <= 12
    except ValueError:
        return False


def current_month_key() -> str:
    return date.today().strftime("%Y-%m")


def format_yen(value: int) -> str:
    return f"¥{value:,}"


def generate_record_id() -> str:
    timestamp = datetime.now().strftime("%Y%m%d%H%M%S%f")
    short_uuid = uuid.uuid4().hex[:8]
    return f"{timestamp}_{short_uuid}"


def clear_cached_data() -> None:
    st.cache_data.clear()


def with_gsheet_retry(func, max_retries: int = 5, base_wait: float = 1.0):
    last_error = None
    for attempt in range(max_retries):
        try:
            return func()
        except Exception as e:
            last_error = e
            if attempt == max_retries - 1:
                break
            time.sleep(base_wait * (2 ** attempt))
    raise last_error


# =========================
# Google Sheets 接続
# =========================
def get_gspread_client() -> gspread.Client:
    try:
        secret_info = st.secrets.get("gcp_service_account")
    except Exception:
        secret_info = None

    if secret_info:
        credentials = Credentials.from_service_account_info(
            dict(secret_info),
            scopes=GOOGLE_SCOPES,
        )
        return gspread.authorize(credentials)

    if not os.path.exists(CREDENTIALS_FILE):
        raise FileNotFoundError(
            f"{CREDENTIALS_FILE} が見つかりません。app.py と同じフォルダに置いてください。"
            "\nまたは Streamlit Cloud では Secrets に gcp_service_account を設定してください。"
        )

    credentials = Credentials.from_service_account_file(
        CREDENTIALS_FILE,
        scopes=GOOGLE_SCOPES,
    )
    return gspread.authorize(credentials)


@st.cache_resource
def get_spreadsheet():
    client = get_gspread_client()
    return client.open(SPREADSHEET_NAME)


def get_or_create_worksheet(name: str, headers: list[str], rows: int = 1000, cols: int = 20):
    spreadsheet = get_spreadsheet()
    try:
        worksheet = spreadsheet.worksheet(name)
    except WorksheetNotFound:
        worksheet = spreadsheet.add_worksheet(title=name, rows=rows, cols=cols)

    header_row = with_gsheet_retry(lambda: worksheet.row_values(1))
    if header_row != headers:
        with_gsheet_retry(lambda: worksheet.update(f"A1:{chr(64 + len(headers))}1", [headers]))
    return worksheet


@st.cache_resource
def get_transactions_worksheet():
    return get_or_create_worksheet(TRANSACTIONS_WORKSHEET_NAME, TRANSACTION_HEADERS, rows=3000, cols=20)


@st.cache_resource
def get_fixed_costs_worksheet():
    return get_or_create_worksheet(FIXED_COSTS_WORKSHEET_NAME, FIXED_COST_HEADERS, rows=1000, cols=20)


@st.cache_data(ttl=10)
def read_all_transactions() -> list[dict]:
    worksheet = get_transactions_worksheet()
    records = with_gsheet_retry(lambda: worksheet.get_all_records(expected_headers=TRANSACTION_HEADERS))

    cleaned_records = []
    for row in records:
        cleaned = {key: row.get(key, "") for key in TRANSACTION_HEADERS}
        cleaned["id"] = str(cleaned.get("id", "")).strip()
        cleaned["amount"] = int(cleaned.get("amount", 0) or 0)
        cleaned["spend_date"] = str(cleaned.get("spend_date", "")).strip()
        cleaned["user_name"] = normalize_user_name(cleaned.get("user_name", ""))
        cleaned["memo"] = str(cleaned.get("memo", "")).strip()
        cleaned["source_type"] = str(cleaned.get("source_type", "")).strip()
        cleaned["image_url"] = str(cleaned.get("image_url", "")).strip()
        cleaned["ocr_raw_text"] = str(cleaned.get("ocr_raw_text", "")).strip()
        cleaned["created_at"] = str(cleaned.get("created_at", "")).strip()
        cleaned_records.append(cleaned)

    return cleaned_records


@st.cache_data(ttl=10)
def read_all_fixed_costs() -> list[dict]:
    worksheet = get_fixed_costs_worksheet()
    records = with_gsheet_retry(lambda: worksheet.get_all_records(expected_headers=FIXED_COST_HEADERS))

    cleaned_records = []
    for row in records:
        cleaned = {key: row.get(key, "") for key in FIXED_COST_HEADERS}
        cleaned["id"] = str(cleaned.get("id", "")).strip()
        cleaned["user_name"] = normalize_user_name(cleaned.get("user_name", ""))
        cleaned["item_name"] = str(cleaned.get("item_name", "")).strip()
        cleaned["amount"] = int(cleaned.get("amount", 0) or 0)
        cleaned["start_month"] = str(cleaned.get("start_month", "")).strip()
        cleaned["end_month"] = str(cleaned.get("end_month", "")).strip()
        cleaned["is_active"] = str(cleaned.get("is_active", "TRUE")).strip().upper() in ["TRUE", "1", "YES", "ON"]
        cleaned["created_at"] = str(cleaned.get("created_at", "")).strip()
        cleaned["updated_at"] = str(cleaned.get("updated_at", "")).strip()
        cleaned_records.append(cleaned)

    return cleaned_records


# =========================
# OCR補助
# =========================
def configure_tesseract() -> bool:
    try:
        import pytesseract
    except ImportError:
        return False

    for candidate in TESSERACT_CANDIDATE_PATHS:
        if os.path.exists(candidate):
            pytesseract.pytesseract.tesseract_cmd = candidate
            return True
    return False


def preprocess_image_for_ocr(image: Image.Image) -> Image.Image:
    img = image.convert("L")
    img = ImageOps.autocontrast(img)
    img = img.resize((img.width * 2, img.height * 2))
    img = img.filter(ImageFilter.SHARPEN)
    img = img.point(lambda x: 255 if x > 180 else 0)
    return img


def ocr_single(image: Image.Image, lang: str = "jpn+eng", psm: int = 6) -> str:
    try:
        import pytesseract
    except ImportError:
        return ""

    if not configure_tesseract():
        return ""

    try:
        config = f"--psm {psm}"
        text = pytesseract.image_to_string(image, lang=lang, config=config)
        return text or ""
    except Exception:
        return ""


def ocr_data(image: Image.Image, lang: str = "jpn+eng", psm: int = 6) -> list[dict]:
    try:
        import pytesseract
        from pytesseract import Output
    except ImportError:
        return []

    if not configure_tesseract():
        return []

    try:
        data = pytesseract.image_to_data(
            image,
            lang=lang,
            config=f"--psm {psm}",
            output_type=Output.DICT,
        )
    except Exception:
        return []

    rows = []
    count = len(data.get("text", []))
    for i in range(count):
        text = (data["text"][i] or "").strip()
        if not text:
            continue
        try:
            conf = float(data["conf"][i])
        except Exception:
            conf = -1.0
        rows.append(
            {
                "text": text,
                "conf": conf,
                "left": int(data["left"][i]),
                "top": int(data["top"][i]),
                "width": int(data["width"][i]),
                "height": int(data["height"][i]),
                "line_num": int(data.get("line_num", [0] * count)[i]),
                "block_num": int(data.get("block_num", [0] * count)[i]),
                "par_num": int(data.get("par_num", [0] * count)[i]),
            }
        )
    return rows


def run_ocr(image: Image.Image, source_type: str) -> str:
    texts = []

    original = image.convert("RGB")
    processed = preprocess_image_for_ocr(original)

    texts.append(ocr_single(original, psm=6))
    texts.append(ocr_single(processed, psm=6))

    if source_type == "paypay":
        width, height = original.size
        center_crop = original.crop((0, int(height * 0.10), int(width * 0.90), int(height * 0.52)))
        center_processed = preprocess_image_for_ocr(center_crop)
        texts.append(ocr_single(center_crop, psm=6))
        texts.append(ocr_single(center_processed, psm=6))

    merged = "\n".join([t for t in texts if t.strip()])
    return merged.strip()


# =========================
# 画像保存
# =========================
def save_image_bytes(image_bytes: bytes, original_name: str, user_name: str) -> str:
    suffix = Path(original_name).suffix or ".png"
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    safe_user = user_name.replace("/", "_")
    file_path = UPLOAD_DIR / f"{timestamp}_{safe_user}{suffix}"

    with open(file_path, "wb") as f:
        f.write(image_bytes)

    return str(file_path)


# =========================
# OCR補助: 金額・日付抽出
# =========================
def normalize_text(text: str) -> str:
    replacements = {
        "，": ",",
        "．": ".",
        "￥": "¥",
        "Y": "¥",
        "y": "¥",
    }
    for before, after in replacements.items():
        text = text.replace(before, after)
    return text


def clean_amount_str(amount_str: str) -> Optional[int]:
    digits = re.sub(r"[^0-9]", "", amount_str)
    if not digits:
        return None
    try:
        value = int(digits)
        if 1 <= value <= 999999:
            return value
    except ValueError:
        return None
    return None


def find_amounts_in_line(line: str) -> list[int]:
    amounts = []
    patterns = [
        r"(?:¥\s*)?(\d{1,3}(?:,\d{3})+|\d{2,6})(?:\s*円)?",
    ]
    for pattern in patterns:
        for match in re.findall(pattern, line):
            value = clean_amount_str(match)
            if value is not None:
                amounts.append(value)
    return amounts


def score_line(line: str, positive_labels: list[str], negative_labels: list[str]) -> int:
    score = 0
    normalized = line.replace(" ", "")

    for label in positive_labels:
        if label in normalized:
            score += 10

    for label in negative_labels:
        if label in normalized:
            score -= 8

    if "¥" in normalized or "円" in normalized:
        score += 2

    if re.search(r"\d{1,3}(?:,\d{3})+|\d{2,6}", normalized):
        score += 1

    return score


PAYPAY_POSITIVE = [
    "支払い金額",
    "支払金額",
    "ご利用金額",
    "決済金額",
    "お支払い金額",
    "支払額",
    "利用金額",
]
PAYPAY_NEGATIVE = [
    "残高",
    "ポイント",
    "付与",
    "還元",
    "日時",
    "時刻",
    "取引番号",
    "注文番号",
    "加盟店",
    "電話番号",
    "残高不足",
]

RECEIPT_POSITIVE = [
    "合計",
    "現計",
    "お買上",
    "領収金額",
    "ご請求額",
    "今回お支払額",
    "お支払額",
    "総合計",
]
RECEIPT_NEGATIVE = [
    "小計",
    "内税",
    "外税",
    "消費税",
    "釣銭",
    "お預り",
    "値引",
    "割引",
    "単価",
    "個数",
]


def guess_paypay_amount_by_layout(image: Image.Image) -> Optional[int]:
    original = image.convert("RGB")
    width, height = original.size

    roi = original.crop(
        (
            int(width * 0.08),
            int(height * 0.10),
            int(width * 0.78),
            int(height * 0.42),
        )
    )
    roi_processed = preprocess_image_for_ocr(roi)

    candidates = []
    for img in [roi, roi_processed]:
        for psm in [6, 11]:
            rows = ocr_data(img, psm=psm)
            if not rows:
                continue

            for row in rows:
                amount = clean_amount_str(row["text"])
                if amount is None:
                    continue
                if amount < 30 or amount > 100000:
                    continue

                cx = row["left"] + row["width"] / 2
                cy = row["top"] + row["height"] / 2
                area = row["width"] * row["height"]
                score = area + max(row["conf"], 0) * 20

                target_x = img.size[0] * 0.40
                target_y = img.size[1] * 0.25
                score -= abs(cx - target_x) * 1.4
                score -= abs(cy - target_y) * 1.8

                if "¥" in row["text"]:
                    score += 120
                if "," in row["text"]:
                    score += 40

                candidates.append((score, amount, row["text"]))

            grouped = {}
            for row in rows:
                key = (row["block_num"], row["par_num"], row["line_num"])
                grouped.setdefault(key, []).append(row)

            for line_rows in grouped.values():
                line_rows = sorted(line_rows, key=lambda r: r["left"])
                line_text = "".join([r["text"] for r in line_rows])
                amount = clean_amount_str(line_text)
                if amount is None:
                    continue
                if amount < 30 or amount > 100000:
                    continue

                min_left = min(r["left"] for r in line_rows)
                min_top = min(r["top"] for r in line_rows)
                max_right = max(r["left"] + r["width"] for r in line_rows)
                max_bottom = max(r["top"] + r["height"] for r in line_rows)
                area = (max_right - min_left) * (max_bottom - min_top)
                avg_conf = sum(max(r["conf"], 0) for r in line_rows) / max(len(line_rows), 1)
                cx = (min_left + max_right) / 2
                cy = (min_top + max_bottom) / 2

                score = area + avg_conf * 25 + 180
                target_x = img.size[0] * 0.40
                target_y = img.size[1] * 0.25
                score -= abs(cx - target_x) * 1.2
                score -= abs(cy - target_y) * 1.6

                if "," in line_text:
                    score += 60
                if any(ch.isdigit() for ch in line_text):
                    score += 20

                candidates.append((score, amount, line_text))

    if not candidates:
        return None

    candidates.sort(key=lambda x: x[0], reverse=True)
    return candidates[0][1]


def guess_amount_from_lines(text: str, source_type: str) -> Optional[int]:
    normalized_text = normalize_text(text)
    lines = [line.strip() for line in normalized_text.splitlines() if line.strip()]

    if source_type == "paypay":
        positive = PAYPAY_POSITIVE
        negative = PAYPAY_NEGATIVE
    else:
        positive = RECEIPT_POSITIVE
        negative = RECEIPT_NEGATIVE

    scored_candidates = []
    for line in lines:
        amounts = find_amounts_in_line(line)
        if not amounts:
            continue

        score = score_line(line, positive, negative)
        for amount in amounts:
            local_score = score
            if amount < 30:
                local_score -= 5
            if amount > 300000:
                local_score -= 5
            scored_candidates.append((local_score, amount, line))

    if scored_candidates:
        scored_candidates.sort(key=lambda x: (x[0], x[1]), reverse=True)
        best_score, best_amount, _ = scored_candidates[0]
        if best_score >= 3:
            return best_amount

    all_amounts = []
    for line in lines:
        all_amounts.extend(find_amounts_in_line(line))

    all_amounts = [a for a in all_amounts if 1 <= a <= 999999]
    if not all_amounts:
        return None

    if source_type == "paypay":
        likely = [a for a in all_amounts if 30 <= a <= 100000]
        if likely:
            return max(likely)

    return max(all_amounts)


def guess_date(text: str) -> Optional[date]:
    text = normalize_text(text)

    patterns = [
        r"(20\d{2})[/-](\d{1,2})[/-](\d{1,2})",
        r"(20\d{2})年(\d{1,2})月(\d{1,2})日",
        r"(\d{2})/(\d{1,2})/(\d{1,2})",
    ]

    for pattern in patterns:
        match = re.search(pattern, text)
        if match:
            parts = match.groups()
            try:
                if len(parts[0]) == 2:
                    year = 2000 + int(parts[0])
                    month = int(parts[1])
                    day = int(parts[2])
                else:
                    year = int(parts[0])
                    month = int(parts[1])
                    day = int(parts[2])
                return date(year, month, day)
            except ValueError:
                continue

    return None


# =========================
# 固定費
# =========================
def is_fixed_cost_applicable(cost: dict, month_key: str) -> bool:
    if not cost.get("is_active", True):
        return False

    start_month = str(cost.get("start_month", "")).strip()
    end_month = str(cost.get("end_month", "")).strip()

    if not is_valid_month_key(start_month):
        return False

    if month_key < start_month:
        return False

    if end_month:
        if not is_valid_month_key(end_month):
            return False
        if month_key > end_month:
            return False

    return True


def get_applicable_fixed_costs(month_key: str, user_name: str) -> list[dict]:
    normalized_user = normalize_user_name(user_name)
    costs = read_all_fixed_costs()
    return [
        cost
        for cost in costs
        if normalize_user_name(cost["user_name"]) == normalized_user and is_fixed_cost_applicable(cost, month_key)
    ]


def get_fixed_cost_total(month_key: str, user_name: str) -> int:
    return sum(int(cost["amount"]) for cost in get_applicable_fixed_costs(month_key, user_name))


def add_fixed_cost(
    user_name: str,
    item_name: str,
    amount: int,
    start_month: str,
    end_month: str = "",
    is_active: bool = True,
) -> None:
    worksheet = get_fixed_costs_worksheet()
    new_id = generate_record_id()
    now_str = datetime.now().isoformat(timespec="seconds")

    with_gsheet_retry(
        lambda: worksheet.append_row(
            [
                new_id,
                normalize_user_name(user_name),
                item_name,
                amount,
                start_month,
                end_month,
                "TRUE" if is_active else "FALSE",
                now_str,
                now_str,
            ],
            value_input_option="USER_ENTERED",
        )
    )
    clear_cached_data()


def get_fixed_cost_row_index(cost_id: str) -> Optional[int]:
    worksheet = get_fixed_costs_worksheet()
    rows = with_gsheet_retry(lambda: worksheet.get_all_values())
    for idx, row in enumerate(rows[1:], start=2):
        if len(row) >= 1 and str(row[0]).strip() == str(cost_id):
            return idx
    return None


def update_fixed_cost(
    cost_id: str,
    user_name: str,
    item_name: str,
    amount: int,
    start_month: str,
    end_month: str,
    is_active: bool,
) -> None:
    worksheet = get_fixed_costs_worksheet()
    row_index = get_fixed_cost_row_index(cost_id)
    if row_index is None:
        raise ValueError("更新対象の固定費が見つかりません。")

    existing = next((x for x in read_all_fixed_costs() if x["id"] == cost_id), None)
    created_at = existing["created_at"] if existing else datetime.now().isoformat(timespec="seconds")
    now_str = datetime.now().isoformat(timespec="seconds")

    row_values = [
        cost_id,
        normalize_user_name(user_name),
        item_name,
        amount,
        start_month,
        end_month,
        "TRUE" if is_active else "FALSE",
        created_at,
        now_str,
    ]
    with_gsheet_retry(
        lambda: worksheet.update(
            f"A{row_index}:I{row_index}",
            [row_values],
            value_input_option="USER_ENTERED",
        )
    )
    clear_cached_data()


def delete_fixed_cost(cost_id: str) -> None:
    worksheet = get_fixed_costs_worksheet()
    row_index = get_fixed_cost_row_index(cost_id)
    if row_index is not None:
        with_gsheet_retry(lambda: worksheet.delete_rows(row_index))
        clear_cached_data()


# =========================
# 取引操作
# =========================
def add_transaction(
    spend_date: date,
    user_name: str,
    amount: int,
    memo: str,
    source_type: str,
    image_path: Optional[str],
    ocr_raw_text: str,
) -> None:
    worksheet = get_transactions_worksheet()
    new_id = generate_record_id()
    with_gsheet_retry(
        lambda: worksheet.append_row(
            [
                new_id,
                spend_date.isoformat(),
                normalize_user_name(user_name),
                amount,
                memo,
                source_type,
                image_path or "",
                ocr_raw_text,
                datetime.now().isoformat(timespec="seconds"),
            ],
            value_input_option="USER_ENTERED",
        )
    )
    clear_cached_data()


def delete_transaction(transaction_id: str) -> None:
    worksheet = get_transactions_worksheet()
    rows = with_gsheet_retry(lambda: worksheet.get_all_values())

    target_row_index = None
    image_path = ""

    for idx, row in enumerate(rows[1:], start=2):
        if len(row) >= 1 and str(row[0]).strip() == str(transaction_id):
            target_row_index = idx
            if len(row) >= 7:
                image_path = row[6]
            break

    if target_row_index is not None:
        with_gsheet_retry(lambda: worksheet.delete_rows(target_row_index))

    if image_path and os.path.exists(image_path):
        try:
            os.remove(image_path)
        except OSError:
            pass

    clear_cached_data()


def get_transactions(month_key: Optional[str] = None):
    rows = read_all_transactions()

    if month_key:
        rows = [row for row in rows if str(row["spend_date"]).startswith(month_key)]

    rows.sort(key=lambda r: (r["spend_date"], r["id"]), reverse=True)
    return rows


def get_month_variable_total(month_key: str, user_name: str) -> int:
    normalized_user = normalize_user_name(user_name)
    rows = read_all_transactions()
    total = 0
    for row in rows:
        if str(row["spend_date"]).startswith(month_key) and normalize_user_name(row["user_name"]) == normalized_user:
            total += int(row["amount"])
    return total


def get_month_history_summary():
    rows = read_all_transactions()
    summary = {}

    for user_name in USERS:
        key = (current_month_key(), user_name)
        summary[key] = {
            "month_key": current_month_key(),
            "user_name": user_name,
            "variable_total": 0,
            "fixed_total": get_fixed_cost_total(current_month_key(), user_name),
            "count_items": 0,
        }

    for row in rows:
        month_key = str(row["spend_date"])[:7]
        user_name = normalize_user_name(row["user_name"])
        key = (month_key, user_name)
        if key not in summary:
            summary[key] = {
                "month_key": month_key,
                "user_name": user_name,
                "variable_total": 0,
                "fixed_total": get_fixed_cost_total(month_key, user_name),
                "count_items": 0,
            }
        summary[key]["variable_total"] += int(row["amount"])
        summary[key]["count_items"] += 1

    result = list(summary.values())
    result.sort(key=lambda x: (x["month_key"], x["user_name"]), reverse=True)
    return result


# =========================
# 画面補助
# =========================
def render_user_status(month_key: str, user_name: str) -> None:
    variable_total = get_month_variable_total(month_key, user_name)
    fixed_total = get_fixed_cost_total(month_key, user_name)
    remaining = MONTHLY_BUDGET - variable_total - fixed_total

    if remaining < 0:
        status = "🚨 使いすぎ"
    elif remaining <= WARNING_THRESHOLD:
        status = "⚠️ 残額少なめ"
    else:
        status = "✅ 範囲内"

    st.metric(
        label=f"{user_name} の残額",
        value=format_yen(remaining),
        delta=f"変動費 {format_yen(variable_total)} / 固定費 {format_yen(fixed_total)}",
    )
    st.caption(status)


def reset_ocr_state() -> None:
    st.session_state["ocr_ready"] = False
    st.session_state["ocr_amount"] = 0
    st.session_state["ocr_date"] = date.today()
    st.session_state["ocr_memo"] = ""
    st.session_state["ocr_raw_text"] = ""
    st.session_state["ocr_image_bytes"] = b""
    st.session_state["ocr_image_name"] = ""
    st.session_state["ocr_upload_token"] = ""
    st.session_state["ocr_source_type"] = ""


def schedule_ocr_clear(message: str = "") -> None:
    st.session_state["clear_ocr_on_next_run"] = True
    st.session_state["flash_message"] = message


# =========================
# セッション初期化
# =========================
if "ocr_ready" not in st.session_state:
    reset_ocr_state()

if st.session_state.get("clear_ocr_on_next_run"):
    st.session_state["clear_ocr_on_next_run"] = False
    reset_ocr_state()


# =========================
# 起動前チェック
# =========================
try:
    get_transactions_worksheet()
    get_fixed_costs_worksheet()
except Exception as e:
    st.error("Google Sheets に接続できません。設定を確認してください。")
    st.code(str(e))
    st.info(
        "確認ポイント：credentials.json が app.py と同じフォルダにあるか、"
        "スプレッドシート名が正しいか、"
        "そのスプレッドシートをサービスアカウントへ共有したか。"
    )
    st.stop()


# =========================
# アプリ本体
# =========================
st.title("💰 共同財布 お小遣い管理アプリ")
st.write("共同財布の中の『お小遣い枠』だけを、2人別々に月2万円で管理するための家計簿アプリです。")
st.caption("文字データは Google Sheets に保存します。画像は現在 uploads フォルダに保存します。")

if st.session_state.get("flash_message"):
    st.success(st.session_state["flash_message"])
    st.session_state["flash_message"] = ""

current_month = current_month_key()
menu_tab1, menu_tab2, menu_tab3, menu_tab4, menu_tab5 = st.tabs(
    ["登録", "今月の状況", "履歴一覧", "固定費設定", "月次集計"]
)


# =========================
# 登録タブ
# =========================
with menu_tab1:
    st.subheader("支出を登録")
    st.write("登録方法は『手入力』『レシート画像』『PayPayスクショ』の3種類に対応しています。")

    entry_method = st.radio(
        "登録方法を選んでください",
        ["手入力", "レシート画像", "PayPayスクショ"],
        horizontal=True,
    )

    source_map = {
        "手入力": "manual",
        "レシート画像": "receipt",
        "PayPayスクショ": "paypay",
    }
    source_type = source_map[entry_method]

    if source_type == "manual":
        manual_user = st.selectbox(
            "使用者",
            USERS,
            index=None,
            placeholder="登録する人を選んでください",
            key="manual_user",
        )
        manual_date = st.date_input("使用日", value=date.today(), key="manual_date")
        manual_amount = st.number_input("金額（円）", min_value=1, step=1, value=100, key="manual_amount")
        manual_memo = st.text_input("メモ", placeholder="例：自販機のジュース", key="manual_memo")

        if st.button("手入力で保存する", use_container_width=True):
            if not manual_user:
                st.error("使用者を選択してください。")
            else:
                add_transaction(
                    spend_date=manual_date,
                    user_name=manual_user,
                    amount=int(manual_amount),
                    memo=manual_memo,
                    source_type="manual",
                    image_path=None,
                    ocr_raw_text="",
                )
                st.success("支出を保存しました。")

    else:
        image_user = st.selectbox(
            "使用者",
            USERS,
            index=None,
            placeholder="登録する人を選んでください",
            key="image_user",
        )
        uploaded_file = st.file_uploader(
            "画像をアップロードしてください",
            type=["png", "jpg", "jpeg", "webp"],
            key=f"upload_{source_type}",
        )

        if uploaded_file is not None:
            image_bytes = uploaded_file.getvalue()
            upload_token = f"{source_type}_{uploaded_file.name}_{len(image_bytes)}"

            if st.session_state.get("ocr_upload_token") != upload_token:
                reset_ocr_state()
                st.session_state["ocr_upload_token"] = upload_token

            image = Image.open(BytesIO(image_bytes)).convert("RGB")
            st.image(image, caption="アップロード画像", use_container_width=True)

            st.info("画像を確認したら、まず『読取する』を押してください。読取後に結果を確認・修正してから保存します。")

            if st.button("読取する", use_container_width=True):
                extracted_text = run_ocr(image, source_type)

                if source_type == "paypay":
                    guessed_amount = guess_paypay_amount_by_layout(image)
                    if guessed_amount is None:
                        guessed_amount = guess_amount_from_lines(extracted_text, source_type)
                else:
                    guessed_amount = guess_amount_from_lines(extracted_text, source_type)

                guessed_spend_date = guess_date(extracted_text) or date.today()

                st.session_state["ocr_ready"] = True
                st.session_state["ocr_amount"] = guessed_amount or 0
                st.session_state["ocr_date"] = guessed_spend_date
                st.session_state["ocr_memo"] = ""
                st.session_state["ocr_raw_text"] = extracted_text
                st.session_state["ocr_image_bytes"] = image_bytes
                st.session_state["ocr_image_name"] = uploaded_file.name
                st.session_state["ocr_source_type"] = source_type

            if st.session_state.get("ocr_ready") and st.session_state.get("ocr_source_type") == source_type:
                st.success("読取が完了しました。内容を確認して、必要なら修正してください。")

                st.date_input("使用日（読取結果を修正可能）", key="ocr_date")
                st.number_input("金額（読取結果を修正可能）", min_value=0, step=1, key="ocr_amount")
                st.text_input("メモ（任意）", placeholder="例：コンビニ、ドラッグストア、ランチ など", key="ocr_memo")

                with st.expander("OCRで読んだテキストを表示"):
                    st.text(st.session_state.get("ocr_raw_text", "")[:5000] or "OCR結果なし")

                if st.button("この内容で保存する", use_container_width=True):
                    final_amount = int(st.session_state.get("ocr_amount", 0))
                    if not image_user:
                        st.error("使用者を選択してください。")
                    elif final_amount <= 0:
                        st.error("金額が0円以下です。正しい金額に修正してから保存してください。")
                    else:
                        image_path = save_image_bytes(
                            st.session_state["ocr_image_bytes"],
                            st.session_state["ocr_image_name"],
                            image_user,
                        )
                        add_transaction(
                            spend_date=st.session_state["ocr_date"],
                            user_name=image_user,
                            amount=final_amount,
                            memo=st.session_state.get("ocr_memo", ""),
                            source_type=source_type,
                            image_path=image_path,
                            ocr_raw_text=st.session_state.get("ocr_raw_text", ""),
                        )
                        schedule_ocr_clear("支出を保存しました。")
                        st.rerun()
        else:
            if st.session_state.get("ocr_ready"):
                schedule_ocr_clear()
                st.rerun()


# =========================
# 今月の状況タブ
# =========================
with menu_tab2:
    st.subheader(f"今月の状況（{current_month}）")

    c1, c2 = st.columns(2)
    with c1:
        render_user_status(current_month, "アベン")
    with c2:
        render_user_status(current_month, "マンゴー")

    aven_variable = get_month_variable_total(current_month, "アベン")
    aven_fixed = get_fixed_cost_total(current_month, "アベン")
    mango_variable = get_month_variable_total(current_month, "マンゴー")
    mango_fixed = get_fixed_cost_total(current_month, "マンゴー")

    st.divider()
    a1, a2, a3 = st.columns(3)
    a1.metric("アベンの変動費", format_yen(aven_variable))
    a2.metric("マンゴーの変動費", format_yen(mango_variable))
    a3.metric("2人の変動費合計", format_yen(aven_variable + mango_variable))

    b1, b2, b3 = st.columns(3)
    b1.metric("アベンの固定費", format_yen(aven_fixed))
    b2.metric("マンゴーの固定費", format_yen(mango_fixed))
    b3.metric("2人の固定費合計", format_yen(aven_fixed + mango_fixed))

    st.caption("※ 毎月1日に、1人2万円の枠から固定費と変動費を差し引いて残額を計算します。")


# =========================
# 履歴一覧タブ
# =========================
with menu_tab3:
    st.subheader("履歴一覧")

    month_filter = st.text_input(
        "表示する月（YYYY-MM形式、空欄なら全件）",
        value=current_month,
        help="例：2026-03",
    ).strip()

    history_user_filter = st.selectbox(
        "表示する人",
        ["全員", "アベン", "マンゴー"],
        index=0,
        key="history_user_filter",
    )

    rows = get_transactions(month_filter if month_filter else None)

    if history_user_filter != "全員":
        rows = [row for row in rows if normalize_user_name(row["user_name"]) == history_user_filter]

    if not rows:
        st.info("該当する履歴はありません。")
    else:
        for row in rows:
            with st.container(border=True):
                top1, top2, top3, top4 = st.columns([2, 2, 2, 1])
                top1.write(f"**日付**: {row['spend_date']}")
                top2.write(f"**使用者**: {normalize_user_name(row['user_name'])}")
                top3.write(f"**金額**: {format_yen(int(row['amount']))}")
                top4.write(f"**種別**: {row['source_type']}")

                st.write(f"**メモ**: {row['memo'] or '-'}")

                if row["image_url"]:
                    try:
                        if str(row["image_url"]).startswith("http://") or str(row["image_url"]).startswith("https://"):
                            st.image(row["image_url"], caption="証跡画像", width=260)
                        elif os.path.exists(row["image_url"]):
                            st.image(row["image_url"], caption="証跡画像", width=260)
                    except Exception:
                        st.caption("画像を表示できませんでした。")

                with st.expander("OCRテキストを見る"):
                    st.text(row["ocr_raw_text"] or "OCRテキストなし")

                delete_key = f"delete_{row['id']}"
                if st.button("この履歴を削除", key=delete_key):
                    delete_transaction(row["id"])
                    st.success("削除しました。画面を再読み込みしてください。")
                    st.stop()


# =========================
# 固定費設定タブ
# =========================
with menu_tab4:
    st.subheader("固定費設定")
    st.write("固定費は複数登録できます。毎月の残額は、固定費を先に差し引いて計算します。")

    with st.form("add_fixed_cost_form", clear_on_submit=True):
        fc_user = st.selectbox(
            "固定費の対象者",
            USERS,
            index=None,
            placeholder="登録する人を選んでください",
            key="fc_user",
        )
        fc_item_name = st.text_input("固定費名", placeholder="例：Netflix", key="fc_item_name")
        fc_amount = st.number_input("固定費金額（円）", min_value=1, step=1, value=1000, key="fc_amount")
        fc_start_month = st.text_input("開始月（YYYY-MM）", value=current_month, key="fc_start_month")
        fc_end_month = st.text_input("終了月（任意・YYYY-MM）", value="", key="fc_end_month")
        submit_fixed_cost = st.form_submit_button("固定費を追加する", use_container_width=True)

        if submit_fixed_cost:
            if not fc_user:
                st.error("対象者を選択してください。")
            elif not fc_item_name.strip():
                st.error("固定費名を入力してください。")
            elif not is_valid_month_key(fc_start_month):
                st.error("開始月は YYYY-MM 形式で入力してください。")
            elif fc_end_month.strip() and not is_valid_month_key(fc_end_month.strip()):
                st.error("終了月は YYYY-MM 形式で入力してください。")
            elif fc_end_month.strip() and fc_end_month.strip() < fc_start_month.strip():
                st.error("終了月は開始月以降にしてください。")
            else:
                add_fixed_cost(
                    user_name=fc_user,
                    item_name=fc_item_name.strip(),
                    amount=int(fc_amount),
                    start_month=fc_start_month.strip(),
                    end_month=fc_end_month.strip(),
                    is_active=True,
                )
                st.success("固定費を追加しました。")
                st.rerun()

    st.divider()
    st.markdown("### 登録済み固定費")

    fixed_costs = read_all_fixed_costs()
    if not fixed_costs:
        st.info("固定費はまだ登録されていません。")
    else:
        for cost in sorted(
            fixed_costs,
            key=lambda x: (normalize_user_name(x["user_name"]), x["item_name"], x["start_month"]),
        ):
            title = f"{normalize_user_name(cost['user_name'])} / {cost['item_name']} / {format_yen(int(cost['amount']))}"
            status = "有効" if cost["is_active"] else "無効"
            with st.expander(f"{title} / {status}"):
                edit_user = st.selectbox(
                    "対象者",
                    USERS,
                    index=USERS.index(normalize_user_name(cost["user_name"])) if normalize_user_name(cost["user_name"]) in USERS else 0,
                    key=f"edit_user_{cost['id']}",
                )
                edit_item_name = st.text_input(
                    "固定費名",
                    value=cost["item_name"],
                    key=f"edit_item_name_{cost['id']}",
                )
                edit_amount = st.number_input(
                    "固定費金額（円）",
                    min_value=1,
                    step=1,
                    value=int(cost["amount"]),
                    key=f"edit_amount_{cost['id']}",
                )
                edit_start_month = st.text_input(
                    "開始月（YYYY-MM）",
                    value=cost["start_month"],
                    key=f"edit_start_month_{cost['id']}",
                )
                edit_end_month = st.text_input(
                    "終了月（任意・YYYY-MM）",
                    value=cost["end_month"],
                    key=f"edit_end_month_{cost['id']}",
                )
                edit_is_active = st.checkbox(
                    "有効にする",
                    value=bool(cost["is_active"]),
                    key=f"edit_is_active_{cost['id']}",
                )

                c1, c2 = st.columns(2)
                with c1:
                    if st.button("この固定費を更新", key=f"update_fixed_cost_{cost['id']}", use_container_width=True):
                        if not edit_item_name.strip():
                            st.error("固定費名を入力してください。")
                        elif not is_valid_month_key(edit_start_month.strip()):
                            st.error("開始月は YYYY-MM 形式で入力してください。")
                        elif edit_end_month.strip() and not is_valid_month_key(edit_end_month.strip()):
                            st.error("終了月は YYYY-MM 形式で入力してください。")
                        elif edit_end_month.strip() and edit_end_month.strip() < edit_start_month.strip():
                            st.error("終了月は開始月以降にしてください。")
                        else:
                            update_fixed_cost(
                                cost_id=cost["id"],
                                user_name=edit_user,
                                item_name=edit_item_name.strip(),
                                amount=int(edit_amount),
                                start_month=edit_start_month.strip(),
                                end_month=edit_end_month.strip(),
                                is_active=edit_is_active,
                            )
                            st.success("固定費を更新しました。")
                            st.rerun()
                with c2:
                    if st.button("この固定費を削除", key=f"delete_fixed_cost_{cost['id']}", use_container_width=True):
                        delete_fixed_cost(cost["id"])
                        st.success("固定費を削除しました。")
                        st.rerun()


# =========================
# 月次集計タブ
# =========================
with menu_tab5:
    st.subheader("月次集計")
    summary_rows = get_month_history_summary()

    if not summary_rows:
        st.info("まだ集計できるデータがありません。")
    else:
        grouped = {}
        for row in summary_rows:
            month_key = row["month_key"]
            user_name = normalize_user_name(row["user_name"])
            variable_total = int(row["variable_total"])
            fixed_total = int(row["fixed_total"])
            count_items = int(row["count_items"])
            remaining = MONTHLY_BUDGET - variable_total - fixed_total
            grouped.setdefault(month_key, {})[user_name] = {
                "variable_total": variable_total,
                "fixed_total": fixed_total,
                "count_items": count_items,
                "remaining": remaining,
            }

        for month_key, users_data in grouped.items():
            st.markdown(f"### {month_key}")
            col1, col2 = st.columns(2)

            for col, user_name in zip([col1, col2], USERS):
                data = users_data.get(
                    user_name,
                    {"variable_total": 0, "fixed_total": get_fixed_cost_total(month_key, user_name), "count_items": 0, "remaining": MONTHLY_BUDGET - get_fixed_cost_total(month_key, user_name)},
                )
                with col:
                    st.write(f"**{user_name}**")
                    st.write(f"変動費: {format_yen(data['variable_total'])}")
                    st.write(f"固定費: {format_yen(data['fixed_total'])}")
                    st.write(f"残額: {format_yen(data['remaining'])}")
                    st.write(f"変動費の件数: {data['count_items']}件")
                    if data["remaining"] < 0:
                        st.error("予算オーバー")
                    elif data["remaining"] <= WARNING_THRESHOLD:
                        st.warning("残額少なめ")
                    else:
                        st.success("範囲内")

            st.divider()


st.sidebar.header("使い方")
st.sidebar.write("1. 『手入力』『レシート画像』『PayPayスクショ』から登録方法を選びます。")
st.sidebar.write("2. 使用者は必ず選択してから登録してください。")
st.sidebar.write("3. 画像登録時は、まず『読取する』を押します。")
st.sidebar.write("4. 読取結果を確認し、必要なら修正してから保存します。")
st.sidebar.write("5. 履歴一覧では人ごとに絞り込めます。")
st.sidebar.write("6. 固定費は複数登録でき、残額計算に反映されます。")
st.sidebar.write("7. 文字データは Google Sheets に保存します。")
st.sidebar.write("8. 画像は現段階では PC の uploads フォルダへ保存します。")

st.sidebar.divider()
st.sidebar.write(f"月予算（1人あたり）: {format_yen(MONTHLY_BUDGET)}")
st.sidebar.write(f"残額注意ライン: {format_yen(WARNING_THRESHOLD)}")
