import json
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
from google.oauth2.service_account import Credentials
from PIL import Image, ImageFilter, ImageOps

# =========================
# 基本設定
# =========================
UPLOAD_DIR = Path("uploads")
UPLOAD_DIR.mkdir(exist_ok=True)
MONTHLY_BUDGET = 20000
USERS = ["自分", "彼女"]
WARNING_THRESHOLD = 5000

# Google Sheets 設定
CREDENTIALS_FILE = "credentials.json"
SPREADSHEET_NAME = "共同財布_記録"
WORKSHEET_NAME = "transactions"
GOOGLE_SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]
SHEET_HEADERS = [
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

# Tesseract本体の場所（PATHが通っていなくても使えるようにする）
TESSERACT_CANDIDATE_PATHS = [
    "C:/Program Files/Tesseract-OCR/tesseract.exe",
    "C:/Program Files (x86)/Tesseract-OCR/tesseract.exe",
]
st.set_page_config(page_title="共同財布 お小遣い管理", page_icon="💰", layout="wide")


# =========================
# Google Sheets 接続
# =========================
def get_gspread_client() -> gspread.Client:
    # 1) Streamlit Community Cloud 用: Secrets management を優先
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

    # 2) ローカル開発用: credentials.json を使う
    if not os.path.exists(CREDENTIALS_FILE):
        raise FileNotFoundError(
            f"{CREDENTIALS_FILE} が見つかりません。app.py と同じフォルダに置いてください。"
            
"または Streamlit Cloud では Secrets に gcp_service_account を設定してください。"
        )

    credentials = Credentials.from_service_account_file(
        CREDENTIALS_FILE,
        scopes=GOOGLE_SCOPES,
    )
    return gspread.authorize(credentials)


@st.cache_resource
def get_worksheet():
    client = get_gspread_client()
    spreadsheet = client.open(SPREADSHEET_NAME)
    worksheet = spreadsheet.worksheet(WORKSHEET_NAME)

    header_row = worksheet.row_values(1)
    if header_row != SHEET_HEADERS:
        worksheet.update("A1:I1", [SHEET_HEADERS])

    return worksheet


@st.cache_data(ttl=10)
def read_all_transactions() -> list[dict]:
    worksheet = get_worksheet()
    records = with_gsheet_retry(lambda: worksheet.get_all_records(expected_headers=SHEET_HEADERS))

    cleaned_records = []
    for row in records:
        cleaned = {key: row.get(key, "") for key in SHEET_HEADERS}
        cleaned["id"] = str(cleaned.get("id", "")).strip()
        cleaned["amount"] = int(cleaned.get("amount", 0) or 0)
        cleaned["spend_date"] = str(cleaned.get("spend_date", "")).strip()
        cleaned["user_name"] = str(cleaned.get("user_name", "")).strip()
        cleaned["memo"] = str(cleaned.get("memo", "")).strip()
        cleaned["source_type"] = str(cleaned.get("source_type", "")).strip()
        cleaned["image_url"] = str(cleaned.get("image_url", "")).strip()
        cleaned["ocr_raw_text"] = str(cleaned.get("ocr_raw_text", "")).strip()
        cleaned["created_at"] = str(cleaned.get("created_at", "")).strip()
        cleaned_records.append(cleaned)

    return cleaned_records


# =========================
# Google Sheets 再試行
# =========================
def with_gsheet_retry(func, max_retries: int = 5, base_wait: float = 1.0):
    last_error = None
    for attempt in range(max_retries):
        try:
            return func()
        except Exception as e:
            last_error = e
            if attempt == max_retries - 1:
                break
            sleep_sec = base_wait * (2 ** attempt)
            time.sleep(sleep_sec)
    raise last_error


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
# 画像保存（現段階ではローカル保存のまま）
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
# Google Sheets 上の記録操作
# =========================
def generate_transaction_id() -> str:
    timestamp = datetime.now().strftime("%Y%m%d%H%M%S%f")
    short_uuid = uuid.uuid4().hex[:8]
    return f"{timestamp}_{short_uuid}"


def add_transaction(
    spend_date: date,
    user_name: str,
    amount: int,
    memo: str,
    source_type: str,
    image_path: Optional[str],
    ocr_raw_text: str,
) -> None:
    worksheet = get_worksheet()
    new_id = generate_transaction_id()
    with_gsheet_retry(
        lambda: worksheet.append_row(
            [
                new_id,
                spend_date.isoformat(),
                user_name,
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
    st.cache_data.clear()


def delete_transaction(transaction_id: int) -> None:
    worksheet = get_worksheet()
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

    st.cache_data.clear()


def get_transactions(month_key: Optional[str] = None):
    rows = read_all_transactions()

    if month_key:
        rows = [row for row in rows if str(row["spend_date"]).startswith(month_key)]

    rows.sort(key=lambda r: (r["spend_date"], r["id"]), reverse=True)
    return rows


def get_month_total(month_key: str, user_name: str) -> int:
    rows = read_all_transactions()
    total = 0
    for row in rows:
        if str(row["spend_date"]).startswith(month_key) and row["user_name"] == user_name:
            total += int(row["amount"])
    return total


def get_month_history_summary():
    rows = read_all_transactions()
    summary = {}

    for row in rows:
        month_key = str(row["spend_date"])[:7]
        user_name = row["user_name"]
        key = (month_key, user_name)
        if key not in summary:
            summary[key] = {
                "month_key": month_key,
                "user_name": user_name,
                "total_amount": 0,
                "count_items": 0,
            }
        summary[key]["total_amount"] += int(row["amount"])
        summary[key]["count_items"] += 1

    result = list(summary.values())
    result.sort(key=lambda x: (x["month_key"], x["user_name"]), reverse=True)
    return result


# =========================
# 画面補助
# =========================
def format_yen(value: int) -> str:
    return f"¥{value:,}"


def current_month_key() -> str:
    return date.today().strftime("%Y-%m")


def render_user_status(month_key: str, user_name: str) -> None:
    used = get_month_total(month_key, user_name)
    remaining = MONTHLY_BUDGET - used

    if remaining < 0:
        status = "🚨 使いすぎ"
    elif remaining <= WARNING_THRESHOLD:
        status = "⚠️ 残額少なめ"
    else:
        status = "✅ 範囲内"

    st.metric(
        label=f"{user_name} の残額",
        value=format_yen(remaining),
        delta=f"使用額 {format_yen(used)}",
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
    get_worksheet()
except Exception as e:
    st.error("Google Sheets に接続できません。設定を確認してください。")
    st.code(str(e))
    st.info(
        "確認ポイント：credentials.json が app.py と同じフォルダにあるか、"
        "スプレッドシート名とシート名が正しいか、"
        "そのスプレッドシートをサービスアカウントへ共有したか。"
    )
    st.stop()


# =========================
# アプリ本体
# =========================
st.title("💰 共同財布 お小遣い管理アプリ")
st.write(
    "共同財布の中の『お小遣い枠』だけを、2人別々に月2万円で管理するための家計簿アプリです。"
)
st.caption("現在、文字データは Google Sheets に保存します。画像は一時的に PC 内の uploads フォルダへ保存します。")

if st.session_state.get("flash_message"):
    st.success(st.session_state["flash_message"])
    st.session_state["flash_message"] = ""

current_month = current_month_key()
menu_tab1, menu_tab2, menu_tab3, menu_tab4 = st.tabs(
    ["登録", "今月の状況", "履歴一覧", "月次集計"]
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
        manual_user = st.selectbox("使用者", USERS, key="manual_user")
        manual_date = st.date_input("使用日", value=date.today(), key="manual_date")
        manual_amount = st.number_input("金額（円）", min_value=1, step=1, value=100, key="manual_amount")
        manual_memo = st.text_input("メモ", placeholder="例：自販機のジュース", key="manual_memo")

        if st.button("手入力で保存する", use_container_width=True):
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
        image_user = st.selectbox("使用者", USERS, key="image_user")
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

                st.date_input(
                    "使用日（読取結果を修正可能）",
                    key="ocr_date",
                )
                st.number_input(
                    "金額（読取結果を修正可能）",
                    min_value=0,
                    step=1,
                    key="ocr_amount",
                )
                st.text_input(
                    "メモ（任意）",
                    placeholder="例：コンビニ、ドラッグストア、ランチ など",
                    key="ocr_memo",
                )

                with st.expander("OCRで読んだテキストを表示"):
                    st.text(st.session_state.get("ocr_raw_text", "")[:5000] or "OCR結果なし")

                if st.button("この内容で保存する", use_container_width=True):
                    final_amount = int(st.session_state.get("ocr_amount", 0))
                    if final_amount <= 0:
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
        render_user_status(current_month, "自分")
    with c2:
        render_user_status(current_month, "彼女")

    total_self = get_month_total(current_month, "自分")
    total_partner = get_month_total(current_month, "彼女")
    total_both = total_self + total_partner

    st.divider()
    a1, a2, a3 = st.columns(3)
    a1.metric("自分の使用額", format_yen(total_self))
    a2.metric("彼女の使用額", format_yen(total_partner))
    a3.metric("2人合計使用額", format_yen(total_both))

    st.caption("※ 毎月1日に、自分2万円・彼女2万円の枠として計算します。")


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

    rows = get_transactions(month_filter if month_filter else None)

    if not rows:
        st.info("該当する履歴はありません。")
    else:
        for row in rows:
            with st.container(border=True):
                top1, top2, top3, top4 = st.columns([2, 2, 2, 1])
                top1.write(f"**日付**: {row['spend_date']}")
                top2.write(f"**使用者**: {row['user_name']}")
                top3.write(f"**金額**: {format_yen(int(row['amount']))}")
                top4.write(f"**種別**: {row['source_type']}")

                st.write(f"**メモ**: {row['memo'] or '-'}")

                if row["image_url"] and os.path.exists(row["image_url"]):
                    try:
                        st.image(row["image_url"], caption="証跡画像", width=260)
                    except Exception:
                        st.caption("画像を表示できませんでした。")

                with st.expander("OCRテキストを見る"):
                    st.text(row["ocr_raw_text"] or "OCRテキストなし")

                delete_key = f"delete_{row['id']}"
                if st.button("この履歴を削除", key=delete_key):
                    delete_transaction(int(row["id"]))
                    st.success("削除しました。画面を再読み込みしてください。")
                    st.stop()


# =========================
# 月次集計タブ
# =========================
with menu_tab4:
    st.subheader("月次集計")
    summary_rows = get_month_history_summary()

    if not summary_rows:
        st.info("まだ集計できるデータがありません。")
    else:
        grouped = {}
        for row in summary_rows:
            month_key = row["month_key"]
            user_name = row["user_name"]
            total_amount = int(row["total_amount"])
            count_items = int(row["count_items"])
            remaining = MONTHLY_BUDGET - total_amount
            grouped.setdefault(month_key, {})[user_name] = {
                "total_amount": total_amount,
                "count_items": count_items,
                "remaining": remaining,
            }

        for month_key, users_data in grouped.items():
            st.markdown(f"### {month_key}")
            col1, col2 = st.columns(2)

            for col, user_name in zip([col1, col2], USERS):
                data = users_data.get(
                    user_name,
                    {"total_amount": 0, "count_items": 0, "remaining": MONTHLY_BUDGET},
                )
                with col:
                    st.write(f"**{user_name}**")
                    st.write(f"使用額: {format_yen(data['total_amount'])}")
                    st.write(f"残額: {format_yen(data['remaining'])}")
                    st.write(f"件数: {data['count_items']}件")
                    if data["remaining"] < 0:
                        st.error("予算オーバー")
                    elif data["remaining"] <= WARNING_THRESHOLD:
                        st.warning("残額少なめ")
                    else:
                        st.success("範囲内")

            st.divider()


st.sidebar.header("使い方")
st.sidebar.write("1. 『手入力』『レシート画像』『PayPayスクショ』から登録方法を選びます。")
st.sidebar.write("2. 画像登録時は、まず『読取する』を押します。")
st.sidebar.write("3. 読取結果を確認し、必要なら修正してから保存します。")
st.sidebar.write("4. PayPayは中央よりやや左上の大きい数字を優先して判定します。")
st.sidebar.write("5. 文字データは Google Sheets に保存します。")
st.sidebar.write("6. 画像は現段階では PC の uploads フォルダへ保存します。")
st.sidebar.write("7. 『今月の状況』『履歴一覧』『月次集計』で確認できます。")

st.sidebar.divider()
st.sidebar.write(f"月予算（1人あたり）: {format_yen(MONTHLY_BUDGET)}")
st.sidebar.write(f"残額注意ライン: {format_yen(WARNING_THRESHOLD)}")
