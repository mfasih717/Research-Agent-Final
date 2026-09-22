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

from flask import Flask, render_template, request, redirect, url_for, session, jsonify
import oracledb
import os
import re
import random
import secrets
from decimal import Decimal
from dotenv import load_dotenv
from openai import OpenAI
import schema as db_schema
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / '.env')

app = Flask(__name__)
app.config["TEMPLATES_AUTO_RELOAD"] = True
app.secret_key = os.environ.get("FLASK_SECRET_KEY", "change_this_secret_key_later")

# ---- Local Oracle 26ai Free Database ----
# Thin mode use ho raha hai, is liye old Oracle client / Toad setup touch nahi hota.
DB_HOST = "localhost"
DB_PORT = 1521
DB_SERVICE = "FREEPDB1"

# Login credentials browser/session cookie mein password ke taur par store nahi hote.
# Successful Oracle login ke baad credentials sirf running Python process ki memory mein rehte hain.
ACTIVE_DB_LOGINS = {}

GROQ_API_KEY = os.environ.get("GROQ_API_KEY", "").strip()
GROQ_MODEL = os.environ.get("GROQ_MODEL", "openai/gpt-oss-120b")
GROQ_STT_MODEL = os.environ.get("GROQ_STT_MODEL", "whisper-large-v3-turbo")

groq_client = (
    OpenAI(
        api_key=GROQ_API_KEY,
        base_url="https://api.groq.com/openai/v1"
    )
    if GROQ_API_KEY
    else None
)

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


def _count_query_for(message, department_names=None, designation_names=None):
    """Count request mein entity aur filter ko alag pehchanta hai."""
    text = str(message or "").strip().lower()
    if not any(x in text for x in ("how many", "total", "count", "kitne", "kitni", "tadaad", "\u06a9\u062a\u0646\u06d2", "\u06a9\u062a\u0646\u06cc", "\u062a\u0639\u062f\u0627\u062f")):
        return None

    employees = ("employee", "employees", "staff", "banda", "bande", "banday", "log", "afrad", "\u0645\u0644\u0627\u0632\u0645", "\u0645\u0644\u0627\u0632\u0645\u06cc\u0646", "\u0628\u0646\u062f\u06d2", "\u0644\u0648\u06af", "\u0627\u06cc\u0645\u067e\u0644\u0627\u0626\u06cc\u0632")
    departments = ("department", "departments", "dept", "\u0688\u06cc\u067e\u0627\u0631\u0679\u0645\u0646\u0679", "\u0634\u0639\u0628\u06d2")
    designations = ("designation", "designations", "desig", "job title", "\u0688\u06cc\u0632\u06af\u0646\u06cc\u0634\u0646", "\u0639\u06c1\u062f\u06d2")

    # Employees target hon to department/designation sirf filter hota hai.
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
        # Koi named department/designation match nahi mila, lekin message
        # generic total se zyada lag raha hai (jaise Urdu script mein koi
        # department ka naam). Blind total return karne ke bajaye None dete
        # hain taake handle_message() ye AI (classify_and_respond) ko de,
        # jise actual DB department/designation names diye jaate hain.
        return None

    # Entity khud departments/designations ho to unhi records ko count karo.
    if any(x in text for x in departments):
        return "SELECT COUNT(*) AS TOTAL_DEPARTMENTS FROM DEPARTMENTS"
    if any(x in text for x in designations):
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


def get_connection():
    """
    Current logged-in Oracle user ke credentials se connection banata hai.
    AI ko credentials nahi milte; connection hamesha Python banata hai.
    """
    login_token = session.get("db_login_token")
    credentials = ACTIVE_DB_LOGINS.get(login_token)

    if not credentials:
        raise RuntimeError("Database login session available nahi hai. Please dobara login karein.")

    dsn = oracledb.makedsn(DB_HOST, DB_PORT, service_name=DB_SERVICE)

    return oracledb.connect(
        user=credentials["username"],
        password=credentials["password"],
        dsn=dsn
    )


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

    if groq_client is None:
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
You are a friendly Employee Research Assistant and Oracle SQL generator.

DATABASE SCHEMA:
{schema_text}

RELATIONS:
{relations_text}

REFERENCE DATA (exact spelling as stored in the database right now):
{reference_names_text}
- When the user names a department or designation in English, Urdu script, or Roman Urdu, match it to the closest entry above and use that EXACT spelling (case-insensitive) in UPPER(...) filters. Never invent, translate loosely, or guess a name that is not in this list.
- If nothing in this list reasonably matches what the user asked for, classify as DOMAIN and say plainly that this department/designation was not found, instead of guessing SQL for it.

LANGUAGE:
SELECTED LANGUAGE MODE:
{language_mode}

- LANG=EN for English.
- LANG=UR for spoken/transcribed Urdu script or Roman Urdu.
- Understand Urdu script input, but always write the reply in Roman Urdu.
- Never use Urdu script in the reply. Roman Urdu only.
- Roman Urdu must use natural Pakistani Urdu wording. Never use Hindi vocabulary.
- Strictly avoid Hindi words such as "kripya", "dhanyavaad", "avashya", "sahayata", "vivaran", and "karmchari".
- Use Pakistani alternatives such as "meherbani", "shukriya", "zaroor", "madad", "tafseel", and "employee".
- For Roman Urdu, write concise and respectful Pakistani conversational Urdu, for example:
  "Meherbani karke employee ka ECODE ya naam batayein."
- Mixed Roman Urdu and English input is LANG=UR by default.
- If the input is neither Urdu nor Roman Urdu, use LANG=EN and reply in clear, simple professional English.
- For casual greetings or small talk, reply warmly and naturally in the user's language.
- During the first 2 or 3 consecutive casual exchanges, do NOT mention employee data or your capabilities; simply continue the conversation naturally.
- Only around the 3rd or 4th consecutive casual user message, add one short reminder in simple words that you can also help with employee information.
- Do not repeat that reminder again unless several more casual turns have passed.
- You may occasionally use one suitable friendly emoji in casual conversation, but do not use an emoji in every reply and never use more than one.
- Keep casual replies concise; do not list every capability unless the user asks.
- If the user explicitly asks for an English answer (for example "English mein jawab do"), use LANG=EN.
- If "English mein jawab do" is a follow-up to the previous answer, classify it as CHAT and
  return that previous answer translated into natural English without querying the database again.

CLASSIFY INTO:
- CHAT: casual conversation.
- SQL: any employee/database question that can be answered using the available DATABASE SCHEMA.
- DOMAIN: an unrelated request OR an employee-data request asking for information that is not available in DATABASE SCHEMA.
- "Manager" or "منیجر" is a designation stored in DESIGNATIONS.DESG_NAME. Always classify manager questions as SQL, never DOMAIN.

OUTPUT EXACTLY 3 LINES:
TYPE:<CHAT|SQL|DOMAIN>
LANG:<EN|UR>
CONTENT:<SQL or reply>

SQL RULES:
1. Generate only ONE SELECT statement.
2. Only use tables/columns from DATABASE SCHEMA.
3. Never generate INSERT, UPDATE, DELETE, DROP, ALTER, TRUNCATE, CREATE,
   GRANT, REVOKE, MERGE, EXECUTE, CALL, UNION, comments, or multiple statements.
4. Do not use SELECT *.
5. Use old-style Oracle table relation syntax:
   FROM EMPLOYEES e, DEPARTMENTS d
   WHERE e.DEPT_CODE = d.DEPT_CODE
   Do NOT use JOIN / LEFT JOIN / RIGHT JOIN / INNER JOIN keywords.
6. ECODE is NUMBER. Example: e.ECODE = 1001. Do not put employee code in quotes.
7. For department name, use DEPARTMENTS.DEPT_NAME.
8. For designation name, use DESIGNATIONS.DESG_NAME.
9. For employee salary, use EMPLOYEES.SALARY.
10. For phone number, use EMPLOYEES.PHONE_NUMBER.
11. For email, use EMPLOYEES.EMAIL.
12. For joining date, use EMPLOYEES.JOINING_DATE.
13. For gender, use EMPLOYEES.GENDER.
14. For city, use EMPLOYEES.CITY.
15. For address, use EMPLOYEES.ADDRESS.
16. If the user asks for information that exists anywhere in DATABASE SCHEMA, classify it as SQL.
17. If the user asks for an employee field/information that DOES NOT exist in DATABASE SCHEMA:
    - classify it as DOMAIN
    - CONTENT must directly explain that this specific information is not available in the employee data.
    - Do not list all available columns.
    - Roman Urdu example: "CNIC ki information employee data mein available nahi hai."
    - English example: "CNIC information is not available in the employee data."
18. If the request is completely unrelated to the employee database:
    - classify it as DOMAIN
    - CONTENT must be exactly the single word: OUT_OF_SCOPE
    - Do not write anything else in CONTENT for this case.
19. If the user asks to change, add, remove, delete, or update employee data:
    - classify it as DOMAIN.
    - Never mention SELECT, SQL, queries, commands, validators, permissions, or technical database rules.
    - Explain the limitation in friendly everyday language.
    - Roman Urdu: "Maazrat, main employee records mein tabdeeli nahi kar sakta. Main maujooda maloomat dekh kar bata sakta hoon."
    - English: "Sorry, I can't change employee records, but I can help you view the information that's already there."
20. For general employee detail, use this pattern:
    {full_detail_query}
21. For aggregate questions, do not include ECODE or EMP_NAME unless grouping is actually needed.
22. Alias a plain, unfiltered total count as TOTAL. When the count is filtered by a department or designation, alias it as TOTAL_<NAME>_EMPLOYEES using that exact REFERENCE DATA name (e.g. TOTAL_IT_EMPLOYEES, TOTAL_WEAVING_EMPLOYEES, TOTAL_ACCOUNTS_MANAGER_EMPLOYEES) so the answer can mention what was actually counted.
23. Do not add ROWNUM merely to shorten a large result. The application detects large result sets and asks the user to narrow the request.
24. CONTENT must contain raw SQL only when TYPE=SQL. No markdown and no explanation.
25. Treat every department and designation uniformly; never create special behavior for IT or any other named department. For a department-manager request, filter both the readable department name and its corresponding manager designation. Example: IT department uses UPPER(d.DEPT_NAME) = 'IT' and UPPER(g.DESG_NAME) = 'IT MANAGER'; Accounts uses 'ACCOUNTS' and 'ACCOUNTS MANAGER'. Always select ECODE, EMP_NAME, department name, designation name, plus every field requested by the user.
26. A message may contain two or more related questions. Include every requested available field in the same SELECT so all parts are answered together in one structured response.
27. Never guess, invent, pre-fill, or reuse an answer from an example or earlier result. SQL answers must come only from the current Oracle query result.
28. Answer only what the user asked. Do not add unrelated employee fields. For a person/manager lookup, identity context (EMP_NAME, DEPARTMENT_NAME, DESIGNATION_NAME) is allowed, followed by the specifically requested field(s).
29. Department counts must filter the named department, and designation counts must filter the named designation. Never fall back to the total employee count when a named filter is present.
30. Always resolve a spoken/typed department or designation reference (English, Urdu script, or Roman Urdu) to its exact spelling from REFERENCE DATA before writing an UPPER(...) filter. If REFERENCE DATA has no reasonable match, do not write SQL for it — classify as DOMAIN instead.

Examples:

User: employee 1001 ka department kya hai
TYPE:SQL
LANG:UR
CONTENT:SELECT e.ECODE, e.EMP_NAME, d.DEPT_NAME AS DEPARTMENT_NAME FROM EMPLOYEES e, DEPARTMENTS d WHERE e.DEPT_CODE = d.DEPT_CODE AND e.ECODE = 1001

User: what is employee 1002 designation
TYPE:SQL
LANG:EN
CONTENT:SELECT e.ECODE, e.EMP_NAME, g.DESG_NAME AS DESIGNATION_NAME FROM EMPLOYEES e, DESIGNATIONS g WHERE e.DESG_CODE = g.DESG_CODE AND e.ECODE = 1002

User: highest salary employee
TYPE:SQL
LANG:EN
CONTENT:SELECT ECODE, EMP_NAME, SALARY FROM EMPLOYEES WHERE SALARY = (SELECT MAX(SALARY) FROM EMPLOYEES)

User: IT manager ka phone number share krdo
TYPE:SQL
LANG:UR
CONTENT:SELECT e.ECODE, e.EMP_NAME, d.DEPT_NAME AS DEPARTMENT_NAME, g.DESG_NAME AS DESIGNATION_NAME, e.PHONE_NUMBER FROM EMPLOYEES e, DEPARTMENTS d, DESIGNATIONS g WHERE e.DEPT_CODE = d.DEPT_CODE AND e.DESG_CODE = g.DESG_CODE AND UPPER(d.DEPT_NAME) = 'IT' AND UPPER(g.DESG_NAME) = 'IT MANAGER'

User: IT ka manager kon hai
TYPE:SQL
LANG:UR
CONTENT:SELECT e.ECODE, e.EMP_NAME, d.DEPT_NAME AS DEPARTMENT_NAME, g.DESG_NAME AS DESIGNATION_NAME FROM EMPLOYEES e, DEPARTMENTS d, DESIGNATIONS g WHERE e.DEPT_CODE = d.DEPT_CODE AND e.DESG_CODE = g.DESG_CODE AND UPPER(d.DEPT_NAME) = 'IT' AND UPPER(g.DESG_NAME) = 'IT MANAGER'

User: Faisalabad ke employees dikhao
TYPE:SQL
LANG:UR
CONTENT:SELECT ECODE, EMP_NAME, CITY FROM EMPLOYEES WHERE UPPER(CITY) = 'FAISALABAD'

User: employee 1044 ka email aur address batao
TYPE:SQL
LANG:UR
CONTENT:SELECT ECODE, EMP_NAME, EMAIL, ADDRESS FROM EMPLOYEES WHERE ECODE = 1044

User: IT department me kitne employees hain
TYPE:SQL
LANG:UR
CONTENT:SELECT COUNT(*) AS TOTAL_IT_EMPLOYEES FROM EMPLOYEES e, DEPARTMENTS d WHERE e.DEPT_CODE = d.DEPT_CODE AND UPPER(d.DEPT_NAME) = 'IT'

User: kitne accounts manager hain
TYPE:SQL
LANG:UR
CONTENT:SELECT COUNT(*) AS TOTAL_ACCOUNTS_MANAGER_EMPLOYEES FROM EMPLOYEES e, DESIGNATIONS g WHERE e.DESG_CODE = g.DESG_CODE AND UPPER(g.DESG_NAME) = 'ACCOUNTS MANAGER'

User: hi
TYPE:CHAT
LANG:EN
CONTENT:Hi! How are you doing? 🙂

User: main theek hoon, aap kaise hain
TYPE:CHAT
LANG:UR
CONTENT:Main bhi bilkul theek hoon, shukriya! Aap ka din kaisa ja raha hai?

User: employee ka CNIC number batao
TYPE:DOMAIN
LANG:UR
CONTENT:CNIC ki information employee data mein available nahi hai.

User: what is an employee's blood group
TYPE:DOMAIN
LANG:EN
CONTENT:Blood group information is not available in the employee data.

User: weather batao
TYPE:DOMAIN
LANG:UR
CONTENT:OUT_OF_SCOPE
""".strip()

    messages = [{"role": "system", "content": system_prompt}]

    for item in (history or [])[-8:]:
        role = "assistant" if item.get("sender") == "bot" else "user"
        messages.append({"role": role, "content": item.get("text", "")})

    messages.append({"role": "user", "content": message})

    response = groq_client.chat.completions.create(
        model=GROQ_MODEL,
        messages=messages,
        temperature=0
    )

    text = response.choices[0].message.content.strip()

    result = {
        "type": "CHAT",
        "lang": "EN",
        "content": "Sorry, I didn't get that."
    }

    for line in text.splitlines():
        line = line.strip()

        if line.upper().startswith("TYPE:"):
            result["type"] = line.split(":", 1)[1].strip().upper()

        elif line.upper().startswith("LANG:"):
            result["lang"] = line.split(":", 1)[1].strip().upper()

        elif line.upper().startswith("CONTENT:"):
            result["content"] = line.split(":", 1)[1].strip()

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
        "EXECUTE", "UNION", "INTO", "--", "/*", ";",
        " JOIN ", " LEFT JOIN ", " RIGHT JOIN ", " INNER JOIN ", " FULL JOIN "
    ]

    for word in forbidden:
        if word in sql_upper:
            return False, f"Blocked: '{word.strip()}' is not allowed."

    # FROM ke baad comma-separated tables ko check karo.
    from_match = re.search(
        r"\bFROM\s+(.+?)(?:\bWHERE\b|\bGROUP\s+BY\b|\bORDER\s+BY\b|$)",
        sql_upper,
        flags=re.DOTALL
    )

    if not from_match:
        return False, "No valid FROM clause found."

    from_part = from_match.group(1)

    # Subquery ka FROM bhi allowed hona chahiye; simple whitelist safety check.
    table_candidates = re.findall(r"\b(EMPLOYEES|DEPARTMENTS|DESIGNATIONS)\b", sql_upper)

    if not table_candidates:
        return False, "No allowed table found."

    # Kisi unknown schema-qualified/table name ko reject karo.
    schema_tables = re.findall(r"\b[A-Z_][A-Z0-9_]*\.[A-Z_][A-Z0-9_]*\b", sql_upper)
    allowed_column_qualifiers = {"E", "D", "G"}

    for item in schema_tables:
        prefix = item.split(".")[0]
        if prefix not in allowed_column_qualifiers:
            return False, f"Schema/table reference '{item}' is not allowed."

    if "SELECT *" in sql_upper:
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
        "I can only help with the employee data available in this system. For anything outside that, the relevant department would be the right place to check.",
        "That's outside what I have access to here — I can only assist with the employee records in this database. For other information, please reach out to the concerned department.",
        "I'm only set up to answer questions about the employee data stored here. For details beyond this, the relevant department can help you further.",
    ],
    "UR": [
        "Ye maloomat is system mein available nahi, main sirf employee data se related sawalon mein madad kar sakta hoon. Baaki cheezon ke liye concerned department se rabta karein.",
        "Main sirf yahan maujood employee records ke bare mein bata sakta hoon. Is scope se bahar ki maloomat ke liye relevant department behtar rahega.",
        "Ye sawal mere scope se bahar hai, main sirf employee data handle karta hoon. Baaki detail concerned department se mil sakti hai.",
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


def handle_message(message, history=None, preferred_language=None):
    message = (message or "").strip()
    message = _normalize_stt_artifacts(message)

    if not message:
        return "Please type something first."

    early_lang = preferred_language if preferred_language in ("EN", "UR") else _detect_typed_language(message) or "EN"

    if _is_oversized_list_request(message):
        return TOO_MANY_RESULTS_MESSAGE_(early_lang)

    count_sql = None
    count_markers = ("how many", "total", "count", "kitne", "kitni", "tadaad", "\u06a9\u062a\u0646\u06d2", "\u06a9\u062a\u0646\u06cc", "\u062a\u0639\u062f\u0627\u062f")
    if any(marker in message.lower() for marker in count_markers):
        try:
            department_names, designation_names = _reference_names()
            count_sql = _count_query_for(message, department_names, designation_names)
        except Exception as exc:
            return f"Database error: {str(exc)}"
    if count_sql:
        try:
            columns, rows, _ = run_query(count_sql)
        except Exception as exc:
            return f"Database error: {str(exc)}"
        answer = format_answer(columns, rows, lang=early_lang, question=message)
        return answer or NO_RESULT_MESSAGE_(early_lang)

    try:
        result = classify_and_respond(message, history=history, preferred_language=preferred_language)
    except Exception as exc:
        return f"AI error: {str(exc)}"

    msg_type = result["type"]
    lang = preferred_language if preferred_language in ("EN", "UR") else result["lang"] if result["lang"] in ("EN", "UR") else "EN"
    content = result["content"].strip()

    if lang == "UR" and result["type"] != "SQL":
        content = _sanitize_roman_urdu(content)

    if msg_type == "CHAT":
        return content

    if msg_type == "DOMAIN":
        if _is_data_change_request(message):
            if lang == "UR":
                return "Maazrat, main employee records mein tabdeeli nahi kar sakta. Main maujooda maloomat dekh kar bata sakta hoon."
            return "Sorry, I can't change employee records, but I can help you view the information that's already there."
        if content.strip().upper() == "OUT_OF_SCOPE":
            return DOMAIN_MESSAGE_(lang)
        if content and content.upper() != "NONE":
            return content
        return DOMAIN_MESSAGE_(lang)

    if content.endswith(";"):
        content = content[:-1].strip()

    is_valid, validation_message = validate_sql(content)

    if not is_valid:
        if lang == "UR":
            return "Maazrat, main is darkhwast par amal nahi kar sakta. Aap employee ki maujooda maloomat pooch sakte hain."
        return "Sorry, I can't carry out that request. You can ask me about existing employee information."

    try:
        columns, rows, has_more_rows = run_query(content)
    except Exception as exc:
        return f"Database error: {str(exc)}"

    if has_more_rows:
        return TOO_MANY_RESULTS_MESSAGE_(lang)

    answer = format_answer(columns, rows, lang=lang, question=message)

    if not answer:
        return NO_RESULT_MESSAGE_(lang)

    return answer


@app.route("/", methods=["GET"])
def home():
    if not session.get("logged_in"):
        return redirect(url_for("login"))

    session["chat_history"] = [
        {
            "sender": "bot",
            "text": (
                "Hello! Ask me anything about the available employee data."
            )
        }
    ]

    return render_template(
        "chat.html",
        history=session["chat_history"],
        db_username=session.get("db_username")
    )


@app.route("/login", methods=["GET", "POST"])
def login():
    """
    Login page par diya gaya username/password Python direct Oracle ko verify karta hai.

    Username:
    - strip + upper kiya jata hai, is liye research / Research / RESEARCH same hain.

    Password:
    - Is research app mein password ko bhi uppercase normalize kiya jata hai.
    - Is liye NTU / ntu / Ntu ko NTU ke taur par Oracle ko bheja jata hai.
    - Ye tab sahi hai jab Oracle password uppercase form mein bana ho, jaise NTU.

    AI ko username/password nahi bheja jata.
    """
    error = None
    error_type = None

    if request.method == "POST":
        username = request.form.get("username", "").strip().upper()
        password = request.form.get("password", "").strip().upper()

        try:
            dsn = oracledb.makedsn(
                DB_HOST,
                DB_PORT,
                service_name=DB_SERVICE
            )

            # Python khud Oracle se login verify karta hai.
            # Username aur password dono uppercase normalize kiye gaye hain
            # taa-ke RESEARCH/research aur NTU/ntu same treat hon.
            test_connection = oracledb.connect(
                user=username,
                password=password,
                dsn=dsn
            )

            # Sirf connection open hona nahi, ek chhota DB test bhi karte hain.
            test_cursor = test_connection.cursor()
            test_cursor.execute("SELECT USER FROM DUAL")
            logged_db_user = test_cursor.fetchone()[0]
            test_cursor.close()
            test_connection.close()

            # Password Flask cookie/session mein nahi rakhte.
            # Sirf random token cookie mein jata hai; credentials Python memory mein rehte hain.
            login_token = secrets.token_urlsafe(32)
            ACTIVE_DB_LOGINS[login_token] = {
                "username": logged_db_user,
                "password": password
            }

            session.clear()
            session["logged_in"] = True
            session["db_username"] = logged_db_user
            session["db_login_token"] = login_token

            return redirect(url_for("home"))

        except oracledb.DatabaseError as exc:
            error_obj = exc.args[0] if exc.args else None
            error_code = getattr(error_obj, "code", None)
            error_message = getattr(error_obj, "message", str(exc))

            if error_code == 1017:
                error_type = "invalid_credentials"
                error = "Invalid Credentials"
            else:
                error_type = "generic"
                error = f"Oracle error: {error_message}"
        except Exception as exc:
            error_type = "generic"
            error = f"Connection error: {str(exc)}"

    return render_template(
        "login.html",
        error=error,
        error_type=error_type
    )


@app.route("/logout")
def logout():
    """
    Sign out = research database login session fully disconnect.

    Query connections are already closed after every query in run_query().
    Yahan active login token + in-memory credentials + Flask session/chat
    sab clear kar diye jate hain.
    """
    login_token = session.get("db_login_token")

    if login_token:
        credentials = ACTIVE_DB_LOGINS.pop(login_token, None)
        if credentials is not None:
            # Password reference ko overwrite/remove karne ki best-effort cleanup.
            credentials["password"] = None
            credentials.clear()

    session.clear()
    return redirect(url_for("login"))


@app.route("/api/transcribe", methods=["POST"])
def transcribe_voice():
    """English, Urdu, or mixed voice ko automatically text mein transcribe karta hai."""
    if not session.get("logged_in"):
        return jsonify({"error": "not_logged_in"}), 401

    if groq_client is None:
        return jsonify({"error": "Voice service is not configured."}), 503

    audio_file = request.files.get("audio")
    if audio_file is None:
        return jsonify({"error": "No voice recording was received."}), 400

    audio_bytes = audio_file.stream.read(10 * 1024 * 1024 + 1)
    if not audio_bytes:
        return jsonify({"error": "The voice recording is empty."}), 400
    if len(audio_bytes) > 10 * 1024 * 1024:
        return jsonify({"error": "Voice recording is too large."}), 413

    try:
        transcription = groq_client.audio.transcriptions.create(
            file=(audio_file.filename or "voice.webm", audio_bytes, audio_file.mimetype or "audio/webm"),
            model=GROQ_STT_MODEL,
            response_format="json",
            temperature=0,
            prompt=(
                "An employee database question spoken naturally in English, Urdu, "
                "Roman Urdu, or mixed Urdu-English. Preserve the speaker's words and language."
            )
        )
        transcript = (getattr(transcription, "text", "") or "").strip()
        if not transcript:
            return jsonify({"error": "No speech was detected. Please try again."}), 422
        return jsonify({"transcript": transcript})
    except Exception as exc:
        return jsonify({"error": f"Voice transcription error: {str(exc)}"}), 502


@app.route("/api/chat", methods=["POST"])
def api_chat():
    if not session.get("logged_in"):
        return jsonify({"error": "not_logged_in"}), 401

    data = request.get_json(silent=True) or {}
    message = data.get("message", "")
    message_source = str(data.get("source", "typed")).lower()
    preferred_language = str(data.get("language", "")).upper()
    if preferred_language not in ("UR", "EN"):
        preferred_language = None
    if message_source == "typed" and preferred_language is None:
        preferred_language = _detect_typed_language(message)

    history = session.get("chat_history", [])
    print(f"[DEBUG] raw message repr: {message!r}")
    print(f"[DEBUG] normalized message repr: {_normalize_stt_artifacts(message)!r}")
    reply = handle_message(message, history=history, preferred_language=preferred_language)

    history.append({"sender": "user", "text": message})
    history.append({"sender": "bot", "text": reply})
    session["chat_history"] = history[-16:]

    return jsonify({"reply": reply})


if __name__ == "__main__":
    print(f"Oracle target: {DB_HOST}:{DB_PORT}/{DB_SERVICE}")
    print("Research Agent is starting...")
    app.run(host="127.0.0.1", port=5000, debug=False)
