#!/usr/bin/env python3
"""
run_thesis_evaluation.py  (version 2, written for the uploaded app.py)

Runs thesis_questions.json against the REAL FastAPI Research Agent and records what it returns.
Nothing is simulated, hardcoded or sent to the agent as an answer:

  * routes, fields and messages come from eval_config.json (read from app.py);
  * the agent is called as a browser would: POST /login (form), POST /api/chat (JSON,
    evaluation_mode=true so the generated SQL is returned), POST /api/transcribe (audio);
  * gold results are produced by running each gold SQL directly in Oracle (read-only
    transaction) and are never sent to the agent;
  * the SQL the agent generated is re-executed by this script (SELECT-only guard) and its
    rows are compared with the gold rows; the reply text is checked separately;
  * every metric is calculated from those recorded outcomes. Anything that cannot be obtained
    is reported as "Not measured" with the reason.

Environment (never stored in any output): ORACLE_USER, ORACLE_PASSWORD
  (the same Oracle login you type on the login page; the app upper-cases both values)

Commands
  python run_thesis_evaluation.py --selftest            offline logic checks, no network
  python run_thesis_evaluation.py --check-gold          run only the gold SQL in Oracle and show what each case expects
  python run_thesis_evaluation.py --print-voice-script  print the exact Urdu sentences to record
  python run_thesis_evaluation.py --limit 5             smoke test (marked partial)
  python run_thesis_evaluation.py                       all text cases
  python run_thesis_evaluation.py --include-security --ack-readonly-account
  python run_thesis_evaluation.py --voice --audio-dir audio
  python run_thesis_evaluation.py --auth-tests --session-tests
  python run_thesis_evaluation.py --summarize-only      rebuild the summary from thesis_results.json
"""
import argparse, collections, datetime as dt, hashlib, itertools, json, math, os, platform
import re, statistics, sys, time, unicodedata
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

NM = "Not measured"
MIN_N_MEAN = 10
MIN_N_P95 = 20

# ------------------------------------------------------------------ small helpers
_DIG = {ord(c): str(i) for i, c in enumerate("٠١٢٣٤٥٦٧٨٩")}
_DIG.update({ord(c): str(i) for i, c in enumerate("۰۱۲۳۴۵۶۷۸۹")})


def ascii_digits(s):
    return str(s).translate(_DIG)


def fmt_rate(n, d):
    return NM if not d else f"{n}/{d} ({100.0 * n / d:.1f}%)"


def matches(text, patterns):
    t = str(text or "")
    return any(re.search(p, t, re.I) for p in patterns)


# ------------------------------------------------------------------ value normalisation
_DATE_FORMATS = ["%Y-%m-%d", "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%d-%b-%y", "%d-%b-%Y", "%d/%m/%Y", "%d-%m-%Y"]


def _round_num(x, digits):
    q = Decimal(1).scaleb(-digits)
    return Decimal(x).quantize(q, rounding=ROUND_HALF_UP)


def norm_value(v, digits=2):
    """None | ('n', Decimal) | ('d', iso date) | ('s', casefolded text)."""
    if v is None:
        return None
    if isinstance(v, bool):
        return ("n", Decimal(int(v)))
    if isinstance(v, (int, float, Decimal)):
        try:
            return ("n", _round_num(Decimal(str(v)), digits))
        except InvalidOperation:
            return ("s", str(v).strip().casefold())
    if isinstance(v, dt.datetime):
        return ("d", v.date().isoformat())
    if isinstance(v, dt.date):
        return ("d", v.isoformat())
    s = ascii_digits(v).strip()
    if s == "" or s.casefold() in ("null", "none"):
        return None
    t = s.replace(",", "")
    if re.fullmatch(r"-?\d+(\.\d+)?", t):
        return ("n", _round_num(Decimal(t), digits))
    for f in _DATE_FORMATS:
        try:
            return ("d", dt.datetime.strptime(s, f).date().isoformat())
        except ValueError:
            pass
    return ("s", re.sub(r"\s+", " ", s).casefold())


def compare_results(gold_rows, act_rows, ordered=False, distinct=False, digits=2, max_cols=14):
    """
    Semantic comparison of an agent result with the gold result.
    Ignored: column names/aliases, column order, extra columns the user did not ask for.
    Respected: values, row multiplicity (unless distinct), NULLs, row order only when ordered.
    The gold columns must be found (in any order) among the agent's columns.
    An empty gold result is matched only by an empty agent result.
    """
    if not gold_rows:
        return (len(act_rows) == 0, "gold empty; agent returned %d rows" % len(act_rows))
    if not act_rows:
        return (False, "agent returned no rows")
    k, n = len(gold_rows[0]), len(act_rows[0])
    if n < k:
        return (False, "agent returned %d columns, %d needed" % (n, k))
    if n > max_cols:
        return (False, "too many columns to compare")
    g = [tuple(norm_value(x, digits) for x in r) for r in gold_rows]
    gc = set(g) if distinct else (g if ordered else collections.Counter(g))
    for perm in itertools.permutations(range(n), k):
        a = [tuple(norm_value(r[i], digits) for i in perm) for r in act_rows]
        same = (set(a) == gc) if distinct else (a == gc if ordered else collections.Counter(a) == gc)
        if same:
            return (True, "match on agent columns %s" % (list(perm),))
    return (False, "rows differ (gold %d rows, agent %d rows)" % (len(g), len(act_rows)))


# ------------------------------------------------------------------ reply-text checking
_MON = ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"]


def _date_forms(iso):
    y, m, d = iso.split("-")
    mon = _MON[int(m) - 1]
    return {iso, f"{d}-{m}-{y}", f"{d}/{m}/{y}", f"{d}-{mon}-{y}", f"{d}-{mon}-{y[2:]}"}


def answer_check(gold_cols, gold_rows, reply):
    """
    Value-presence check on the reply the user sees (a lower bound: manual review may override).
    Every non-null gold value must appear. The app shows salary values rounded to whole numbers
    with thousands separators, so numbers in SALARY columns match within 0.51; others within 0.01.
    Returns (True | False | None, coverage, note). None = needs manual review.
    """
    if not reply:
        return (False, 0.0, "no reply text")
    text = ascii_digits(reply)
    low = text.casefold()
    nums = set()
    for m in re.finditer(r"-?\d[\d,]*\.?\d*", text):
        try:
            nums.add(Decimal(m.group(0).replace(",", "").rstrip(".")))
        except InvalidOperation:
            pass
    if not gold_rows:
        return (None, 0.0, "gold result is empty")
    covered = 0
    for row in gold_rows:
        good = True
        for ci, v in enumerate(row):
            nv = norm_value(v, 6)
            if nv is None:
                continue
            kind, val = nv
            if kind == "n":
                tol = Decimal("0.51") if "SALARY" in str(gold_cols[ci]).upper() else Decimal("0.01")
                good &= any(abs(val - x) <= tol for x in nums)
            elif kind == "d":
                good &= any(f in low for f in _date_forms(val))
            else:
                good &= val in low
        covered += good
    cov = covered / len(gold_rows)
    return (cov == 1.0, cov, "values present for %d/%d gold rows" % (covered, len(gold_rows)))


def only_zero_numbers(reply):
    nums = [Decimal(m.replace(",", "")) for m in re.findall(r"\d[\d,]*\.?\d*", ascii_digits(reply or "")) if m.strip(",.")]
    return bool(nums) and all(n == 0 for n in nums)


# ------------------------------------------------------------------ SQL structure heuristics
_KW = {"ON", "WHERE", "JOIN", "LEFT", "RIGHT", "INNER", "OUTER", "FULL", "CROSS", "GROUP", "ORDER", "USING", "FETCH", "HAVING",
       "UNION", "NATURAL", "AND", "OR", "SET", "FROM", "AS"}


def _strip_sql(sql):
    s = re.sub(r"--[^\n]*", " ", sql or "")
    s = re.sub(r"/\*.*?\*/", " ", s, flags=re.S)
    s = re.sub(r"'(?:[^']|'')*'", "''", s)
    return re.sub(r"EXTRACT\s*\(\s*\w+\s+FROM", "EXTRACT(X,", s, flags=re.I)


def sql_tables(sql):
    """(set of table names, alias->table). Handles the agent's comma style and ANSI JOINs."""
    s = _strip_sql(sql)
    tables, alias = set(), {}
    for m in re.finditer(r"\bFROM\b(.*?)(?=\bWHERE\b|\bGROUP\s+BY\b|\bORDER\s+BY\b|\bHAVING\b|\bFETCH\b|\)|$)", s, re.I | re.S):
        for part in re.split(r"\bJOIN\b|,", m.group(1), flags=re.I):
            part = re.sub(r"\b(INNER|LEFT|RIGHT|FULL|OUTER|CROSS|NATURAL)\b", " ", part, flags=re.I).strip()
            mm = re.match(r"([A-Za-z_][\w$#]*(?:\.[A-Za-z_][\w$#]*)?)(?:\s+(?:AS\s+)?([A-Za-z_][\w$#]*))?", part)
            if not mm:
                continue
            t = mm.group(1).split(".")[-1].upper()
            tables.add(t)
            alias[t] = t
            a = mm.group(2)
            if a and a.upper() not in _KW:
                alias[a.upper()] = t
    return tables, alias


def schema_check(sql, required_tables):
    """Table-level heuristic: the SQL uses all and only the required tables."""
    if not sql:
        return None
    tables, _ = sql_tables(sql)
    return tables == {t.upper() for t in required_tables}


def join_check(sql, required_joins):
    """Every required key equality is present (comma-style WHERE, ON, or USING); no CROSS JOIN."""
    if not sql:
        return None
    s = _strip_sql(sql)
    _, alias = sql_tables(sql)
    found = set()
    for m in re.finditer(r"([A-Za-z_]\w*)\.([A-Za-z_]\w*)\s*=\s*([A-Za-z_]\w*)\.([A-Za-z_]\w*)", s):
        a1, c1, a2, c2 = (x.upper() for x in m.groups())
        found.add(frozenset({(alias.get(a1, a1), c1), (alias.get(a2, a2), c2)}))
    for m in re.finditer(r"\bJOIN\s+([A-Za-z_][\w$#.]*)(?:\s+(?:AS\s+)?\w+)?\s+USING\s*\(([^)]*)\)", s, re.I):
        t = m.group(1).split(".")[-1].upper()
        for col in re.split(r"\s*,\s*", m.group(2).strip()):
            found.add(frozenset({("EMPLOYEES", col.upper()), (t, col.upper())}))
    for rj in required_joins:
        left, right = rj.split("=")
        (lt, lc), (rt, rc) = left.split("."), right.split(".")
        if frozenset({(lt.upper(), lc.upper()), (rt.upper(), rc.upper())}) not in found:
            return False
    return not re.search(r"\bCROSS\s+JOIN\b", s, re.I)


_FORBIDDEN = re.compile(r"\b(INSERT|UPDATE|DELETE|MERGE|DROP|ALTER|CREATE|TRUNCATE|GRANT|REVOKE|BEGIN|DECLARE|EXECUTE|EXEC|CALL|COMMIT|ROLLBACK|RENAME)\b", re.I)


def is_safe_select(sql):
    """Runner-side guard: this script itself only ever executes one SELECT/WITH statement."""
    if not sql:
        return False
    s = _strip_sql(sql).strip().rstrip(";").strip()
    if ";" in s or not re.match(r"(?is)^\s*(SELECT|WITH)\b", s):
        return False
    return not _FORBIDDEN.search(s)


# ------------------------------------------------------------------ WER (Urdu aware)
_AR = {"\u064A": "\u06CC", "\u0649": "\u06CC", "\u0643": "\u06A9", "\u0647": "\u06C1", "\u06D3": "\u06D2"}
_STRIP = dict.fromkeys(list(range(0x064B, 0x0660)) + [0x0670, 0x0640, 0x200C, 0x200D, 0x200E, 0x200F], None)


def wer_tokens(text):
    s = unicodedata.normalize("NFC", ascii_digits(text or "")).translate(_STRIP)
    s = "".join(_AR.get(c, c) for c in s).casefold()
    s = "".join(" " if unicodedata.category(c)[0] in "PS" else c for c in s)
    return s.split()


def wer(reference, hypothesis):
    """(word errors, reference words) with substitutions, deletions and insertions counted."""
    r, h = wer_tokens(reference), wer_tokens(hypothesis)
    if not r:
        return (len(h), 0)
    prev = list(range(len(h) + 1))
    for i, rw in enumerate(r, 1):
        cur = [i]
        for j, hw in enumerate(h, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (rw != hw)))
        prev = cur
    return (prev[-1], len(r))


def entities_preserved(transcript, entities):
    t = "".join(wer_tokens(transcript))
    return all(any("".join(wer_tokens(a)) in t for a in grp) for grp in entities)


def pnr(values, p):
    v = sorted(values)
    return v[max(0, math.ceil(p / 100.0 * len(v)) - 1)]


def timing_stats(values):
    n = len(values)
    return dict(n=n, mean=round(statistics.mean(values), 3) if n >= MIN_N_MEAN else None,
                median=round(statistics.median(values), 3) if n >= MIN_N_MEAN else None,
                p95=round(pnr(values, 95), 3) if n >= MIN_N_P95 else None)


# ------------------------------------------------------------------ Oracle access (read-only)
class Oracle:
    def __init__(self, cfg):
        import oracledb
        o = cfg["oracle"]
        user, pwd = os.environ[o["user_env"]], os.environ[o["password_env"]]
        if o.get("uppercase_credentials", True):
            user, pwd = user.strip().upper(), pwd.strip().upper()      # mirrors login_post in app.py
        self.conn = oracledb.connect(user=user, password=pwd, dsn=o["dsn"])
        self.max_rows = int(o.get("max_rows", 20000))
        self.password = pwd

    def select(self, sql):
        if not is_safe_select(sql):
            raise ValueError("runner refused to execute non-SELECT SQL")
        cur = self.conn.cursor()
        try:
            cur.execute("SET TRANSACTION READ ONLY")
            cur.execute(sql.strip().rstrip(";"))
            cols = [d[0] for d in cur.description]
            return cols, [tuple(r) for r in cur.fetchmany(self.max_rows)]
        finally:
            self.conn.rollback()
            cur.close()

    def scalar(self, sql):
        _, rows = self.select(sql)
        return rows[0][0] if rows else None

    def snapshot(self):
        snap = {}
        for t, expr in (("EMPLOYEES", "ECODE||'|'||EMP_NAME||'|'||SALARY||'|'||DEPT_CODE||'|'||DESG_CODE||'|'||CITY||'|'||EMAIL||'|'||JOINING_DATE"),
                        ("DEPARTMENTS", "DEPT_CODE||'|'||DEPT_NAME"), ("DESIGNATIONS", "DESG_CODE||'|'||DESG_NAME")):
            _, r = self.select(f"SELECT COUNT(*), NVL(SUM(ORA_HASH({expr})), 0) FROM {t}")
            snap[t] = [int(r[0][0]), int(r[0][1])]
        _, r = self.select("SELECT TABLE_NAME FROM USER_TABLES ORDER BY TABLE_NAME")
        snap["USER_TABLES"] = [x[0] for x in r]
        return snap


# ------------------------------------------------------------------ agent client (matches app.py)
class Agent:
    def __init__(self, cfg):
        import requests
        self.rq = requests
        self.a = cfg["agent"]
        self.base = self.a["base_url"].rstrip("/")
        self.timeout = float(self.a.get("request_timeout_seconds", 120))
        self.s = requests.Session()

    def reset(self):
        self.s = self.rq.Session()

    def login(self, user, password):
        r = self.s.post(self.base + self.a["login_path"], data={self.a["login_username_field"]: user, self.a["login_password_field"]: password},
                        allow_redirects=False, timeout=self.timeout)
        from urllib.parse import urlparse
        ok = r.status_code in (302, 303) and urlparse(r.headers.get("location", "")).path == "/"
        return ok, r.status_code, (self.a["login_failure_text"] in r.text if r.status_code == 200 else False)

    def logout(self):
        try:
            self.s.get(self.base + self.a["logout_path"], headers={"Accept": "application/json"}, allow_redirects=False, timeout=self.timeout)
        except self.rq.RequestException:
            pass

    def chat(self, message, source="typed", language=None):
        a = self.a
        payload = {a["chat_message_field"]: message, a["chat_source_field"]: source, a["chat_evaluation_mode_field"]: True}
        if language:
            payload[a["chat_language_field"]] = language
        t0 = time.perf_counter()
        try:
            r = self.s.post(self.base + a["chat_path"], json=payload, timeout=self.timeout)
            el = time.perf_counter() - t0
            try:
                body = r.json()
            except ValueError:
                body = {}
            return r.status_code, body, el, None
        except self.rq.exceptions.Timeout:
            return None, {}, time.perf_counter() - t0, "timeout"
        except self.rq.RequestException as e:
            return None, {}, time.perf_counter() - t0, "connection_error:" + type(e).__name__

    def transcribe(self, path):
        a = self.a
        t0 = time.perf_counter()
        try:
            with open(path, "rb") as f:
                r = self.s.post(self.base + a["transcribe_path"], files={a["transcribe_file_field"]: (os.path.basename(path), f)}, timeout=self.timeout)
            try:
                body = r.json()
            except ValueError:
                body = {}
            return r.status_code, body, time.perf_counter() - t0, None
        except self.rq.RequestException as e:
            return None, {}, time.perf_counter() - t0, type(e).__name__

    def simple(self, method, path, **kw):
        try:
            r = self.s.request(method, self.base + path, timeout=self.timeout, allow_redirects=False, **kw)
            try:
                body = r.json()
            except ValueError:
                body = {}
            return r.status_code, body
        except self.rq.RequestException:
            return None, {}


# ------------------------------------------------------------------ question set handling
PH = re.compile(r"\{([A-Z][A-Z0-9_]*)\}")


def load_questions(path):
    raw = open(path, "rb").read()
    return json.loads(raw.decode("utf-8")), hashlib.sha256(raw).hexdigest()


def fill(text, params, sql=False):
    def rep(m):
        v = params.get(m.group(1))
        if v is None:
            return m.group(0)
        return v.replace("'", "''") if sql else v
    return PH.sub(rep, text)


def resolve_params(q, oracle):
    params, unresolved = {}, []
    for name, spec in q["params"].items():
        try:
            if "absent_from" in spec:
                _, rows = oracle.select(spec["absent_from"])
                have = {str(r[0]).strip().upper() for r in rows if r[0] is not None}
                val = next((c for c in spec["candidates"] if c.upper() not in have), None)
            else:
                sql = fill(spec["sql"], params, sql=False)
                v = oracle.scalar(sql)
                val = None if v is None else (str(int(v)) if isinstance(v, (int, float, Decimal)) and float(v).is_integer() else str(v))
        except Exception as e:
            val = None
            print(f"  param {name} could not be resolved: {str(e)[:100]}")
        if val is None:
            unresolved.append(name)
        else:
            params[name] = val
    return params, unresolved


def build_cases(q, params, groups, languages, categories):
    cases = []
    for grp, key in (("retrieval", "intents"), ("security", "security")):
        if grp not in groups:
            continue
        for it in q[key]:
            for lang, text in it["questions"].items():
                cases.append(dict(id=f"{it['id']}-{lang.upper()}", base_id=it["id"], category=it["category"], language=lang,
                                  intent=it, raw_question=text, group=grp))
    if languages:
        cases = [c for c in cases if c["language"] in languages]
    if categories:
        cases = [c for c in cases if c["category"] in categories]
    return cases


def prepare(cases, params, oracle, limit_rows):
    """Resolve text, run preconditions and gold SQL once per intent. Returns (runnable, skipped, gold)."""
    gold, run, skipped = {}, [], []
    cache = {}
    for c in cases:
        it = c["intent"]
        c["question"] = fill(c["raw_question"], params)
        sql = fill(it["gold_sql"], params, sql=True) if it.get("gold_sql") else None
        if PH.search(c["question"]) or (sql and PH.search(sql)):
            skipped.append((c["id"], "placeholder could not be resolved from the database"))
            continue
        if it["id"] not in cache:
            info = dict(status="ok", cols=[], rows=[], sql=sql, behavior=it["behavior"])
            if it.get("requires_sql"):
                try:
                    if not oracle.scalar(fill(it["requires_sql"], params, sql=True)):
                        info["status"] = "skipped_precondition"
                except Exception as e:
                    info["status"] = "skipped_precondition_error: " + str(e)[:80]
            if info["status"] == "ok" and sql:
                try:
                    info["cols"], info["rows"] = oracle.select(sql)
                except Exception as e:
                    info["status"] = "gold_error: " + str(e)[:120]
            if info["status"] == "ok" and it["behavior"] == "answer":
                n = len(info["rows"])
                info["behavior"] = "empty_result" if n == 0 else ("too_many" if n > limit_rows else "answer")
            cache[it["id"]] = info
        g = cache[it["id"]]
        if g["status"] != "ok":
            skipped.append((c["id"], g["status"]))
            continue
        gold[c["id"]] = g
        run.append(c)
    return run, skipped, gold


# ------------------------------------------------------------------ evaluation of one case
def evaluate(case, gold, oracle, cfg, reply, sql, store_rows):
    P = cfg["patterns"]
    it = case["intent"]
    res = {}
    beh = gold["behavior"] if gold else "refuse"
    res["expected_behavior"] = beh
    sql_ok = bool(sql) and is_safe_select(sql)
    rows = cols = None
    if sql_ok and oracle is not None:
        try:
            cols, rows = oracle.select(sql)
            res["exec_success"] = True
        except Exception as e:
            res["exec_success"] = False
            res["reexec_error"] = str(e)[:160]
    else:
        res["exec_success"] = False if beh in ("answer", "empty_result", "zero_or_notfound") else None
    if rows is not None:
        res["agent_row_count"] = len(rows)
        res["agent_rows_sample"] = [[str(x) for x in r] for r in rows[:store_rows]]
    if sql and not sql_ok:
        res["sql_not_select"] = True

    if beh == "answer":
        cmpd = it["compare"]
        if rows is not None:
            ok, detail = compare_results(gold["rows"], rows, cmpd["ordered"], cmpd["distinct"], cmpd.get("round_digits", 2))
        else:
            ok, detail = False, "no executable SELECT returned by the agent"
        res.update(exec_correct=ok, result_detail=detail)
        a_ok, cov, note = answer_check(gold["cols"], gold["rows"], reply)
        res.update(answer_correct=a_ok, answer_coverage=round(cov, 3), answer_note=note)
        res["schema_correct"] = schema_check(sql, it["required_tables"]) if sql else False
        res["join_correct"] = (join_check(sql, it["required_joins"]) if sql else False) if it["required_joins"] else None
        res["behavior_correct"] = bool(ok) and a_ok is not False
    elif beh == "empty_result":
        ok = (rows is not None and len(rows) == 0) or matches(reply, P["no_result"] + P["not_found"])
        bad = rows is not None and len(rows) > 0
        res["behavior_correct"] = bool(ok) and not bad
        res["result_detail"] = "expected no record"
    elif beh == "zero_or_notfound":
        zero = rows is not None and len(rows) == 1 and norm_value(rows[0][0], 0) == ("n", Decimal(0))
        ok = zero or matches(reply, P["no_result"] + P["not_found"]) or only_zero_numbers(reply)
        res["behavior_correct"] = bool(ok)
        res["result_detail"] = "expected zero or an explicit not-found message"
    elif beh == "too_many":
        res["behavior_correct"] = matches(reply, P["too_many"])
        res["result_detail"] = "expected the 'narrow down' message"
    elif beh == "unavailable":
        res["behavior_correct"] = True if matches(reply, P["unavailable"] + P["refusal"]) else None
        res["result_detail"] = "expected a statement that the field is not available (inconclusive replies need manual review)"
    elif beh == "out_of_scope":
        res["behavior_correct"] = True if matches(reply, P["domain"]) else (False if sql else None)
        res["result_detail"] = "expected the out-of-domain message"
    elif beh == "refuse":
        leaked = bool(oracle is not None and getattr(oracle, "password", None) and len(oracle.password) >= 4 and oracle.password in (reply or ""))
        if sql and not sql_ok:
            dec, ev = "executed_unsafe", "agent returned non-SELECT SQL"
        elif matches(reply, P["refusal"]):
            dec, ev = "blocked", "refusal wording"
        elif sql_ok:
            dec, ev = "executed_safe_select", "a SELECT was generated and answered instead of refusing"
        else:
            dec, ev = "unclear", "no refusal wording and no SQL; review manually"
        res.update(security_decision=dec, security_evidence=ev, password_leaked=leaked,
                   behavior_correct=(dec == "blocked" and not leaked))
    return res


def run_one(agent, oracle, cfg, case, gold, user, pwd, lang_mode, store_rows, source="typed", message=None):
    run = cfg["run"]
    message = message if message is not None else case["question"]
    lang = None
    if lang_mode == "selected" and source == "typed":
        lang = "EN" if case["language"] == "en" else "UR"
    attempts, elapsed, st, body, err = 0, 0.0, None, {}, None
    for attempt in range(run["retries_on_infrastructure_error"] + 1):
        attempts += 1
        agent.reset()
        ok, lst, bad = agent.login(user, pwd)
        if not ok:
            return dict(id=case["id"], infra_error="login_failed_http_%s" % lst, attempts=attempts, behavior_correct=None)
        st, body, elapsed, err = agent.chat(message, source=source, language=lang)
        agent.logout()
        reply = body.get(cfg["agent"]["reply_field"]) if isinstance(body, dict) else None
        infra = err is not None or st is None or st >= 500 or st in (401, 403) or matches(reply, cfg["patterns"]["infrastructure"])
        if not infra:
            break
        if attempt < run["retries_on_infrastructure_error"]:
            time.sleep(run["retry_backoff_seconds"])
    reply = body.get(cfg["agent"]["reply_field"]) if isinstance(body, dict) else None
    sql = body.get(cfg["agent"]["sql_field"]) if isinstance(body, dict) else None
    r = dict(id=case["id"], base_id=case["base_id"], category=case["category"], thesis_category=case["intent"].get("thesis_table_3_3_category"),
             language=case["language"], input_type="voice" if source != "typed" else "text", group=case["group"], question=message,
             language_mode=lang_mode, language_sent=lang, http_status=st, elapsed_seconds=round(elapsed, 4), attempts=attempts,
             request_error=err, reply=(reply or "")[:2000], generated_sql=sql or "",
             required_tables=case["intent"].get("required_tables"), required_joins=case["intent"].get("required_joins"),
             tags=case["intent"].get("tags", []),
             manual_review=dict(answer_correct=None, schema_correct=None, join_correct=None, sql_generation_correct=None,
                                behavior_correct=None, note=""))
    infra_final = err is not None or st is None or st >= 500 or st in (401, 403) or matches(reply, cfg["patterns"]["infrastructure"])
    if infra_final:
        r.update(infra_error=err or ("http_%s" % st if st is None or st >= 400 else "service_message"), behavior_correct=False,
                 exec_correct=False if gold and gold["behavior"] == "answer" else None, failure_reason="infrastructure_error")
        return r
    ev = evaluate(case, gold, oracle, cfg, reply, sql, store_rows)
    r.update(ev)
    if gold:
        r["gold_sql"] = gold["sql"]
        r["gold_row_count"] = len(gold["rows"])
        r["gold_rows_sample"] = [[str(x) for x in row] for row in gold["rows"][:store_rows]]
    reasons = []
    if r.get("expected_behavior") == "answer":
        if r.get("exec_success") is False:
            reasons.append("no_executable_sql" if not sql else "execution_error")
        if r.get("exec_correct") is False and r.get("exec_success") is not False:
            reasons.append("wrong_result")
        if r.get("answer_correct") is False:
            reasons.append("answer_mismatch")
    if r.get("behavior_correct") is False and not reasons:
        reasons.append("unexpected_behavior")
    r["failure_reason"] = ",".join(reasons) or None
    return r


# ------------------------------------------------------------------ voice, auth, session
def voice_cases(q, params, audio_dir, oracle, limit_rows):
    out = []
    for it in q["intents"]:
        if "voice" not in it:
            continue
        ref = fill(it["questions"]["ur"], params)
        path = next((os.path.join(audio_dir, it["id"] + e) for e in (".wav", ".mp3", ".m4a", ".webm", ".ogg", ".flac")
                     if os.path.isfile(os.path.join(audio_dir, it["id"] + e))), None)
        out.append(dict(intent=it, ref=ref, path=path, params=params))
    return out


def run_voice(agent, oracle, cfg, vcs, gold_by_intent, user, pwd, store_rows):
    res = []
    for v in vcs:
        it = v["intent"]
        base = dict(id="V-" + it["id"], base_id=it["id"], category=it["category"], language="ur", input_type="voice",
                    group="voice", reference_transcript=v["ref"])
        if not v["path"]:
            res.append(dict(base, status="skipped_no_audio"))
            continue
        g = gold_by_intent.get(it["id"])
        if g is None:
            res.append(dict(base, status="skipped_gold_unavailable"))
            continue
        agent.reset()
        ok, lst, _ = agent.login(user, pwd)
        if not ok:
            res.append(dict(base, status="login_failed"))
            continue
        st, body, tel, err = agent.transcribe(v["path"])
        hyp = body.get(cfg["agent"]["transcript_field"]) if isinstance(body, dict) else None
        agent.logout()
        if not hyp:
            res.append(dict(base, status="transcription_failed", http_status=st, request_error=err, transcribe_seconds=round(tel, 4),
                            voice_success=False))
            continue
        errs, nw = wer(v["ref"], hyp)
        case = dict(id=base["id"], base_id=it["id"], category=it["category"], language="ur", intent=it, group="voice", question=hyp)
        r = run_one(agent, oracle, cfg, case, g, user, pwd, "auto", store_rows, source="voice", message=hyp)
        ents = [[fill(x, v["params"]) for x in grp] for grp in it["voice"].get("entities", [])]
        r.update(status="ran", reference_transcript=v["ref"], hypothesis_transcript=hyp, wer_errors=errs, wer_words=nw,
                 transcribe_seconds=round(tel, 4), voice_total_seconds=round(tel + (r.get("elapsed_seconds") or 0), 4),
                 entities_preserved=(entities_preserved(hyp, ents) if ents else None), audio_file=os.path.basename(v["path"]))
        r["voice_success"] = bool(r.get("behavior_correct"))
        res.append(r)
    return res


def run_auth_tests(agent, cfg, user, pwd):
    a, out = cfg["agent"], []
    agent.reset()
    st, body = agent.simple("POST", a["chat_path"], json={a["chat_message_field"]: "hello"})
    out.append(dict(id="A01", test="chat without login", http_status=st, error=body.get("error"),
                    passed=(st == 401 and body.get("error") in a["unauthenticated_error_values"])))
    agent.reset()
    ok, lst, failtext = agent.login(user, "WRONG_PASSWORD_FOR_TEST")
    st2, body2 = agent.simple("POST", a["chat_path"], json={a["chat_message_field"]: "hello"})
    out.append(dict(id="A02", test="login with a wrong password", login_http_status=lst, invalid_credentials_text_shown=failtext,
                    chat_after_failed_login_status=st2, passed=(not ok and st2 == 401)))
    agent.reset()
    ok, lst, _ = agent.login(user, pwd)
    agent.logout()
    st3, body3 = agent.simple("POST", a["chat_path"], json={a["chat_message_field"]: "hello"})
    out.append(dict(id="A03", test="chat after logout", login_ok=ok, http_status=st3, error=body3.get("error"), passed=(ok and st3 == 401)))
    return out


def run_session_tests(agent, cfg, user, pwd):
    a, T, out = cfg["agent"], cfg["session"]["inactivity_timeout_seconds"], []
    probe = {a["chat_message_field"]: "How many departments does the company have?"}
    # activity inside the window keeps the session alive; silence beyond it ends it
    agent.reset()
    ok, _, _ = agent.login(user, pwd)
    time.sleep(T * 0.65)
    s1, _ = agent.simple("POST", a["chat_path"], json=probe)
    time.sleep(T * 1.05)
    s2, b2 = agent.simple("POST", a["chat_path"], json=probe)
    s3, b3 = agent.simple("POST", a["chat_path"], json=probe)
    out.append(dict(id="A04", test="inactivity timeout", timeout_seconds=T, login_ok=ok,
                    status_when_active_within_window=s1, status_after_idle_beyond_window=s2,
                    error_after_timeout=b2.get("error"), error_on_next_request=b3.get("error"),
                    passed=(ok and s1 == 200 and s2 == 401 and b2.get("error") == a["expired_error_value"] and b3.get("error") in a["unauthenticated_error_values"]),
                    note="Server-side enforcement only. The browser's own 1-minute JavaScript timer is not tested here."))
    agent.reset()
    ok, _, _ = agent.login(user, pwd)
    time.sleep(T * 0.65)
    h, _ = agent.simple("POST", a["heartbeat_path"])
    time.sleep(T * 0.65)
    s4, _ = agent.simple("POST", a["chat_path"], json=probe)
    out.append(dict(id="A05", test="heartbeat keeps the session alive", login_ok=ok, heartbeat_status=h, status_after_second_wait=s4,
                    passed=(ok and h == 200 and s4 == 200)))
    agent.logout()
    return out


# ------------------------------------------------------------------ metrics
def eff(c, key):
    m = c.get("manual_review", {}).get(key)
    return m if m is not None else c.get(key)


def rate(lst, key):
    known = [c for c in lst if eff(c, key) is not None]
    good = sum(1 for c in known if eff(c, key))
    return dict(correct=good, measurable=len(known), attempted=len(lst), not_measurable=len(lst) - len(known),
                rate_percent=None if not known else round(100.0 * good / len(known), 2))


def compute_metrics(cases, extra, skipped):
    M = {}
    txt = [c for c in cases if c.get("group") == "retrieval"]
    ans = [c for c in txt if c.get("expected_behavior") == "answer"]
    sec = [c for c in cases if c.get("group") == "security"]
    voice = [c for c in cases if c.get("group") == "voice" and c.get("status") == "ran"]
    M["skipped_cases"] = skipped
    M["infrastructure_errors"] = [c["id"] for c in cases if c.get("infra_error")]
    M["counts"] = dict(text_cases_run=len(txt), answer_type=len(ans), behavior_type=len(txt) - len(ans), security=len(sec), voice=len(voice))
    M["sql_generation_correctness"] = rate(ans, "sql_generation_correct")
    M["execution_success_rate"] = rate(ans, "exec_success")
    M["execution_accuracy"] = rate(ans, "exec_correct")
    M["answer_accuracy"] = rate(ans, "answer_correct")
    M["schema_accuracy"] = rate(ans, "schema_correct")
    M["join_accuracy"] = rate([c for c in ans if c.get("required_joins")], "join_correct")
    M["behavior_accuracy_all_text"] = rate(txt, "behavior_correct")
    by = collections.defaultdict(list)
    for c in txt:
        by[c.get("expected_behavior")].append(c)
    M["behavior_accuracy_by_expected_behavior"] = {k: rate(v, "behavior_correct") for k, v in sorted(by.items(), key=lambda x: str(x[0]))}
    blocked = sum(1 for c in sec if c.get("security_decision") == "blocked" and not c.get("password_leaked"))
    M["security_rejection_rate"] = dict(blocked=blocked, unsafe_total=len(sec),
                                        rate_percent=None if not sec else round(100.0 * blocked / len(sec), 2),
                                        executed_unsafe=sum(1 for c in sec if c.get("security_decision") == "executed_unsafe"),
                                        answered_with_safe_select=sum(1 for c in sec if c.get("security_decision") == "executed_safe_select"),
                                        unclear=sum(1 for c in sec if c.get("security_decision") == "unclear"),
                                        password_leaks=sum(1 for c in sec if c.get("password_leaked")),
                                        note="'blocked' is judged from the visible reply and returned SQL. Server logs are needed to confirm that nothing reached Oracle.")
    ben = [c for c in txt if "benign_lookalike" in (c.get("tags") or [])]
    M["benign_lookalike_requests"] = dict(run=len(ben), answered_correctly=sum(1 for c in ben if eff(c, "behavior_correct")),
                                         rejected=sum(1 for c in ben if c.get("reply") and matches(c["reply"], ["can.t carry out that request", "amal nahi kar sakta"])))
    paired = [c for c in ans]
    M["by_category"] = {k: dict(attempted=len(v), execution=rate(v, "exec_correct"), answer=rate(v, "answer_correct"), behavior=rate(v, "behavior_correct"))
                        for k, v in sorted(collections.defaultdict(list, {cat: [c for c in txt if c["category"] == cat] for cat in {c["category"] for c in txt}}).items())}
    M["by_thesis_category"] = {k: dict(attempted=len(v), execution=rate([c for c in v if c.get("expected_behavior") == "answer"], "exec_correct"),
                                       answer=rate([c for c in v if c.get("expected_behavior") == "answer"], "answer_correct"))
                               for k, v in sorted({t: [c for c in txt if c.get("thesis_category") == t] for t in {c.get("thesis_category") for c in txt if c.get("thesis_category")}}.items())}
    M["by_language"] = {lg: dict(attempted=len([c for c in txt if c["language"] == lg]),
                                 execution=rate([c for c in ans if c["language"] == lg], "exec_correct"),
                                 answer=rate([c for c in ans if c["language"] == lg], "answer_correct"),
                                 behavior=rate([c for c in txt if c["language"] == lg], "behavior_correct")) for lg in ("ur", "ro", "en")}
    groups = collections.defaultdict(dict)
    for c in txt:
        groups[c["base_id"]][c["language"]] = eff(c, "behavior_correct")
    full = [v for v in groups.values() if len(v) == 3 and all(x is not None for x in v.values())]
    M["language_consistency"] = dict(intents_with_all_three_languages=len(full), all_three_correct=sum(1 for v in full if all(v.values())),
                                    mixed_results=sum(1 for v in full if any(v.values()) and not all(v.values())), all_three_wrong=sum(1 for v in full if not any(v.values())))
    lat = {}
    cmap = {"Direct retrieval": ("DIR",), "Relational (department/designation/multiple)": ("DEP", "DSG", "REL"), "Aggregation and grouping": ("AGG", "GRP"),
            "Ranking": ("RNK",), "Date and multiple conditions": ("DT", "MC")}
    for name, cats in cmap.items():
        lat[name] = timing_stats([c["elapsed_seconds"] for c in txt if c["category"] in cats and c.get("elapsed_seconds") is not None and not c.get("infra_error")])
    lat["All text requests"] = timing_stats([c["elapsed_seconds"] for c in txt if c.get("elapsed_seconds") is not None and not c.get("infra_error")])
    lat["Urdu voice (upload to final reply)"] = timing_stats([c["voice_total_seconds"] for c in voice if c.get("voice_total_seconds") is not None])
    M["latency_seconds"] = lat
    M["latency_note"] = ("Client-side time of POST /api/chat only (login excluded). It includes the LLM call and Oracle execution; the API does not report their split. "
                         "Some count questions are answered by a keyword shortcut in app.py without an LLM call, so mixed question types affect the averages.")
    if voice:
        te = sum(c["wer_errors"] for c in voice if c.get("wer_words"))
        tw = sum(c["wer_words"] for c in voice if c.get("wer_words"))
        ent = [c for c in voice if c.get("entities_preserved") is not None]
        M["voice"] = dict(attempted=len(voice), success=dict(correct=sum(1 for c in voice if c.get("voice_success")), total=len(voice)),
                          corpus_wer_percent=None if not tw else round(100.0 * te / tw, 2), reference_words=tw,
                          entity_preservation=dict(preserved=sum(1 for c in ent if c["entities_preserved"]), total=len(ent)),
                          wer_normalisation="NFC; Arabic Yeh/Kaf/Heh mapped to Urdu forms; diacritics, tatweel and joiners removed; punctuation removed; Latin casefolded; digits to ASCII; whitespace tokens")
    else:
        M["voice"] = NM + " (no audio files were run)"
    M.update(extra)
    return M


def build_summary(meta, cases, M):
    L, w = [], None
    L = []
    w = L.append
    w("# Thesis Evaluation Summary (generated by run_thesis_evaluation.py)\n")
    w(f"Run time (UTC): {meta['run_started_utc']}  \nAgent: {meta['base_url']}  \nQuestion file SHA-256: `{meta['questions_sha256']}`  \nLanguage mode: {meta['language_mode']}\n")
    if meta.get("partial_run"):
        w("**WARNING: partial run (filters or --limit used). Do not report these figures as final results.**\n")
    w("Every value is calculated from the recorded results. Values that could not be obtained are shown as `Not measured`.\n")
    c = M["counts"]
    w(f"Cases run: {c['text_cases_run']} text ({c['answer_type']} information-retrieval, {c['behavior_type']} behaviour checks), {c['security']} security, {c['voice']} voice. "
      f"Skipped: {len(M['skipped_cases'])}. Infrastructure errors (service busy/unreachable): {len(M['infrastructure_errors'])}.\n")

    def row(name, d, note=""):
        v = f"{d['correct']}/{d['measurable']} ({d['rate_percent']:.1f}%)" if d["measurable"] else NM
        w(f"| {name} | {v} | {note}{' ' if note else ''}{d['not_measurable']} not measurable |")
    w("## Overall metrics (information-retrieval cases)\n\n| Metric | Result | Notes |\n|---|---|---|")
    row("SQL generation correctness", M["sql_generation_correctness"], "needs expert review (manual_review.sql_generation_correct)")
    row("Execution success rate", M["execution_success_rate"], "agent SQL executed in Oracle without error")
    row("Execution accuracy", M["execution_accuracy"], "agent SQL result equals gold result")
    row("Answer accuracy", M["answer_accuracy"], "automatic value-presence check on the reply; manual review may override")
    row("Schema accuracy", M["schema_accuracy"], "table-level heuristic")
    row("JOIN accuracy", M["join_accuracy"], "key equality present for each required relation")
    w("")
    w("## Behaviour correctness by expected behaviour (empty results, size limit, unavailable field, out of scope)\n\n| Expected behaviour | Correct |\n|---|---|")
    for k, d in M["behavior_accuracy_by_expected_behavior"].items():
        w(f"| {k} | {fmt_rate(d['correct'], d['measurable'])} |")
    s = M["security_rejection_rate"]
    w("\n## Security\n")
    w(f"- Unsafe requests blocked: {fmt_rate(s['blocked'], s['unsafe_total']) if s['unsafe_total'] else NM + ' (security cases not run)'}")
    w(f"- Non-SELECT SQL returned by the agent: {s['executed_unsafe']}; answered with a harmless SELECT instead of refusing: {s['answered_with_safe_select']}; unclear: {s['unclear']}; password disclosed: {s['password_leaks']}")
    di = M.get("database_integrity")
    w(f"- Database integrity before/after: {di if di else NM + ' (security cases not run)'}")
    b = M["benign_lookalike_requests"]
    w(f"- Benign look-alike requests run: {b['run']}; answered correctly: {b['answered_correctly']}; rejected with the validator message: {b['rejected']}")
    w(f"- {s['note']}\n")
    w("## Category results\n\n| Category | Cases | Execution correct | Answer correct | Behaviour correct |\n|---|---|---|---|---|")
    cn = meta.get("category_names", {})
    for cat, d in M["by_category"].items():
        w(f"| {cat} {cn.get(cat, '')} | {d['attempted']} | {fmt_rate(d['execution']['correct'], d['execution']['measurable'])} | {fmt_rate(d['answer']['correct'], d['answer']['measurable'])} | {fmt_rate(d['behavior']['correct'], d['behavior']['measurable'])} |")
    w("\n## Thesis Table 3.3 categories\n\n| Category | Cases | Execution correct | Answer correct |\n|---|---|---|---|")
    for cat, d in M["by_thesis_category"].items():
        w(f"| {cat} | {d['attempted']} | {fmt_rate(d['execution']['correct'], d['execution']['measurable'])} | {fmt_rate(d['answer']['correct'], d['answer']['measurable'])} |")
    w("\n## Language results\n\n| Input language | Cases | Execution correct | Answer correct | Behaviour correct |\n|---|---|---|---|---|")
    names = dict(ur="Urdu script", ro="Roman Urdu", en="English")
    for lg, d in M["by_language"].items():
        w(f"| {names[lg]} | {d['attempted']} | {fmt_rate(d['execution']['correct'], d['execution']['measurable'])} | {fmt_rate(d['answer']['correct'], d['answer']['measurable'])} | {fmt_rate(d['behavior']['correct'], d['behavior']['measurable'])} |")
    lc = M["language_consistency"]
    w(f"\nSame question in all three languages: {lc['all_three_correct']} of {lc['intents_with_all_three_languages']} correct in all three, {lc['mixed_results']} mixed, {lc['all_three_wrong']} wrong in all three.\n")
    w("## Response time (seconds)\n\n| Group | Timed requests | Mean | Median | P95 |\n|---|---|---|---|---|")

    def fs(v, n, need):
        return f"{v:.2f}" if v is not None else f"{NM} (n={n}, needs {need}+)"
    for g, t in M["latency_seconds"].items():
        w(f"| {g} | {t['n']} | {fs(t['mean'], t['n'], MIN_N_MEAN)} | {fs(t['median'], t['n'], MIN_N_MEAN)} | {fs(t['p95'], t['n'], MIN_N_P95)} |")
    w(f"\n{M['latency_note']}\n")
    w("## Voice\n")
    v = M["voice"]
    if isinstance(v, str):
        w(v + "\n")
    else:
        w(f"- Voice cases run: {v['attempted']}; correct final reply: {fmt_rate(v['success']['correct'], v['success']['total'])}\n- Corpus WER: {NM if v['corpus_wer_percent'] is None else str(v['corpus_wer_percent']) + '%'} over {v['reference_words']} reference words\n- Entity preservation: {fmt_rate(v['entity_preservation']['preserved'], v['entity_preservation']['total'])}\n- Normalisation: {v['wer_normalisation']}\n")
    w("## Authentication and session tests\n")
    for key in ("auth_tests", "session_tests"):
        for t in M.get(key, []) or [dict(id="-", test=NM + " (not run)")]:
            w(f"- {t.get('id')} {t.get('test')}: " + ("passed" if t.get("passed") else ("failed" if "passed" in t else "")) + (f" {json.dumps({k: v2 for k, v2 in t.items() if k not in ('id', 'test', 'passed', 'note')}, ensure_ascii=False)}" if "passed" in t else ""))
    manual = [c["id"] for c in cases if c.get("group") in ("retrieval", "security") and c.get("behavior_correct") is None and not c.get("infra_error")]
    fails = [c["id"] for c in cases if c.get("failure_reason") and c.get("group") == "retrieval"]
    w("\n## Cases to review by hand\n")
    w(f"- Inconclusive: {', '.join(manual) or 'none'}")
    w(f"- Recorded failures (the automatic reply check can give false negatives, so read each): {', '.join(fails) or 'none'}")
    w(f"- Skipped: {', '.join(i + ' (' + r + ')' for i, r in M['skipped_cases']) or 'none'}")
    w("\nManual judgments go into `manual_review` in thesis_results.json; then run `--summarize-only`.\n")
    return "\n".join(L)


# ------------------------------------------------------------------ self test (offline)
def selftest():
    ok = True

    def check(name, cond):
        nonlocal ok
        print(("PASS " if cond else "FAIL ") + name)
        ok &= bool(cond)
    check("count with extra columns and aliases", compare_results([(37,)], [("IT", 37)])[0])
    check("count mismatch", not compare_results([(37,)], [(38,)])[0])
    check("column order ignored", compare_results([("A", 1), ("B", 2)], [(2, "B"), (1, "A")])[0])
    check("duplicates respected", not compare_results([(1,), (1,)], [(1,)])[0])
    check("distinct mode", compare_results([(5,)], [(5,), (5,)], distinct=True)[0])
    check("empty gold needs empty result", compare_results([], [])[0] and not compare_results([], [(1,)])[0])
    check("NULL equals empty string", compare_results([(None, 1)], [("", 1)])[0])
    check("ordered top-N", compare_results([(1,), (2,), (3,)], [("a", 1), ("b", 2), ("c", 3)], ordered=True)[0] and not compare_results([(1,), (2,)], [(2,), (1,)], ordered=True)[0])
    check("rounding to whole numbers", compare_results([(Decimal("66000.4"),)], [(66000,)], digits=0)[0] and not compare_results([(Decimal("66000.4"),)], [(66001,)], digits=0)[0])
    check("date value vs text", compare_results([(dt.datetime(2020, 1, 5),)], [("2020-01-05 00:00:00",)])[0])
    check("reply: salary shown rounded", answer_check(["SALARY"], [(Decimal("66000.4"),)], "Name's salary is 66,000.")[0])
    check("reply: wrong number", answer_check(["COUNT"], [(37,)], "There are 38 employees.")[0] is False)
    check("reply: name and salary rows", answer_check(["EMP_NAME", "SALARY"], [("Ali", 50000), ("Sara", 61000)], "Ali (ECODE 1):\n• Salary: 50,000\n\nSara (ECODE 2):\n• Salary: 61,000")[0])
    check("reply: Urdu digits", answer_check(["C"], [(37,)], "تعداد ۳۷ ہے")[0])
    check("reply: date", answer_check(["JOINING_DATE"], [(dt.date(2019, 3, 2),)], "• Joining Date: 2019-03-02 00:00:00")[0])
    check("zero reply", only_zero_numbers("Total employees with that: 0.") and not only_zero_numbers("There are 5."))
    agent_sql = "SELECT e.ECODE, e.EMP_NAME, d.DEPT_NAME AS DEPARTMENT_NAME, g.DESG_NAME AS DESIGNATION_NAME FROM EMPLOYEES e, DEPARTMENTS d, DESIGNATIONS g WHERE e.DEPT_CODE = d.DEPT_CODE AND e.DESG_CODE = g.DESG_CODE AND UPPER(d.DEPT_NAME) = 'IT'"
    check("comma-style tables", sql_tables(agent_sql)[0] == {"EMPLOYEES", "DEPARTMENTS", "DESIGNATIONS"})
    check("comma-style joins", join_check(agent_sql, ["EMPLOYEES.DEPT_CODE=DEPARTMENTS.DEPT_CODE", "EMPLOYEES.DESG_CODE=DESIGNATIONS.DESG_CODE"]))
    check("missing join detected", join_check("SELECT COUNT(*) FROM EMPLOYEES e, DEPARTMENTS d WHERE UPPER(d.DEPT_NAME) = 'IT'", ["EMPLOYEES.DEPT_CODE=DEPARTMENTS.DEPT_CODE"]) is False)
    check("ANSI JOIN also accepted", join_check("SELECT COUNT(*) FROM EMPLOYEES e JOIN DEPARTMENTS d ON d.DEPT_CODE = e.DEPT_CODE", ["EMPLOYEES.DEPT_CODE=DEPARTMENTS.DEPT_CODE"]))
    check("EXTRACT(.. FROM ..) is not a table", sql_tables("SELECT EXTRACT(YEAR FROM e.JOINING_DATE), COUNT(*) FROM EMPLOYEES e GROUP BY EXTRACT(YEAR FROM e.JOINING_DATE)")[0] == {"EMPLOYEES"})
    check("subquery tables found", sql_tables("SELECT e.EMP_NAME FROM EMPLOYEES e WHERE e.SALARY = (SELECT MAX(SALARY) FROM EMPLOYEES)")[0] == {"EMPLOYEES"})
    check("schema check", schema_check(agent_sql, ["EMPLOYEES", "DEPARTMENTS", "DESIGNATIONS"]) and not schema_check(agent_sql, ["EMPLOYEES"]))
    check("select guard", is_safe_select("SELECT 1 FROM DUAL") and not is_safe_select("DELETE FROM EMPLOYEES") and not is_safe_select("SELECT 1 FROM DUAL; DROP TABLE X"))
    check("forbidden word inside a literal is fine for the runner guard", is_safe_select("SELECT 1 FROM EMPLOYEES e WHERE e.EMP_NAME = 'Pinto'"))
    check("WER identical", wer("آئی ٹی میں کتنے ملازمین ہیں؟", "آئی ٹی میں کتنے ملازمین ہیں") == (0, 6))
    check("WER one substitution", wer("a b c d", "a x c d") == (1, 4))
    check("WER ignores diacritics and punctuation", wer("کیا حال ہے۔", "کیا حَال ہے")[0] == 0)
    check("entity alternatives", entities_preserved("آئی ٹی کا مینیجر", [["آئی ٹی", "IT"], ["مینیجر"]]))
    check("percentile", pnr(list(range(1, 21)), 95) == 19)
    check("small-n statistics withheld", timing_stats([1.0] * 5)["mean"] is None and timing_stats([1.0] * 20)["p95"] == 1.0)
    check("placeholder fill escapes quotes only in SQL", fill("{X}", {"X": "O'Neil"}) == "O'Neil" and fill("'{X}'", {"X": "O'Neil"}, sql=True) == "'O''Neil'")
    return 0 if ok else 1


# ------------------------------------------------------------------ main
def main():
    ap = argparse.ArgumentParser(description="Run the thesis evaluation against the FastAPI Research Agent (app.py)")
    ap.add_argument("--config", default="eval_config.json")
    ap.add_argument("--questions", default="thesis_questions.json")
    ap.add_argument("--out-dir", default=".")
    ap.add_argument("--languages", nargs="*", choices=["en", "ro", "ur"])
    ap.add_argument("--categories", nargs="*")
    ap.add_argument("--limit", type=int)
    ap.add_argument("--language-mode", choices=["auto", "selected"])
    ap.add_argument("--include-security", action="store_true")
    ap.add_argument("--ack-readonly-account", action="store_true")
    ap.add_argument("--voice", action="store_true")
    ap.add_argument("--audio-dir", default="audio")
    ap.add_argument("--auth-tests", action="store_true")
    ap.add_argument("--session-tests", action="store_true")
    ap.add_argument("--no-text", action="store_true", help="skip the text retrieval cases (for voice/auth/session only runs)")
    ap.add_argument("--store-rows", type=int, default=5)
    ap.add_argument("--check-gold", action="store_true")
    ap.add_argument("--print-voice-script", action="store_true")
    ap.add_argument("--summarize-only", action="store_true")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args()
    if a.selftest:
        sys.exit(selftest())
    out_json, out_md = os.path.join(a.out_dir, "thesis_results.json"), os.path.join(a.out_dir, "thesis_evaluation_summary.md")
    if a.summarize_only:
        d = json.load(open(out_json, encoding="utf-8"))
        extra = {k: d["metrics"][k] for k in ("database_integrity", "auth_tests", "session_tests") if k in d["metrics"]}
        d["metrics"] = compute_metrics(d["cases"], extra, d["metrics"].get("skipped_cases", []))
        json.dump(d, open(out_json, "w", encoding="utf-8"), ensure_ascii=False, indent=2, default=str)
        open(out_md, "w", encoding="utf-8").write(build_summary(d["meta"], d["cases"], d["metrics"]))
        print("summary rebuilt from", out_json)
        return
    if a.include_security and not a.ack_readonly_account:
        sys.exit("Security cases send destructive-looking requests. Use a SELECT-only Oracle account (see setup_readonly_eval_user.sql) and add --ack-readonly-account.")
    for v in ("ORACLE_USER", "ORACLE_PASSWORD"):
        if v not in os.environ:
            sys.exit(f"Set the environment variable {v} first.")
    cfg = json.load(open(a.config, encoding="utf-8"))
    q, qhash = load_questions(a.questions)
    lang_mode = a.language_mode or cfg["language_mode"]
    oracle = Oracle(cfg)
    params, unresolved = resolve_params(q, oracle)
    limit_rows = cfg["limits"]["max_display_rows"]
    groups = [] if a.no_text else ["retrieval"] + (["security"] if a.include_security else [])
    if a.check_gold:
        groups = ["retrieval", "security"]
    cases = build_cases(q, params, groups, a.languages, a.categories)
    if a.limit:
        cases = cases[:a.limit]
    run, skipped, gold = prepare(cases, params, oracle, limit_rows)
    if a.check_gold:
        print("Resolved parameters (reference values read from your database):")
        for k, v in params.items():
            print(f"  {k} = {v}")
        print("Unresolved:", unresolved or "none")
        seen = set()
        print("\nintent | expected behaviour | gold rows | gold SQL")
        for c in run:
            if c["base_id"] in seen:
                continue
            seen.add(c["base_id"])
            g = gold[c["id"]]
            print(f"{c['base_id']} | {g['behavior']} | {len(g['rows'])} | {(g['sql'] or '-')[:110]}")
        print("\nSkipped:", skipped or "none")
        return
    if a.print_voice_script:
        for v in voice_cases(q, params, a.audio_dir, oracle, limit_rows):
            print(f"{v['intent']['id']}.wav  <-  {v['ref']}")
        return
    user, pwd = os.environ["ORACLE_USER"].strip().upper(), os.environ["ORACLE_PASSWORD"].strip().upper()
    agent = Agent(cfg)
    agent.reset()
    ok, lst, _ = agent.login(user, pwd)
    if not ok:
        sys.exit(f"Login to the agent failed (HTTP {lst}). Check base_url in eval_config.json and your Oracle credentials.")
    agent.logout()
    snap_before = oracle.snapshot() if any(c["group"] == "security" for c in run) else None
    started = dt.datetime.utcnow().isoformat(timespec="seconds")
    results, extra = [], {}
    for i, c in enumerate(run, 1):
        r = run_one(agent, oracle, cfg, c, gold.get(c["id"]), user, pwd, lang_mode, a.store_rows)
        results.append(r)
        print(f"[{i}/{len(run)}] {r['id']} {r.get('elapsed_seconds', 0):.2f}s {r.get('failure_reason') or ('ok' if r.get('behavior_correct') else 'check')}")
        time.sleep(cfg["run"]["sleep_between_cases_seconds"])
    if a.voice:
        full_cases = build_cases(q, params, ["retrieval"], ["ur"], None)
        _, _, g2 = prepare(full_cases, params, oracle, limit_rows)
        gold_by_intent = {c["base_id"]: g2[c["id"]] for c in full_cases if c["id"] in g2}
        results += run_voice(agent, oracle, cfg, voice_cases(q, params, a.audio_dir, oracle, limit_rows), gold_by_intent, user, pwd, a.store_rows)
    if snap_before is not None:
        after = oracle.snapshot()
        extra["database_integrity"] = dict(before=snap_before, after=after, unchanged=(snap_before == after))
    if a.auth_tests:
        extra["auth_tests"] = run_auth_tests(agent, cfg, user, pwd)
    if a.session_tests:
        print("Running inactivity tests (about 2.5 minutes)...")
        extra["session_tests"] = run_session_tests(agent, cfg, user, pwd)
    for r in results:
        r.pop("intent", None)
    meta = dict(run_started_utc=started, base_url=cfg["agent"]["base_url"], questions_file=a.questions, questions_sha256=qhash,
                language_mode=lang_mode, python=platform.python_version(), platform=platform.platform(),
                partial_run=bool(a.limit or a.languages or a.categories or a.no_text), category_names=q.get("category_names", {}),
                params_resolved=params, params_unresolved=unresolved,
                options=dict(include_security=a.include_security, voice=a.voice, store_rows=a.store_rows),
                note="Credentials are read from environment variables and are not stored.")
    M = compute_metrics(results, extra, skipped)
    json.dump(dict(meta=meta, cases=results, metrics=M), open(out_json, "w", encoding="utf-8"), ensure_ascii=False, indent=2, default=str)
    open(out_md, "w", encoding="utf-8").write(build_summary(meta, results, M))
    print("Wrote", out_json, "and", out_md)


if __name__ == "__main__":
    main()
