"""
Employee Research Agent - Local Oracle 26ai Free

Flow:
User question -> AI generates SELECT only -> Python validates SQL
-> Oracle FREEPDB1 executes -> result -> user

Local research schema:
- EMPLOYEES
- DEPARTMENTS
- DESIGNATIONS
"""

from fastapi import FastAPI, Request, Form, UploadFile, File
from fastapi.responses import JSONResponse, RedirectResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

import sys
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
import oracledb
import os
import re
import random
import secrets
import time
from decimal import Decimal
from dotenv import load_dotenv
from openai import OpenAI
import schema as db_schema
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / '.env')

app = FastAPI()

# Session middleware (signed cookie-based sessions)
app.add_middleware(
    SessionMiddleware,
    secret_key=os.environ.get("FLASK_SECRET_KEY", "change_this_secret_key_later"),
    session_cookie="session",
    max_age=None,            # Session cookie (expires when browser closes)
    same_site="lax",
    https_only=False,
)

# Mount static files
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")

# Set up Jinja2 templates
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))

# ---- Local Oracle 26ai Free Database ----
# Thin mode use ho raha hai, is liye old Oracle client / Toad setup touch nahi hota.
DB_HOST = "localhost"
DB_PORT = 1522
DB_SERVICE = "FREEPDB1"

# Login credentials browser/session cookie mein password ke taur par store nahi hote.
# Successful Oracle login ke baad credentials sirf running Python process ki memory mein rehte hain.
ACTIVE_DB_LOGINS = {}

# ---------------------------------------------------------------------------
# Thread-local/context storage for passing session data into business logic
# functions (e.g. get_connection, _reference_names, run_query).
# ---------------------------------------------------------------------------
import contextvars
_current_session_ref = contextvars.ContextVar("_current_session_ref", default=None)


def _get_connection_internal(session_data):
    """
    Current logged-in Oracle user ke credentials se connection banata hai.
    AI ko credentials nahi milte; connection hamesha Python banata hai.
    """
    if session_data is None:
        raise RuntimeError("Database login session available nahi hai. Please dobara login karein.")

    login_token = session_data.get("db_login_token")
    credentials = ACTIVE_DB_LOGINS.get(login_token)

    if not credentials:
        raise RuntimeError("Database login session available nahi hai. Please dobara login karein.")

    dsn = oracledb.makedsn(DB_HOST, DB_PORT, service_name=DB_SERVICE)

    return oracledb.connect(
        user=credentials["username"],
        password=credentials["password"],
        dsn=dsn
    )


def get_connection():
    """
    Current logged-in Oracle user ke credentials se connection banata hai.
    AI ko credentials nahi milte; connection hamesha Python banata hai.
    """
    session_data = _current_session_ref.get()
    return _get_connection_internal(session_data)


GROQ_MODEL = os.environ.get("GROQ_MODEL", "openai/gpt-oss-120b")
GROQ_STT_MODEL = os.environ.get("GROQ_STT_MODEL", "whisper-large-v3-turbo")

# ---------------------------------------------------------------------------
# Server-Side Session & Exact 60-Second Inactivity Enforcement
# ---------------------------------------------------------------------------
SESSION_INACTIVITY_TIMEOUT = int(os.environ.get("SESSION_INACTIVITY_TIMEOUT", "60"))  # 60 seconds (1 minute)


def _clear_session_data(request: Request):
    """Completely purges session credentials, in-memory tokens, and session state."""
    login_token = request.session.get("db_login_token")
    if login_token:
        credentials = ACTIVE_DB_LOGINS.pop(login_token, None)
        if credentials is not None:
            credentials["password"] = None
            credentials.clear()
    request.session.clear()


def _check_session_auth(request: Request, is_api: bool = False):
    """
    Enforces server-side session authentication and exact 60-second inactivity timeout.
    - If valid and active (< 60s inactivity): updates last_activity to now and returns None.
    - If expired (>= 60s inactivity) or unauthenticated: purges session and returns
      a 401 JSONResponse (for API) or a 302 RedirectResponse to /login?reason=expired (for HTML).
    """
    import time
    logged_in = request.session.get("logged_in")
    login_token = request.session.get("db_login_token")
    last_activity = request.session.get("last_activity")
    now = time.time()

    if not logged_in or not login_token or login_token not in ACTIVE_DB_LOGINS:
        _clear_session_data(request)
        if is_api:
            return JSONResponse({"success": False, "error": "not_logged_in"}, status_code=401)
        return RedirectResponse(url="/login", status_code=302)

    if last_activity is not None and (now - float(last_activity)) >= SESSION_INACTIVITY_TIMEOUT:
        logger.info(f"Session expired due to inactivity ({now - float(last_activity):.1f}s >= {SESSION_INACTIVITY_TIMEOUT}s).")
        _clear_session_data(request)
        if is_api:
            return JSONResponse({"success": False, "error": "session_expired"}, status_code=401)
        return RedirectResponse(url="/login?reason=expired", status_code=302)

    request.session["last_activity"] = now
    return None


# ---------------------------------------------------------------------------
# Centralized AI Client Management & Multi-Key Failover
# Supported slots: GROQ_API_KEY_1, GROQ_API_KEY_2, GROQ_API_KEY_3
# Backward compatibility: GROQ_API_KEY
# ---------------------------------------------------------------------------
_groq_client_cache: dict[str, OpenAI] = {}


def get_configured_groq_keys() -> list[str]:
    """Returns list of configured non-empty Groq API keys in preference order (Slot 1 -> Slot 2 -> Slot 3)."""
    keys = []
    # Slot 1: GROQ_API_KEY_1 with fallback to legacy GROQ_API_KEY
    k1 = os.environ.get("GROQ_API_KEY_1", "").strip() or os.environ.get("GROQ_API_KEY", "").strip()
    if k1:
        keys.append(k1)
    k2 = os.environ.get("GROQ_API_KEY_2", "").strip()
    if k2 and k2 not in keys:
        keys.append(k2)
    k3 = os.environ.get("GROQ_API_KEY_3", "").strip()
    if k3 and k3 not in keys:
        keys.append(k3)
    return keys


def get_groq_client(api_key: str) -> OpenAI:
    """Returns cached OpenAI client configured for Groq base URL."""
    if api_key not in _groq_client_cache:
        _groq_client_cache[api_key] = OpenAI(
            api_key=api_key,
            base_url="https://api.groq.com/openai/v1",
            timeout=30.0,
            max_retries=0,  # We manage bounded key-slot failover explicitly
        )
    return _groq_client_cache[api_key]


def _is_retryable_ai_error(exc: Exception) -> bool:
    """
    Identifies temporary/retryable AI API errors:
    - HTTP 429 rate limit / TPM exceeded
    - HTTP 401 / 403 invalid/expired/unauthorized key
    - HTTP 500, 502, 503, 504 server/gateway errors
    - Network timeouts and connection drops
    """
    import openai
    if isinstance(exc, (openai.RateLimitError, openai.AuthenticationError, openai.PermissionDeniedError,
                        openai.InternalServerError, openai.APIConnectionError, openai.APITimeoutError)):
        return True
    status = getattr(exc, "status_code", None)
    if status in (429, 401, 403, 500, 502, 503, 504):
        return True
    err_str = str(exc).lower()
    if any(marker in err_str for marker in ("rate limit", "429", "tpm", "quota", "timeout", "connection error", "503", "502")):
        return True
    return False


_EXHAUSTED_UNTIL = {}
_LAST_KEY_ROTATION = 0


def call_groq_chat_with_fallback(messages: list[dict], **kwargs):
    """
    Executes a chat completion with failover across configured GROQ API key slots (1 -> 2 -> 3)
    and graceful fallback between models (120b -> 20b).
    Only logs credential slot indices; never logs or exposes API keys or secrets.
    """
    global _LAST_KEY_ROTATION
    keys = get_configured_groq_keys()
    if not keys:
        logger.error("No Groq API keys configured.")
        raise RuntimeError("AI service is not configured.")

    kwargs.setdefault("max_tokens", 400)

    models_to_try = [GROQ_MODEL]
    if GROQ_MODEL != "openai/gpt-oss-20b" and "gpt-oss" in GROQ_MODEL:
        models_to_try.append("openai/gpt-oss-20b")

    _LAST_KEY_ROTATION += 1
    n = len(keys)
    rotated_slots = [(i % n + 1, keys[i % n]) for i in range(_LAST_KEY_ROTATION, _LAST_KEY_ROTATION + n)]

    all_pairs = []
    for model_name in models_to_try:
        for slot_idx, key in rotated_slots:
            all_pairs.append((model_name, slot_idx, key))

    last_error = None
    max_rounds = 2
    for round_num in range(max_rounds):
        now = time.time()
        # Prefer pairs that are not currently cooling down
        active_pairs = [p for p in all_pairs if now >= _EXHAUSTED_UNTIL.get((p[0], p[1]), 0)]
        pairs_to_run = active_pairs if active_pairs else all_pairs

        for model_name, slot_idx, key in pairs_to_run:
            now = time.time()
            if active_pairs and now < _EXHAUSTED_UNTIL.get((model_name, slot_idx), 0):
                continue
            try:
                client = get_groq_client(key)
                logger.info(f"Attempting AI request ({model_name}, slot {slot_idx})...")
                return client.chat.completions.create(
                    model=model_name,
                    messages=messages,
                    **kwargs
                )
            except Exception as exc:
                last_error = exc
                err_str = str(exc).lower()
                wait_sec = 5.0
                match = re.search(r"try again in (\d+(?:\.\d+)?)\s*(ms|m|s)?", err_str)
                if match:
                    val = float(match.group(1))
                    unit = (match.group(2) or "s").lower()
                    if unit == "ms":
                        wait_sec = max(1.0, val / 1000.0)
                    elif unit == "m":
                        wait_sec = val * 60.0
                    else:
                        wait_sec = val
                elif "tokens per day" in err_str or "tpd" in err_str:
                    wait_sec = 600.0

                if "tokens per day" not in err_str and "tpd" not in err_str:
                    wait_sec = min(wait_sec + 0.5, 15.0)

                _EXHAUSTED_UNTIL[(model_name, slot_idx)] = time.time() + wait_sec

                if _is_retryable_ai_error(exc):
                    status_code = getattr(exc, "status_code", "temporary/network")
                    logger.warning(
                        f"AI request failed on {model_name} slot {slot_idx} (status: {status_code}): {exc}. Cooldown: {wait_sec:.1f}s."
                    )
                    continue
                else:
                    logger.error(f"Non-retryable AI error on {model_name} slot {slot_idx}: {type(exc).__name__}")
                    raise exc

        if round_num < max_rounds - 1:
            logger.warning(f"All slots/models busy in round {round_num + 1}. Waiting 3s for token replenishment...")
            time.sleep(3.0)

    logger.error(f"All configured AI credential slots and models failed after {max_rounds} rounds. Last error: {type(last_error).__name__}")
    raise RuntimeError("AI service is temporarily busy. Please try again shortly.")


def call_groq_transcribe_with_fallback(audio_file_tuple, prompt: str, **kwargs):
    """
    Executes voice transcription with failover across configured GROQ API key slots (1 -> 2 -> 3).
    """
    keys = get_configured_groq_keys()
    if not keys:
        logger.error("No Groq API keys configured for voice transcription.")
        raise RuntimeError("Voice service is not configured.")

    last_error = None
    for slot_idx, key in enumerate(keys, start=1):
        try:
            client = get_groq_client(key)
            logger.info(f"Attempting voice transcription using credential slot {slot_idx}...")
            return client.audio.transcriptions.create(
                file=audio_file_tuple,
                model=GROQ_STT_MODEL,
                response_format="json",
                temperature=0,
                prompt=prompt,
                **kwargs
            )
        except Exception as exc:
            last_error = exc
            if _is_retryable_ai_error(exc):
                status_code = getattr(exc, "status_code", "temporary/network")
                logger.warning(
                    f"Voice transcription failed on credential slot {slot_idx} (status: {status_code}). "
                    f"{'Trying next slot...' if slot_idx < len(keys) else 'All configured slots exhausted.'}"
                )
                continue
            else:
                logger.error(f"Non-retryable voice transcription error on slot {slot_idx}: {type(exc).__name__}")
                raise exc

    logger.error(f"All {len(keys)} configured voice credential slots failed. Last error: {type(last_error).__name__}")
    raise RuntimeError("Voice service is temporarily busy. Please try again shortly.")


ALLOWED_TABLES = db_schema.ALLOWED_TABLES
IDENTITY_COLUMNS = {"ECODE", "EMP_NAME"}
MAX_DISPLAY_ROWS = 20

HINDI_TO_PAKISTANI_URDU = {
    "kripya": "meherbani karke",
    "dhanyavaad": "shukriya",
    "avashya": "zaroor",
    "sahayata": "madad",
    "vivaran": "tafseel",
    "karmchari": "employee",
}

# Groq Whisper (voice-to-text) kabhi kabhi in words ko galat sunta hai.
# Jaise "IT" ko "N.T" (این ٹی) sun lena, ya "manager"/"منیجر" ko
# "میچنجر"/"میسنجر" jaisa likh dena. Ye fixes AI/keyword-matching tak
# pohanchne se pehle hi in mistakes ko theek kar dete hain.
STT_ARTIFACT_FIXES = {
    "میچنجر": "منیجر",
    "میسنجر": "منیجر",
    "میچینجر": "منیجر",
    "مینیجر": "منیجر",
    "این ٹی": "آئی ٹی",
    "اين تي": "آئی ٹی",
}


def _normalize_stt_artifacts(text):
    """Voice transcription ki aam ghalatiyon ko theek karta hai (downstream
    keyword matching aur AI dono is normalized text par kaam karte hain)."""
    cleaned = str(text or "")
    for wrong, right in STT_ARTIFACT_FIXES.items():
        cleaned = cleaned.replace(wrong, right)
    return cleaned


def _sanitize_roman_urdu(text):
    """Hindi vocabulary ko Pakistani Roman Urdu alternatives se replace karta hai."""
    cleaned = str(text or "")
    for hindi_word, urdu_word in HINDI_TO_PAKISTANI_URDU.items():
        cleaned = re.sub(rf"\b{re.escape(hindi_word)}\b", urdu_word, cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"\bemployee ka tafseel\b", "employee ki tafseel", cleaned, flags=re.IGNORECASE)
    return cleaned


def _is_data_change_request(message):
    """Employee records ko change karne wali aam English/Urdu requests pehchanta hai."""
    text = str(message or "").strip().lower()
    change_markers = (
        "update", "delete", "remove", "insert", "add employee", "change salary",
        "salary zero", "zero kar", "tabdeel kar", "badal do", "hata do", "mita do",
        "اپڈیٹ", "ڈیلیٹ", "حذف", "تبدیل", "بدل", "ہٹا", "مٹا", "زیرو کر",
    )
    return any(marker in text for marker in change_markers)


def _is_oversized_list_request(message):
    """Bohat zyada/all employee records mangne wali request ko AI se pehle rokta hai."""
    text = str(message or "").strip().lower()
    employee_words = r"(?:employees?|employeez|employes|emolyes|emp|ملازمین|ایمپلائیز|ایمپلائز)"

    numbered_request = re.search(rf"\b(\d+)\s+{employee_words}\b", text)
    if numbered_request and int(numbered_request.group(1)) > MAX_DISPLAY_ROWS:
        return True

    large_quantity_words = (
        "hundred employees", "thousand employees", "hazar employees",
        "sau employees", "ہزار ملازمین", "ہزار ایمپلائیز", "سو ملازمین",
    )
    if any(phrase in text for phrase in large_quantity_words):
        return True

    all_markers = (
        "all employees", "every employee", "sab employees", "tamam employees",
        "sare employees", "saare employees", "poori employee list",
        "تمام ملازمین", "سب ملازمین", "سارے ایمپلائیز", "تمام ایمپلائیز",
    )
    detail_markers = (
        "detail", "details", "list", "record", "records", "dikhao", "batao",
        "provide", "share", "تفصیل", "ڈیٹیل", "لسٹ", "ریکارڈ", "دکھاؤ", "بتاؤ",
    )
    return any(marker in text for marker in all_markers) and any(
        marker in text for marker in detail_markers
    )


def _sql_literal(value):
    """A database value ko safe Oracle string literal banata hai."""
    return str(value).replace("'", "''")


def _reference_names():
    """Count filters ke liye actual department/designation names load karta hai."""
    departments = []
    designations = []
    connection = get_connection()
    cursor = connection.cursor()
    try:
        for sql, target in (
            ("SELECT DEPT_NAME FROM DEPARTMENTS", departments),
            ("SELECT DESG_NAME FROM DESIGNATIONS", designations),
        ):
            cursor.execute(sql)
            target.extend(str(row[0]).strip() for row in cursor.fetchall() if row and row[0])
    finally:
        cursor.close()
        connection.close()
    return departments, designations


def _named_reference_in_message(message, names):
    """User text mein longest exact reference name dhoondta hai."""
    text = re.sub(r"[^a-z0-9]+", " ", str(message or "").lower()).strip()
    padded = f" {text} "
    for name in sorted(names, key=len, reverse=True):
        normalized = re.sub(r"[^a-z0-9]+", " ", name.lower()).strip()
        if normalized and f" {normalized} " in padded:
            return name
    return None


GENERIC_COUNT_TOKENS = {
    # english
    "total", "employees", "employee", "employeez", "employes", "emolyes", "emp",
    "how", "many", "count", "overall", "all", "the", "number", "of", "are", "there", "is",
    # roman urdu
    "kitne", "kitni", "hain", "hai", "sab", "tamam", "sare", "saare", "tadaad", "kul",
    "ka", "ki", "ke", "mein", "me", "hn",
    # urdu script
    "\u0645\u0644\u0627\u0632\u0645\u06cc\u0646", "\u0627\u06cc\u0645\u067e\u0644\u0627\u0626\u06cc\u0632",
    "\u0627\u06cc\u0645\u067e\u0644\u0627\u0626\u0632", "\u06a9\u062a\u0646\u06d2", "\u06a9\u062a\u0646\u06cc",
    "\u06a9\u0644", "\u062a\u0639\u062f\u0627\u062f", "\u06c1\u06cc\u06ba", "\u06c1\u06d2",
    "\u0645\u06cc\u06ba", "\u06a9\u0627", "\u06a9\u06cc", "\u06a9\u06d2", "\u0633\u0628", "\u062a\u0645\u0627\u0645",
}


def _is_plain_total_request(text):
    """
    Agar message mein sirf generic count/employee alfaz hon (koi department ya
    designation ka hint na ho), tabhi ye ek plain 'total employees' sawal hai.
    Warna false-positive total kabhi return nahi karna — user shayad kisi
    specific department/designation ke baare mein poochh raha ho jise hum
    literal string-match se pehchan nahi paaye (jaise Urdu script naam).
    """
    tokens = re.findall(r"[^\W\d_]+", text, flags=re.UNICODE)
    leftover = [t for t in tokens if t.lower() not in GENERIC_COUNT_TOKENS]
    return len(leftover) == 0


def _is_salary_aggregate_intent(message):
    """
    Detects whether the user question is asking for a salary aggregate
    (SUM, AVG, MIN, MAX of salary) rather than a simple COUNT of employees.
    This must fire BEFORE the count shortcut to prevent misrouting.
    Returns True if salary aggregate intent is detected.
    """
    text = str(message or "").strip().lower()
    # Salary-related keywords across all three languages
    salary_words = (
        "salary", "salaries", "tankhwa", "tankha", "tankhwah", "payroll",
        "\u062a\u0646\u062e\u0648\u0627\u06c1",  # تنخواہ
    )
    # Aggregate operation keywords across all three languages
    agg_cues = (
        "total salary", "sum salary", "sum of salary", "total payroll",
        "total tankhwa", "kul tankhwa", "kul salary", "total tankha",
        "average salary", "avg salary", "mean salary",
        "average tankhwa", "ausat salary", "ausat tankhwa",
        "highest salary", "lowest salary", "maximum salary", "minimum salary",
        "sab se zyada salary", "sab se kam salary",
        "sab se zyada tankhwa", "sab se kam tankhwa",
        # Urdu script patterns
        "\u06a9\u0644 \u062a\u0646\u062e\u0648\u0627\u06c1",      # کل تنخواہ
        "\u0627\u0648\u0633\u0637 \u062a\u0646\u062e\u0648\u0627\u06c1",  # اوسط تنخواہ
        "\u0632\u06cc\u0627\u062f\u06c1 \u062a\u0646\u062e\u0648\u0627\u06c1",  # زیادہ تنخواہ
        "\u06a9\u0645 \u062a\u0646\u062e\u0648\u0627\u06c1",      # کم تنخواہ
    )
    # Check for explicit aggregate + salary phrases
    if any(cue in text for cue in agg_cues):
        return True
    # Check for aggregate operation word near a salary word
    agg_ops = (
        "total", "sum", "average", "avg", "mean", "ausat",
        "\u06a9\u0644",    # کل
        "\u0627\u0648\u0633\u0637",  # اوسط
    )
    has_salary = any(sw in text for sw in salary_words)
    has_agg = any(op in text for op in agg_ops)
    if has_salary and has_agg:
        return True
    return False


def _has_extra_filters(message):
    """
    Detects whether a count-like question has additional filters beyond
    just a department/designation name (e.g., gender, city, salary threshold).
    If so, the simple count shortcut should be bypassed.
    """
    text = str(message or "").strip().lower()
    extra_filter_cues = (
        # Gender
        "female", "male", "khawateen", "khateen", "mard",
        "\u062e\u0648\u0627\u062a\u06cc\u0646",  # خواتین
        "\u0645\u0631\u062f",                      # مرد
        # City indicators
        "faisalabad", "lahore", "karachi", "islamabad", "rawalpindi",
        "peshawar", "quetta", "multan", "gujranwala", "sialkot",
        "hyderabad", "sargodha", "bahawalpur", "mardan", "sukkur",
        "gujrat", "jhang", "kasur", "sheikhupura", "larkana",
        "\u0641\u06cc\u0635\u0644 \u0622\u0628\u0627\u062f",  # فیصل آباد
        "\u0644\u0627\u06c1\u0648\u0631",              # لاہور
        "\u06a9\u0631\u0627\u0686\u06cc",              # کراچی
        "\u0627\u0633\u0644\u0627\u0645 \u0622\u0628\u0627\u062f",  # اسلام آباد
        # Salary threshold
        "salary above", "salary below", "salary greater", "salary less",
        "salary se zyada", "salary se kam", "salary zyada",
        "\u062a\u0646\u062e\u0648\u0627\u06c1 \u0632\u06cc\u0627\u062f\u06c1",  # تنخواہ زیادہ
        "\u062a\u0646\u062e\u0648\u0627\u06c1 \u06a9\u0645",              # تنخواہ کم
        # Explicit combined-filter cues
        "from", "se hain", "se hai",
    )
    # Check for salary numeric thresholds like "above 80000" or "se zyada hai"
    if re.search(r"salary.*\d{4,}", text) or re.search(r"\d{4,}.*salary", text):
        return True
    if re.search(r"\u062a\u0646\u062e\u0648\u0627\u06c1.*\d{4,}", text):  # تنخواہ + number
        return True
    # Check if multiple filter categories are mentioned
    filter_count = sum(1 for cue in extra_filter_cues if cue in text)
    # Having gender + city, or gender + department already implies multi-filter
    gender_present = any(g in text for g in ("female", "male", "khawateen", "mard",
                                              "\u062e\u0648\u0627\u062a\u06cc\u0646", "\u0645\u0631\u062f"))
    if gender_present:
        return True
    return filter_count >= 2


def _count_query_for(message, department_names=None, designation_names=None):
    """Count request mein entity aur filter ko alag pehchanta hai."""
    text = str(message or "").strip().lower()

    # Word boundary regex for count triggers (avoid matching 'accounts' or 'accountant' as 'count')
    if not re.search(r"\b(how many|total|count|headcount|kitne|kitni|tadaad|\u06a9\u062a\u0646\u06d2|\u06a9\u062a\u0646\u06cc|\u062a\u0639\u062f\u0627\u062f)\b", text):
        return None

    # Bypass shortcut for queries that ask for specific attributes, conditions, rankings, or groupings
    bypass_cues = (
        "name", "names", "naam", "salary", "salaries", "tankhwa", "tankha",
        "email", "address", "phone", "highest", "lowest", "sab se", "between",
        "each", "har ", "mukhtalif", "different", "who", "kon", "kaun",
        "\u06a9\u0648\u0646", "\u0646\u0627\u0645", "\u062a\u0646\u062e\u0648\u0627\u06c1", "\u06c1\u0631 ", "\u0645\u062e\u062a\u0644\u0641",
        "earns", "working as", "kaam kar", "list", "dikhao",
    )
    if any(cue in text for cue in bypass_cues):
        return None

    employees = (
        "employee", "employees", "staff", "banda", "bande", "banday", "log", "afrad", "headcount",
        "\u0645\u0644\u0627\u0632\u0645", "\u0645\u0644\u0627\u0632\u0645\u06cc\u0646", "\u0628\u0646\u062f\u06d2", "\u0644\u0648\u06af",
        "\u0627\u06cc\u0645\u067e\u0644\u0627\u0626\u06cc\u0632", "\u0627\u0641\u0631\u0627\u062f"
    )
    departments = ("department", "departments", "dept", "\u0688\u06cc\u067e\u0627\u0631\u0679\u0645\u0646\u0679", "\u0634\u0639\u0628\u06d2")
    designations = ("designation", "designations", "desig", "job title", "\u0688\u06cc\u0632\u06af\u0646\u06cc\u0634\u0646", "\u0639\u06c1\u062f\u06d2")

    # Multi-filter detection: agar gender, city, salary threshold jaisi
    # extra conditions bhi hon to simple count shortcut bypass karo.
    if _has_extra_filters(text):
        return None

    if any(x in text for x in employees):
        department = _named_reference_in_message(text, department_names or [])
        designation = _named_reference_in_message(text, designation_names or [])
        if designation:
            alias = re.sub(r"[^A-Z0-9]+", "_", designation.upper()).strip("_")
            return (f"SELECT COUNT(*) AS TOTAL_{alias}_EMPLOYEES FROM EMPLOYEES e, DESIGNATIONS g "
                    f"WHERE e.DESG_CODE = g.DESG_CODE AND UPPER(g.DESG_NAME) = '{_sql_literal(designation.upper())}'")
        if department:
            alias = re.sub(r"[^A-Z0-9]+", "_", department.upper()).strip("_")
            return (f"SELECT COUNT(*) AS TOTAL_{alias}_EMPLOYEES FROM EMPLOYEES e, DEPARTMENTS d "
                    f"WHERE e.DEPT_CODE = d.DEPT_CODE AND UPPER(d.DEPT_NAME) = '{_sql_literal(department.upper())}'")
        if "manager" in text or "\u0645\u0646\u06cc\u062c\u0631" in text:
            return "SELECT COUNT(*) AS TOTAL_MANAGER_EMPLOYEES FROM EMPLOYEES e, DESIGNATIONS g WHERE e.DESG_CODE = g.DESG_CODE AND UPPER(g.DESG_NAME) LIKE '%MANAGER%'"
        if _is_plain_total_request(text):
            return "SELECT COUNT(*) AS TOTAL_EMPLOYEES FROM EMPLOYEES"
        return None

    # Plain department count: e.g. "how many departments are there" or "total departments"
    if any(x in text for x in ("how many departments", "total departments", "departments kitne", "departments count", "\u06a9\u0644 \u06a9\u062a\u0646\u06d2 \u0634\u0639\u0628\u06d2", "\u06a9\u062a\u0646\u06d2 \u0688\u06cc\u067e\u0627\u0631\u0679\u0645\u0646\u0679", "\u06a9\u0644 \u0688\u06cc\u067e\u0627\u0631\u0679\u0645\u0646\u0679")):
        return "SELECT COUNT(*) AS TOTAL_DEPARTMENTS FROM DEPARTMENTS"

    # Plain designation count: e.g. "how many designations are there" or "total designations"
    if any(x in text for x in ("how many designations", "total designations", "designations kitne", "designations count", "\u06a9\u0644 \u06a9\u062a\u0646\u06d2 \u0639\u06c1\u062f\u06d2", "\u06a9\u062a\u0646\u06d2 \u0688\u06cc\u0632\u06af\u0646\u06cc\u0634\u0646")):
        return "SELECT COUNT(*) AS TOTAL_DESIGNATIONS FROM DESIGNATIONS"

    return None


def _detect_typed_language(message):
    """Short typed follow-ups ko conversation history se independent classify karta hai."""
    text = str(message or "").strip().lower()
    if re.search(r"[\u0600-\u06ff]", text):
        return "UR"

    words = set(re.findall(r"[a-z']+", text))
    urdu_markers = {
        "kya", "kiya", "kon", "kaun", "ka", "ki", "ke", "hai", "hain",
        "uska", "uski", "unka", "unki", "iska", "iski", "mujhe", "muje",
        "batao", "batayein", "kitne", "kitni", "wala", "wali", "mein", "ho"
    }
    english_markers = {
        "his", "her", "their", "what", "who", "whose", "how", "which",
        "tell", "show", "find", "give", "is", "are", "the", "about", "employees"
    }
    urdu_score = len(words & urdu_markers)
    english_score = len(words & english_markers)
    if english_score > urdu_score:
        return "EN"
    if urdu_score:
        return "UR"
    return None




def _display_value(label, value):
    """UI text ke liye Oracle values ko clean format karta hai."""
    if value is None:
        return ""

    label_upper = str(label).upper()

    # Salary / salary aggregates ko whole rounded number mein show karo.
    # Example: 66000.0 -> 66,000
    if "SALARY" in label_upper:
        try:
            return f"{int(round(float(value))):,}"
        except (TypeError, ValueError):
            return str(value)

    # Oracle NUMBER kabhi Decimal/float form mein aa sakta hai.
    # Agar value whole number ho to unnecessary .0 hata do.
    if isinstance(value, (Decimal, float)):
        try:
            numeric = float(value)
            if numeric.is_integer():
                return f"{int(numeric):,}"
        except Exception:
            pass

    return str(value)


def _answer_label(column, lang):
    labels = {
        "DEPARTMENT_NAME": ("Department", "Department"), "DEPT_NAME": ("Department", "Department"),
        "DESIGNATION_NAME": ("Designation", "Designation"), "DESG_NAME": ("Designation", "Designation"),
        "PHONE_NUMBER": ("Phone number", "Phone number"), "SALARY": ("Salary", "Salary"),
        "EMAIL": ("Email", "Email"), "JOINING_DATE": ("Joining date", "Joining date"),
        "GENDER": ("Gender", "Gender"), "CITY": ("City", "Shehar"), "ADDRESS": ("Address", "Pata"),
    }
    en, ur = labels.get(column.upper(), (column.replace("_", " ").title(), column.replace("_", " ").title()))
    return ur if lang == "UR" else en


def format_answer(columns, rows, lang="EN", question=""):
    """Oracle result ko simple readable answer mein convert karta hai."""
    if not rows:
        return None

    blocks = []
    question_lower = (question or "").lower()
    manager_question = any(word in question_lower for word in ("manager", "منیجر"))

    for row in rows:
        row_dict = dict(zip(columns, row))
        code = row_dict.get("ECODE")
        name = row_dict.get("EMP_NAME")

        other_fields = {
            k: v for k, v in row_dict.items()
            if k not in IDENTITY_COLUMNS and v is not None
        }

        # Role questions ka direct, context-aware jawab dein instead of "mil gaya".
        if manager_question and name and code is not None:
            department = row_dict.get("DEPARTMENT_NAME") or row_dict.get("DEPT_NAME")
            role = row_dict.get("DESIGNATION_NAME") or row_dict.get("DESG_NAME") or "Manager"
            summary_lines = [f"• Name: {name}"]
            if department:
                summary_lines.append(f"• Department: {department}")
            summary_lines.append(f"• Designation: {role}")
            if re.search(r"\b(?:ecode|employee\s*code|code)\b", question_lower):
                summary_lines.append(f"• ECODE: {_display_value('ECODE', code)}")
            extra_fields = {
                key: value for key, value in other_fields.items()
                if key not in {"DEPARTMENT_NAME", "DEPT_NAME", "DESIGNATION_NAME", "DESG_NAME"}
            }
            summary_lines.extend(
                f"• {_answer_label(key, lang)}: {_display_value(key, value)}"
                for key, value in extra_fields.items()
            )
            blocks.append("\n".join(summary_lines))
            continue

        if name and code is not None:
            code_requested = bool(re.search(r"\b(?:ecode|employee\s*code|code)\b", question_lower))
            repeatable_single_detail = len(other_fields) == 1 and not code_requested
            ref = name if repeatable_single_detail else f"{name} (ECODE {code})"
        elif name:
            ref = name
        elif code is not None:
            ref = f"Employee {code}"
        else:
            ref = None

        if ref:
            if len(other_fields) == 1:
                label, value = list(other_fields.items())[0]
                label_text = label.replace("_", " ").title()
                clean_value = _display_value(label, value)
                if lang == "UR":
                    natural_label = label.replace("_", " ").lower()
                    possessive = "ki" if label.upper() in {"SALARY", "JOINING_DATE", "CITY"} else "ka"
                    blocks.append(f"{ref} {possessive} {natural_label} {clean_value} hai.")
                else:
                    blocks.append(f"{ref}'s {label_text.lower()} is {clean_value}.")
            elif not other_fields:
                blocks.append(
                    f"Employee ka record mil gaya: {name} (ECODE {code})." if lang == "UR" and name and code is not None
                    else f"Employee record found: {name} (ECODE {code})." if name and code is not None
                    else f"{ref}."
                )
            else:
                lines = [f"{ref}:"]
                for key, value in other_fields.items():
                    clean_value = _display_value(key, value)
                    lines.append(f"• {key.replace('_', ' ').title()}: {clean_value}")
                blocks.append("\n".join(lines))
        else:
            if len(other_fields) == 1:
                label, value = list(other_fields.items())[0]
                blocks.append(_format_aggregate_sentence(label, value, lang))
            else:
                lines = [
                    f"{key.replace('_', ' ').title()}: {_display_value(key, value)}"
                    for key, value in other_fields.items()
                ]
                blocks.append("\n".join(lines))

    return "\n\n".join(blocks)


TOTAL_EMPLOYEES_SENTENCE_VARIANTS = {
    "EN": [
        "The total number of employees is {value}.",
        "There are {value} employees in total.",
        "Overall, the company currently has {value} employees.",
    ],
    "UR": [
        "Employees ki total tadaad {value} hai.",
        "Company mein is waqt total {value} employees hain.",
        "Overall {value} employees hain.",
    ],
}

TOTAL_DEPARTMENTS_SENTENCE_VARIANTS = {
    "EN": [
        "The total number of departments is {value}.",
        "There are {value} departments in total.",
    ],
    "UR": [
        "Departments ki total tadaad {value} hai.",
        "Company mein total {value} departments hain.",
    ],
}

TOTAL_DESIGNATIONS_SENTENCE_VARIANTS = {
    "EN": [
        "The total number of designations is {value}.",
        "There are {value} designations in total.",
    ],
    "UR": [
        "Designations ki total tadaad {value} hai.",
        "Company mein total {value} designations hain.",
    ],
}

FILTERED_COUNT_DEPARTMENT_VARIANTS = {
    "EN": [
        "There are {value} employees in the {context} department.",
        "The {context} department currently has {value} employees.",
        "{context} department's employee count is {value}.",
    ],
    "UR": [
        "{context} department mein employees ki total tadaad {value} hai.",
        "{context} department mein is waqt {value} employees hain.",
        "{context} department ka total employee count {value} hai.",
    ],
}

FILTERED_COUNT_DESIGNATION_VARIANTS = {
    "EN": [
        "There are {value} employees with the {context} designation.",
        "The {context} designation currently has {value} employees.",
        "{context} employee count comes to {value}.",
    ],
    "UR": [
        "{context} designation walay employees ki total tadaad {value} hai.",
        "{context} ke total {value} employees hain.",
        "{context} ka total employee count {value} hai.",
    ],
}

FILTERED_COUNT_SENTENCE_VARIANTS = {
    "EN": [
        "There are {value} employees in {context}.",
        "{context} currently has {value} employees.",
        "The employee count for {context} is {value}.",
    ],
    "UR": [
        "{context} mein employees ki total tadaad {value} hai.",
        "{context} mein is waqt {value} employees hain.",
        "{context} ka total employee count {value} hai.",
    ],
}


def _classify_context_category(qualifier_upper):
    """Alias se nikla filter naam department hai ya designation, DB ki asal
    reference list se check karta hai (best-effort; fail-safe)."""
    try:
        department_names, designation_names = _reference_names()
    except Exception:
        return None

    normalized_qualifier = re.sub(r"[^A-Z0-9]+", " ", qualifier_upper).strip()
    for name in department_names:
        if re.sub(r"[^A-Z0-9]+", " ", name.upper()).strip() == normalized_qualifier:
            return "department"
    for name in designation_names:
        if re.sub(r"[^A-Z0-9]+", " ", name.upper()).strip() == normalized_qualifier:
            return "designation"
    return None


def _prettify_context_name(raw_upper):
    """
    Alias se nikala gaya context (e.g. 'IT', 'SUPPLY CHAIN', 'ACCOUNTS MANAGER')
    ko readable banata hai. Chhote acronyms (<=3 letters, jaise IT, HR) apne
    original uppercase mein rehte hain; baaki words Title Case ban jaate hain.
    """
    words = raw_upper.split()
    pretty_words = [w if len(w) <= 3 else w.capitalize() for w in words]
    return " ".join(pretty_words)


def _format_aggregate_sentence(label, value, lang):
    label_upper = label.upper()
    clean_value = _display_value(label, value)
    lang_key = lang if lang in ("EN", "UR") else "EN"

    if label_upper in {"TOTAL", "TOTAL_EMPLOYEES"}:
        return random.choice(TOTAL_EMPLOYEES_SENTENCE_VARIANTS[lang_key]).format(value=clean_value)
    if label_upper == "TOTAL_DEPARTMENTS":
        return random.choice(TOTAL_DEPARTMENTS_SENTENCE_VARIANTS[lang_key]).format(value=clean_value)
    if label_upper == "TOTAL_DESIGNATIONS":
        return random.choice(TOTAL_DESIGNATIONS_SENTENCE_VARIANTS[lang_key]).format(value=clean_value)

    if label_upper.startswith("TOTAL_"):
        qualifier = label_upper[len("TOTAL_"):].replace("_", " ").strip()
        if qualifier.endswith(" EMPLOYEES"):
            qualifier = qualifier[: -len(" EMPLOYEES")].strip()
        if not qualifier:
            return random.choice(TOTAL_EMPLOYEES_SENTENCE_VARIANTS[lang_key]).format(value=clean_value)
        context = _prettify_context_name(qualifier)
        category = _classify_context_category(qualifier)
        if category == "department":
            return random.choice(FILTERED_COUNT_DEPARTMENT_VARIANTS[lang_key]).format(context=context, value=clean_value)
        if category == "designation":
            return random.choice(FILTERED_COUNT_DESIGNATION_VARIANTS[lang_key]).format(context=context, value=clean_value)
        return random.choice(FILTERED_COUNT_SENTENCE_VARIANTS[lang_key]).format(context=context, value=clean_value)

    pretty_label = label.replace("_", " ").title()
    return f"{pretty_label}: {clean_value}"


def classify_and_respond(message, history=None, preferred_language=None):
    """
    AI ek hi call mein:
    1) language detect karta hai
    2) domain classify karta hai
    3) employee-data question ho to SELECT query generate karta hai
    """

    if not get_configured_groq_keys():
        raise RuntimeError(
            "GROQ_API_KEY .env file mein set nahi hai. "
            "Project folder ki .env file mein apni Groq API key add karein."
        )

    schema_text = db_schema.build_schema_description()
    relations_text = db_schema.build_relations_description()

    # AI ko department/designation ke asal DB spellings dete hain taake wo
    # Urdu script, Roman Urdu, ya English -- kisi bhi form mein poocha gaya
    # naam sahi column value se match kar sake, guess kar ke wrong SQL na banaye.
    try:
        department_names, designation_names = _reference_names()
    except Exception:
        department_names, designation_names = [], []

    reference_names_text = (
        "Departments: " + (", ".join(sorted(department_names)) if department_names else "(none found)") + "\n"
        "Designations: " + (", ".join(sorted(designation_names)) if designation_names else "(none found)")
    )

    selected_language = "UR" if preferred_language == "UR" else "EN" if preferred_language == "EN" else None
    language_mode = (
        "The user selected URDU mode. You MUST output LANG=UR. For CHAT and DOMAIN, reply only in Pakistani Roman Urdu, never Urdu script."
        if selected_language == "UR"
        else "The user selected ENGLISH mode. You MUST output LANG=EN. For CHAT and DOMAIN, reply only in clear, simple professional English, even if the input is Urdu or Roman Urdu."
        if selected_language == "EN"
        else "No language mode was selected; detect the language using the rules below."
    )

    full_detail_query = (
        "SELECT e.ECODE, e.EMP_NAME, d.DEPT_NAME AS DEPARTMENT_NAME, "
        "g.DESG_NAME AS DESIGNATION_NAME, e.SALARY, e.PHONE_NUMBER, "
        "e.EMAIL, e.JOINING_DATE, e.GENDER, e.CITY, e.ADDRESS "
        "FROM EMPLOYEES e, DEPARTMENTS d, DESIGNATIONS g "
        "WHERE e.DEPT_CODE = d.DEPT_CODE "
        "AND e.DESG_CODE = g.DESG_CODE "
        "AND e.ECODE = 1001"
    )

    system_prompt = f"""
You are a helpful Employee Research Assistant and Oracle SQL generator.

DATABASE SCHEMA:
{schema_text}

RELATIONS:
{relations_text}

REFERENCE DATA (exact spelling as stored in the database right now):
{reference_names_text}
- When the user names a department or designation in English, Urdu script, or Roman Urdu, match it to the closest entry above and use that EXACT spelling (case-insensitive) in UPPER(...) filters.
- If nothing in this list reasonably matches what the user asked for, classify as DOMAIN.

LANGUAGE:
{language_mode}
- LANG=EN for English.
- LANG=UR for Urdu script or Roman Urdu. Understand Urdu script input, but always write replies in natural Pakistani Roman Urdu. Never use Urdu script or Hindi vocabulary in the reply.
- Casual conversation (CHAT): reply warmly and concisely in Pakistani Roman Urdu or English with 1-2 friendly emojis.

CLASSIFY INTO:
- CHAT: casual conversation.
- SQL: any employee/database question that can be answered using DATABASE SCHEMA.
- DOMAIN: an unrelated request OR asking for an employee field not available in DATABASE SCHEMA.
- "Manager" or "منیجر" is a designation stored in DESIGNATIONS.DESG_NAME. Always classify manager questions as SQL.

OUTPUT EXACTLY 3 LINES:
TYPE:<CHAT|SQL|DOMAIN>
LANG:<EN|UR>
CONTENT:<SQL or reply>

SQL RULES:
1. Generate only ONE SELECT statement. Do not use SELECT *.
2. Only use tables/columns from DATABASE SCHEMA.
3. Never generate INSERT, UPDATE, DELETE, DROP, ALTER, TRUNCATE, CREATE, GRANT, REVOKE, MERGE, EXECUTE, CALL, UNION, comments, or multiple statements.
4. Join syntax: Both ANSI JOINs (FROM EMPLOYEES e JOIN DEPARTMENTS d ON e.DEPT_CODE = d.DEPT_CODE) and comma joins (FROM EMPLOYEES e, DEPARTMENTS d WHERE e.DEPT_CODE = d.DEPT_CODE) are supported. Join keys: e.DEPT_CODE = d.DEPT_CODE and e.DESG_CODE = g.DESG_CODE. Never use CROSS JOIN.
5. ECODE is NUMBER. Example: e.ECODE = 1001.
6. Columns: DEPARTMENTS.DEPT_NAME, DESIGNATIONS.DESG_NAME, EMPLOYEES.SALARY, EMPLOYEES.PHONE_NUMBER, EMPLOYEES.EMAIL, EMPLOYEES.JOINING_DATE, EMPLOYEES.GENDER, EMPLOYEES.CITY, EMPLOYEES.ADDRESS.
7. Email filtering: Always case-insensitive: UPPER(e.EMAIL) = UPPER('email@address.com').
8. Joining date: Use EXTRACT(YEAR FROM e.JOINING_DATE) or TO_DATE('YYYY-MM-DD', 'YYYY-MM-DD') or ANSI date literal DATE 'YYYY-MM-DD'.
9. Gender: Values in database are strictly 'MALE' or 'FEMALE'. Never use 'M' or 'F'. Filter as UPPER(e.GENDER) = 'MALE' or UPPER(e.GENDER) = 'FEMALE'.
10. City: Always use UPPER(e.CITY) = 'CITYNAME'.
11. If the requested field DOES NOT exist in DATABASE SCHEMA:
    - classify as DOMAIN, and CONTENT explains that this specific information is not available (e.g. "CNIC ki information employee data mein available nahi hai.").
12. If completely unrelated to the employee database: classify as DOMAIN with CONTENT: OUT_OF_SCOPE.
13. If asked to change, add, remove, or update employee data: classify as DOMAIN, explaining politely that records cannot be changed.
14. For department-manager requests, filter both department and manager designation: UPPER(d.DEPT_NAME) = 'IT' AND UPPER(g.DESG_NAME) = 'IT MANAGER'.
15. Alias filtered counts as TOTAL_<NAME>_EMPLOYEES (e.g. TOTAL_IT_EMPLOYEES).
16. For aggregate breakdown by category (e.g. male/female count): use GROUP BY:
    SELECT UPPER(e.GENDER), COUNT(*) FROM EMPLOYEES e GROUP BY UPPER(e.GENDER)
    Do NOT use SUM(CASE WHEN ...).
17. Distinct designations per department:
    SELECT d.DEPT_NAME, COUNT(DISTINCT g.DESG_NAME) FROM EMPLOYEES e, DEPARTMENTS d, DESIGNATIONS g WHERE e.DEPT_CODE = d.DEPT_CODE AND e.DESG_CODE = g.DESG_CODE GROUP BY d.DEPT_NAME
18. Ranking in Oracle 26ai:
    - Top/bottom N: ORDER BY <col> [ASC|DESC] FETCH FIRST N ROWS ONLY
    - Department with largest employees or designation with highest avg salary (with ties): FETCH FIRST 1 ROWS WITH TIES
      SELECT d.DEPT_NAME FROM EMPLOYEES e, DEPARTMENTS d WHERE e.DEPT_CODE = d.DEPT_CODE GROUP BY d.DEPT_NAME ORDER BY COUNT(*) DESC FETCH FIRST 1 ROWS WITH TIES
    - Highest earner within a department:
      SELECT e.EMP_NAME, e.SALARY FROM EMPLOYEES e, DEPARTMENTS d WHERE e.DEPT_CODE = d.DEPT_CODE AND UPPER(d.DEPT_NAME) = 'ACCOUNTS' AND e.SALARY = (SELECT MAX(x.SALARY) FROM EMPLOYEES x, DEPARTMENTS y WHERE x.DEPT_CODE = y.DEPT_CODE AND UPPER(y.DEPT_NAME) = 'ACCOUNTS')
19. RAW SQL only in CONTENT when TYPE=SQL. No markdown quotes and no explanations.

Examples:

User: employee 1001 ka department kya hai
TYPE:SQL
LANG:UR
CONTENT:SELECT e.ECODE, e.EMP_NAME, d.DEPT_NAME AS DEPARTMENT_NAME FROM EMPLOYEES e, DEPARTMENTS d WHERE e.DEPT_CODE = d.DEPT_CODE AND e.ECODE = 1001

User: IT manager ka phone number share krdo
TYPE:SQL
LANG:UR
CONTENT:SELECT e.ECODE, e.EMP_NAME, d.DEPT_NAME AS DEPARTMENT_NAME, g.DESG_NAME AS DESIGNATION_NAME, e.PHONE_NUMBER FROM EMPLOYEES e, DEPARTMENTS d, DESIGNATIONS g WHERE e.DEPT_CODE = d.DEPT_CODE AND e.DESG_CODE = g.DESG_CODE AND UPPER(d.DEPT_NAME) = 'IT' AND UPPER(g.DESG_NAME) = 'IT MANAGER'

User: Which employee has the email address asad.ali1026@researchmail.com?
TYPE:SQL
LANG:EN
CONTENT:SELECT e.ECODE, e.EMP_NAME FROM EMPLOYEES e WHERE UPPER(e.EMAIL) = UPPER('asad.ali1026@researchmail.com')

User: Jin employees ka designation Accountant hai un ke naam batayein
TYPE:SQL
LANG:UR
CONTENT:SELECT e.EMP_NAME FROM EMPLOYEES e, DESIGNATIONS g WHERE e.DESG_CODE = g.DESG_CODE AND UPPER(g.DESG_NAME) = 'ACCOUNTANT'

User: Accounts department mein jo log Accountant hain un ke naam aur salary dikhayein
TYPE:SQL
LANG:UR
CONTENT:SELECT e.EMP_NAME, e.SALARY FROM EMPLOYEES e, DEPARTMENTS d, DESIGNATIONS g WHERE e.DEPT_CODE = d.DEPT_CODE AND e.DESG_CODE = g.DESG_CODE AND UPPER(d.DEPT_NAME) = 'ACCOUNTS' AND UPPER(g.DESG_NAME) = 'ACCOUNTANT'

User: What is the combined salary of everyone in the Accounts department?
TYPE:SQL
LANG:EN
CONTENT:SELECT SUM(e.SALARY) FROM EMPLOYEES e, DEPARTMENTS d WHERE e.DEPT_CODE = d.DEPT_CODE AND UPPER(d.DEPT_NAME) = 'ACCOUNTS'

User: Officer ke uhday par kitni khawateen mulazmeen hain?
TYPE:SQL
LANG:UR
CONTENT:SELECT COUNT(*) FROM EMPLOYEES e, DESIGNATIONS g WHERE e.DESG_CODE = g.DESG_CODE AND UPPER(e.GENDER) = 'FEMALE' AND UPPER(g.DESG_NAME) = 'OFFICER'

User: Who are the three employees with the lowest salaries?
TYPE:SQL
LANG:EN
CONTENT:SELECT e.EMP_NAME, e.SALARY FROM EMPLOYEES e ORDER BY e.SALARY ASC FETCH FIRST 3 ROWS ONLY

User: Sab se zyada employees kis department mein hain?
TYPE:SQL
LANG:UR
CONTENT:SELECT d.DEPT_NAME FROM EMPLOYEES e, DEPARTMENTS d WHERE e.DEPT_CODE = d.DEPT_CODE GROUP BY d.DEPT_NAME ORDER BY COUNT(*) DESC FETCH FIRST 1 ROWS WITH TIES

User: Har department mein kitni mukhtalif designations hain?
TYPE:SQL
LANG:UR
CONTENT:SELECT d.DEPT_NAME, COUNT(DISTINCT g.DESG_NAME) FROM EMPLOYEES e, DEPARTMENTS d, DESIGNATIONS g WHERE e.DEPT_CODE = d.DEPT_CODE AND e.DESG_CODE = g.DESG_CODE GROUP BY d.DEPT_NAME

User: Mard aur khawateen mulazmeen ki tadaad alag alag batayein
TYPE:SQL
LANG:UR
CONTENT:SELECT UPPER(e.GENDER), COUNT(*) FROM EMPLOYEES e GROUP BY UPPER(e.GENDER)

User: employee ka CNIC number batao
TYPE:DOMAIN
LANG:UR
CONTENT:CNIC ki information employee data mein available nahi hai.

User: weather batao
TYPE:DOMAIN
LANG:UR
CONTENT:OUT_OF_SCOPE

User: hello, kaise ho
TYPE:CHAT
LANG:UR
CONTENT:Assalam o Alaikum! Main bilkul theek hoon 😊 Aap sunayein, kya haal hain aapke?
""".strip()

    messages = [{"role": "system", "content": system_prompt}]

    for item in (history or [])[-8:]:
        role = "assistant" if item.get("sender") == "bot" else "user"
        messages.append({"role": role, "content": item.get("text", "")})

    messages.append({"role": "user", "content": message})

    response = call_groq_chat_with_fallback(
        messages=messages,
        temperature=0.3,
        max_tokens=400
    )

    text = response.choices[0].message.content.strip()

    result = {
        "type": "CHAT",
        "lang": "EN",
        "content": "Sorry, I didn't get that."
    }

    lines = [l.strip() for l in text.splitlines() if l.strip()]
    content_lines = []
    in_content = False

    for line in lines:
        if line.upper().startswith("TYPE:") and not in_content:
            result["type"] = line.split(":", 1)[1].strip().upper()
        elif line.upper().startswith("LANG:") and not in_content:
            result["lang"] = line.split(":", 1)[1].strip().upper()
        elif line.upper().startswith("CONTENT:") and not in_content:
            in_content = True
            c_part = line.split(":", 1)[1].strip()
            if c_part:
                content_lines.append(c_part)
        elif in_content:
            if not line.startswith("```"):
                content_lines.append(line)

    if content_lines:
        result["content"] = " ".join(content_lines).strip()
    elif text.strip().upper().startswith("SELECT"):
        result["type"] = "SQL"
        result["content"] = text.strip()

    return result


def validate_sql(sql):
    """
    AI ko DB ka direct control nahi diya.
    Python SQL validate karke sirf safe SELECT chalata hai.
    """

    sql_upper = sql.upper().strip()

    if not sql_upper.startswith("SELECT"):
        return False, "Only SELECT queries are allowed."

    forbidden = [
        "INSERT", "UPDATE", "DELETE", "DROP", "ALTER", "TRUNCATE",
        "CREATE", "GRANT", "REVOKE", "MERGE", "CALL", "EXEC",
        "EXECUTE", "UNION", "INTO", "--", "/*", ";"
    ]

    for word in forbidden:
        if word in sql_upper:
            return False, f"Blocked: '{word.strip()}' is not allowed."

    # Strip single-quoted string literals so email addresses, domain names, or text with dots aren't treated as schema tables
    clean_for_check = re.sub(r"'[^']*'", "''", sql_upper)

    # Subquery ka FROM bhi allowed hona chahiye; simple whitelist safety check.
    table_candidates = re.findall(r"\b(EMPLOYEES|DEPARTMENTS|DESIGNATIONS)\b", clean_for_check)

    if not table_candidates:
        return False, "No allowed table found."

    # Disallow known Oracle dictionary / system schemas
    blocked_schemas = {"SYS.", "SYSTEM.", "CTXSYS.", "MDSYS.", "XDB.", "WMSYS.", "OUTLN.", "AUDSYS.", "ALL_TABLES", "USER_TABLES", "DBA_TABLES", "ALL_TAB_COLUMNS", "V$", "GV$"}
    for bs in blocked_schemas:
        if bs in clean_for_check:
            return False, f"Blocked schema/view: '{bs}'"

    # Schema/table qualifier check: allow standard table names and common aliases
    schema_tables = re.findall(r"\b([A-Z_][A-Z0-9_]*)\.([A-Z_][A-Z0-9_]*)\b", clean_for_check)
    allowed_column_qualifiers = {"E", "D", "G", "X", "Y", "A", "B", "C", "S", "T", "SUB", "EMP", "DEPT", "DESG", "EMPLOYEES", "DEPARTMENTS", "DESIGNATIONS"}

    for prefix, col in schema_tables:
        if prefix not in allowed_column_qualifiers:
            return False, f"Schema/table reference '{prefix}.{col}' is not allowed."

    if "SELECT *" in clean_for_check:
        return False, "SELECT * is not allowed."

    return True, "OK"


def run_query(sql):
    """Validated SELECT chalata hai aur UI limit se zyada rows load nahi karta."""
    connection = get_connection()
    cursor = connection.cursor()

    try:
        cursor.execute(sql)
        columns = [col[0] for col in cursor.description]
        rows = cursor.fetchmany(MAX_DISPLAY_ROWS + 1)
        has_more_rows = len(rows) > MAX_DISPLAY_ROWS
        return columns, rows[:MAX_DISPLAY_ROWS], has_more_rows
    finally:
        cursor.close()
        connection.close()


DOMAIN_MESSAGE_VARIANTS = {
    "EN": [
        "That's outside my domain — I can only provide employee data.",
        "I'm not able to help with that; I only handle employee information.",
        "That's beyond what I do here — I'm limited to employee data.",
    ],
    "UR": [
        "Ye meri domain se bahar hai, main sirf employee data provide kar sakta hoon.",
        "Is mein meri madad nahi ho sakti, main sirf employee information handle karta hoon.",
        "Ye mera scope nahi hai, main sirf employee data tak mehdood hoon.",
    ],
}

NO_RESULT_MESSAGE_VARIANTS = {
    "EN": [
        "No matching record was found for that.",
        "I couldn't find anything matching that in the employee data.",
        "That didn't return any results — could you double-check the details and try again?",
    ],
    "UR": [
        "Is se milta koi record nahi mila.",
        "Employee data mein iske mutabiq kuch nahi mila.",
        "Koi result nahi mila — meherbani karke detail check kar ke dobara try karein.",
    ],
}

TOO_MANY_RESULTS_MESSAGE_VARIANTS = {
    "EN": [
        "This would return a large number of records, which isn't practical to show here. Could you narrow it down by department, city, designation, or employee code?",
        "That's a big result set for a chat view — I can't provide that much data at once. Please specify a department, city, designation, or employee code so I can give you a focused answer.",
        "I can look that up, but the list would be too long to display clearly here. Narrowing it down by department, city, designation, or employee code would help.",
    ],
    "UR": [
        "Is sawal ka result kaafi bara hoga jo yahan saaf tareeqe se dikhana mushkil hai. Meherbani karke department, city, designation ya employee code bata kar sawal thora specific kar dein.",
        "Itni zyada records ek sath dikhana mumkin nahi, main itna zyada data ek sath provide nahi kar sakta. Aap relevant department, city, designation ya employee code bata dein taake sahi jawab mil sake.",
        "Ye list bohat lambi ban jayegi is liye chat mein dikhana theek nahi hoga. Please department, city, designation ya employee code specify kar dein.",
    ],
}


def DOMAIN_MESSAGE_(lang):
    return random.choice(DOMAIN_MESSAGE_VARIANTS.get(lang, DOMAIN_MESSAGE_VARIANTS["EN"]))


def NO_RESULT_MESSAGE_(lang):
    return random.choice(NO_RESULT_MESSAGE_VARIANTS.get(lang, NO_RESULT_MESSAGE_VARIANTS["EN"]))


def TOO_MANY_RESULTS_MESSAGE_(lang):
    return random.choice(TOO_MANY_RESULTS_MESSAGE_VARIANTS.get(lang, TOO_MANY_RESULTS_MESSAGE_VARIANTS["EN"]))


def handle_message(message, history=None, preferred_language=None, return_sql=False):
    message = (message or "").strip()
    message = _normalize_stt_artifacts(message)

    if not message:
        res = "Please type something first."
        return (res, "") if return_sql else res

    early_lang = preferred_language if preferred_language in ("EN", "UR") else _detect_typed_language(message) or "EN"

    if _is_oversized_list_request(message):
        res = TOO_MANY_RESULTS_MESSAGE_(early_lang)
        return (res, "") if return_sql else res

    # ---------------------------------------------------------------------------
    # Priority 1: Detect salary-aggregate intents (SUM, AVG, MIN, MAX) BEFORE
    # any count shortcut. These must always go to the AI for proper SQL generation.
    # ---------------------------------------------------------------------------
    if _is_salary_aggregate_intent(message):
        # Skip count shortcut entirely; let AI generate the correct aggregate SQL.
        pass
    else:
        # Priority 2: Simple count shortcut (only for genuine count-of-employees queries)
        count_sql = None
        count_regex = r"\b(how many|total|count|headcount|kitne|kitni|tadaad|\u06a9\u062a\u0646\u06d2|\u06a9\u062a\u0646\u06cc|\u062a\u0639\u062f\u0627\u062f)\b"
        if re.search(count_regex, message.lower()):
            try:
                department_names, designation_names = _reference_names()
                count_sql = _count_query_for(message, department_names, designation_names)
            except Exception as exc:
                logger.error(f"Database count query error: {exc}")
                res = "Database connection is unavailable."
                return (res, "") if return_sql else res
        if count_sql:
            try:
                columns, rows, _ = run_query(count_sql)
            except Exception as exc:
                logger.error(f"Count query execution error: {exc}")
                res = "Database connection is unavailable."
                return (res, "") if return_sql else res
            answer = format_answer(columns, rows, lang=early_lang, question=message)
            res = answer or NO_RESULT_MESSAGE_(early_lang)
            return (res, count_sql) if return_sql else res

    try:
        result = classify_and_respond(message, history=history, preferred_language=preferred_language)
    except Exception as exc:
        logger.error(f"AI classification error: {exc}")
        if "AI service is" in str(exc):
            res = str(exc)
        else:
            res = "AI service is temporarily busy. Please try again shortly."
        return (res, "") if return_sql else res

    msg_type = result["type"]
    lang = preferred_language if preferred_language in ("EN", "UR") else result["lang"] if result["lang"] in ("EN", "UR") else "EN"
    content = result["content"].strip()

    if lang == "UR" and result["type"] != "SQL":
        content = _sanitize_roman_urdu(content)

    if msg_type == "CHAT":
        return (content, "") if return_sql else content

    if msg_type == "DOMAIN":
        if _is_data_change_request(message):
            if lang == "UR":
                res = "Maazrat, main employee records mein tabdeeli nahi kar sakta. Main maujooda maloomat dekh kar bata sakta hoon."
            else:
                res = "Sorry, I can't change employee records, but I can help you view the information that's already there."
            return (res, "") if return_sql else res
        if content.strip().upper() == "OUT_OF_SCOPE":
            res = DOMAIN_MESSAGE_(lang)
            return (res, "") if return_sql else res
        if content and content.upper() != "NONE":
            return (content, "") if return_sql else content
        res = DOMAIN_MESSAGE_(lang)
        return (res, "") if return_sql else res

    if content.endswith(";"):
        content = content[:-1].strip()

    is_valid, validation_message = validate_sql(content)

    if not is_valid:
        if lang == "UR":
            res = "Maazrat, main is darkhwast par amal nahi kar sakta. Aap employee ki maujooda maloomat pooch sakte hain."
        else:
            res = "Sorry, I can't carry out that request. You can ask me about existing employee information."
        return (res, "") if return_sql else res

    try:
        columns, rows, has_more_rows = run_query(content)
    except Exception as exc:
        logger.error(f"Run query execution error: {exc}")
        res = "Database connection is unavailable."
        return (res, content) if return_sql else res

    if has_more_rows:
        res = TOO_MANY_RESULTS_MESSAGE_(lang)
        return (res, content) if return_sql else res

    answer = format_answer(columns, rows, lang=lang, question=message)

    if not answer:
        res = NO_RESULT_MESSAGE_(lang)
        return (res, content) if return_sql else res

    return (answer, content) if return_sql else answer


# ============================================================================
# FastAPI Routes
# ============================================================================

import logging
logger = logging.getLogger("research_agent")


@app.get("/", response_class=HTMLResponse)
async def home(request: Request):
    auth_redirect = _check_session_auth(request, is_api=False)
    if auth_redirect:
        return auth_redirect

    if "chat_history" not in request.session:
        request.session["chat_history"] = [
            {
                "sender": "bot",
                "text": "Hello! Ask me anything about employee data 😊"
            }
        ]

    return templates.TemplateResponse(
        request=request,
        name="chat.html",
        context={
            "history": request.session.get("chat_history", []),
            "db_username": request.session.get("db_username"),
            "session_timeout_seconds": SESSION_INACTIVITY_TIMEOUT,
        }
    )


@app.get("/login", response_class=HTMLResponse)
async def login_get(request: Request):
    # If already logged in and active, redirect to home
    logged_in = request.session.get("logged_in")
    login_token = request.session.get("db_login_token")
    last_activity = request.session.get("last_activity")
    import time
    now = time.time()
    if logged_in and login_token and login_token in ACTIVE_DB_LOGINS:
        if last_activity is not None and (now - float(last_activity)) < SESSION_INACTIVITY_TIMEOUT:
            return RedirectResponse(url="/", status_code=302)

    error = None
    error_type = None
    reason = request.query_params.get("reason")
    if reason in ("inactive", "expired"):
        error = "Your session has expired due to inactivity. Please log in again."
        error_type = "session_expired"

    return templates.TemplateResponse(
        request=request,
        name="login.html",
        context={
            "error": error,
            "error_type": error_type,
        }
    )


@app.post("/login", response_class=HTMLResponse)
async def login_post(
    request: Request,
    username: str = Form(""),
    password: str = Form(""),
):
    error = None
    error_type = None

    username = username.strip().upper()
    password = password.strip().upper()

    try:
        dsn = oracledb.makedsn(
            DB_HOST,
            DB_PORT,
            service_name=DB_SERVICE
        )

        test_connection = oracledb.connect(
            user=username,
            password=password,
            dsn=dsn
        )

        test_cursor = test_connection.cursor()
        test_cursor.execute("SELECT USER FROM DUAL")
        logged_db_user = test_cursor.fetchone()[0]
        test_cursor.close()
        test_connection.close()

        import time
        login_token = secrets.token_urlsafe(32)
        ACTIVE_DB_LOGINS[login_token] = {
            "username": logged_db_user,
            "password": password
        }

        _clear_session_data(request)
        request.session["logged_in"] = True
        request.session["db_username"] = logged_db_user
        request.session["db_login_token"] = login_token
        request.session["last_activity"] = time.time()
        request.session["chat_history"] = [
            {
                "sender": "bot",
                "text": "Hello! Ask me anything about employee data 😊"
            }
        ]

        return RedirectResponse(url="/", status_code=302)

    except oracledb.DatabaseError as exc:
        error_obj = exc.args[0] if exc.args else None
        error_code = getattr(error_obj, "code", None)
        error_message = getattr(error_obj, "message", str(exc))

        if error_code == 1017:
            error_type = "invalid_credentials"
            error = "Invalid Credentials"
        else:
            error_type = "generic"
            error = "Oracle database connection could not be established."
    except Exception as exc:
        error_type = "generic"
        error = "Database connection error."

    return templates.TemplateResponse(
        request=request,
        name="login.html",
        context={
            "error": error,
            "error_type": error_type,
        }
    )


@app.get("/logout")
@app.post("/logout")
async def logout(request: Request):
    """
    Sign out = research database login session fully disconnect.
    Clears active login token + in-memory credentials + session data.
    """
    _clear_session_data(request)
    reason = request.query_params.get("reason")

    accept = request.headers.get("accept", "")
    if "application/json" in accept:
        return JSONResponse({"success": True, "message": "Logged out successfully"})

    redirect_url = f"/login?reason={reason}" if reason else "/login"
    return RedirectResponse(url=redirect_url, status_code=302)


@app.post("/api/transcribe")
async def transcribe_voice(request: Request, audio: UploadFile = File(None)):
    """English, Urdu, or mixed voice ko automatically text mein transcribe karta hai."""
    auth_check = _check_session_auth(request, is_api=True)
    if auth_check:
        return auth_check

    if not get_configured_groq_keys():
        return JSONResponse({"error": "Voice service is not configured."}, status_code=503)

    if audio is None:
        return JSONResponse({"error": "No voice recording was received."}, status_code=400)

    audio_bytes = await audio.read(10 * 1024 * 1024 + 1)
    if not audio_bytes:
        return JSONResponse({"error": "The voice recording is empty."}, status_code=400)
    if len(audio_bytes) > 10 * 1024 * 1024:
        return JSONResponse({"error": "Voice recording is too large."}, status_code=413)

    try:
        audio_file_tuple = (audio.filename or "voice.webm", audio_bytes, audio.content_type or "audio/webm")
        prompt = (
            "An employee database question spoken naturally in English, Urdu, "
            "Roman Urdu, or mixed Urdu-English. Preserve the speaker's words and language."
        )
        transcription = call_groq_transcribe_with_fallback(
            audio_file_tuple=audio_file_tuple,
            prompt=prompt
        )
        transcript = (getattr(transcription, "text", "") or "").strip()
        if not transcript:
            return JSONResponse({"error": "No speech was detected. Please try again."}, status_code=422)
        return JSONResponse({"transcript": transcript})
    except Exception as exc:
        logger.error(f"Voice transcription error: {exc}")
        if "Voice service is" in str(exc):
            return JSONResponse({"error": str(exc)}, status_code=503)
        return JSONResponse({"error": "Voice service is temporarily busy. Please try again shortly."}, status_code=502)


@app.post("/api/heartbeat")
@app.get("/api/heartbeat")
async def api_heartbeat(request: Request):
    """
    Keepalive endpoint called while user is actively interacting with the UI
    (mouse movements, clicks, typing, scrolling, voice recording, etc.).
    Resets the server-side inactivity session timer.
    """
    auth_check = _check_session_auth(request, is_api=True)
    if auth_check:
        return auth_check
    return JSONResponse({"success": True, "active": True})


@app.post("/api/chat")
async def api_chat(request: Request):
    auth_check = _check_session_auth(request, is_api=True)
    if auth_check:
        return auth_check

    try:
        data = await request.json()
    except Exception:
        data = {}
    message = data.get("message", "")
    message_source = str(data.get("source", "typed")).lower()
    preferred_language = str(data.get("language", "")).upper()
    if preferred_language not in ("UR", "EN"):
        preferred_language = None
    if message_source == "typed" and preferred_language is None:
        preferred_language = _detect_typed_language(message)

    evaluation_mode = bool(data.get("evaluation_mode", False))

    history = request.session.get("chat_history", [])

    token = _current_session_ref.set(dict(request.session))
    generated_sql = ""
    try:
        if evaluation_mode:
            reply, generated_sql = handle_message(
                message, history=history, preferred_language=preferred_language, return_sql=True
            )
        else:
            reply = handle_message(
                message, history=history, preferred_language=preferred_language, return_sql=False
            )
    except Exception as exc:
        logger.error(f"Error handling message: {exc}")
        reply = "Meherbani karke dobara koshish karein ya apna sawal thoda mukhtalif alfaaz mein poochein."
        generated_sql = ""
    finally:
        _current_session_ref.reset(token)

    history.append({"sender": "user", "text": message})
    history.append({"sender": "bot", "text": reply})
    request.session["chat_history"] = history[-16:]

    response_data = {"reply": reply}
    if evaluation_mode:
        response_data["sql"] = generated_sql or ""
    return JSONResponse(response_data)


# ============================================================================
# Entry Point
# ============================================================================

if __name__ == "__main__":
    import uvicorn
    print(f"Oracle target: {DB_HOST}:{DB_PORT}/{DB_SERVICE}")
    print("Research Agent is starting...")
    uvicorn.run("app:app", host="127.0.0.1", port=5000, reload=False)
