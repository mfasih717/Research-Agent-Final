# -*- coding: utf-8 -*-
"""
Thesis-aligned automated evaluator for:
"Integrating Urdu Language with AI Agent for Intelligent Database Querying"

Place this file, thesis_questions.json, app.py, schema.py, templates/, static/
and your existing .env in the same project folder.

It measures the thesis-defined categories/metrics without changing app functionality.
"""

import os, re, json, time, sys, math, statistics, getpass
from pathlib import Path
from collections import defaultdict, Counter
from difflib import SequenceMatcher

import oracledb
from dotenv import load_dotenv
from starlette.testclient import TestClient
import app as application

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")

QUESTIONS_FILE = BASE_DIR / "thesis_questions.json"
RESULTS_FILE = BASE_DIR / "thesis_results.json"
SUMMARY_FILE = BASE_DIR / "thesis_evaluation_summary.md"

ORACLE_USER = os.environ.get("ORACLE_USER", "RESEARCH").strip().upper()
ORACLE_PASSWORD = os.environ.get("ORACLE_PASSWORD", "NTU").strip()
ORACLE_DSN = os.environ.get("ORACLE_DSN", "localhost:1521/FREEPDB1").strip()

LOGIN_PATH = "/login"
CHAT_PATH = "/api/chat"
TRANSCRIBE_PATH = "/api/transcribe"

LANG_MAP = {"English":"EN", "Roman Urdu":"UR", "Urdu":"UR"}

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")


def pct(n, d):
    return round(100.0*n/d, 2) if d else None

def percentile(values, p):
    if not values:
        return None
    vals = sorted(values)
    if len(vals) == 1:
        return vals[0]
    k = (len(vals)-1)*(p/100.0)
    f, c = math.floor(k), math.ceil(k)
    if f == c:
        return vals[int(k)]
    return vals[f]*(c-k) + vals[c]*(k-f)

def is_select(sql):
    s = (sql or "").strip().rstrip(";").strip()
    return bool(re.match(r"(?is)^(select|with)\b", s)) and ";" not in s

def oracle_conn():
    return oracledb.connect(user=ORACLE_USER, password=ORACLE_PASSWORD, dsn=ORACLE_DSN)

def run_sql(conn, sql):
    s = (sql or "").strip().rstrip(";")
    if not is_select(s):
        raise ValueError("Not a single read-only SELECT/WITH statement.")
    cur = conn.cursor()
    try:
        cur.execute(s)
        cols = [d[0].upper() for d in cur.description] if cur.description else []
        rows = cur.fetchall()
        norm = [tuple("" if v is None else str(v).strip().upper() for v in row) for row in rows]
        return cols, norm
    finally:
        cur.close()

def sort_rows(rows):
    return sorted(rows, key=lambda r: tuple(str(x) for x in r))

def equivalent_results(gold_cols, gold_rows, gen_cols, gen_rows):
    """
    Thesis 3.4.1 says evaluation should focus on correct information retrieval,
    not exact SQL-string equality. This comparator:
    1) accepts exact row-value equivalence;
    2) if generated SQL returns extra columns, projects it onto gold columns;
    3) for same-width aggregate/grouped outputs, compares values even if aliases differ.
    """
    g_rows = sort_rows(gold_rows)
    r_rows = sort_rows(gen_rows)
    if g_rows == r_rows:
        return True, "exact_result_match"

    # Project generated rows onto gold-named columns when possible.
    if gold_cols and all(c in gen_cols for c in gold_cols):
        idx = [gen_cols.index(c) for c in gold_cols]
        projected = [tuple(row[i] for i in idx) for row in gen_rows]
        if sort_rows(projected) == g_rows:
            return True, "gold_columns_match_generated_projection"

    # Same-width outputs with different aliases (e.g. EMP_COUNT vs TOTAL).
    if gold_rows and gen_rows and all(len(r)==len(gold_rows[0]) for r in gen_rows) and len(gen_rows[0]) == len(gold_rows[0]):
        if sort_rows(gen_rows) == g_rows:
            return True, "same_values_alias_difference"

    # Both empty is valid only because the gold query itself defines the expected empty result.
    if len(gold_rows) == 0 and len(gen_rows) == 0:
        return True, "both_empty_gold_defined"

    return False, "result_mismatch"

def tables_of(sql):
    return {t for t in ("EMPLOYEES","DEPARTMENTS","DESIGNATIONS")
            if re.search(r"\b"+t+r"\b", sql or "", re.I)}

def required_join_paths(sql):
    s = (sql or "").upper()
    req = set()
    if "DEPARTMENTS" in s:
        req.add("DEPT")
    if "DESIGNATIONS" in s:
        req.add("DESG")
    return req

def generated_join_paths(sql):
    s = re.sub(r"\s+", " ", (sql or "").upper())
    got = set()
    if ("E.DEPT_CODE = D.DEPT_CODE" in s or "D.DEPT_CODE = E.DEPT_CODE" in s):
        got.add("DEPT")
    if ("E.DESG_CODE = G.DESG_CODE" in s or "G.DESG_CODE = E.DESG_CODE" in s):
        got.add("DESG")
    return got

def contains_all_answer_values(answer, gold_rows):
    """
    Conservative automatic answer check. For scalar/small outputs, all gold values
    should appear in the user-visible answer. Returns None when the automatic check
    is unsuitable and manual review should be used.
    """
    if not answer:
        return False
    if not gold_rows:
        low = answer.lower()
        return any(x in low for x in ("no result","not found","koi result","record nahi","نہیں","دستیاب نہیں"))
    if len(gold_rows) > 20:
        return None
    text = re.sub(r"[,\s]+", " ", answer.upper()).strip()
    vals = []
    for row in gold_rows:
        for v in row:
            sv = str(v).strip().upper()
            if not sv:
                continue
            # Normalize common Oracle date/time rendering.
            sv = sv.replace("00:00:00","").strip()
            vals.append(sv)
    if not vals:
        return None
    matched = 0
    for v in vals:
        vv = re.sub(r"[,\s]+", " ", v).strip()
        if vv in text or v.replace(",","") in answer.upper().replace(",",""):
            matched += 1
    return matched == len(vals)

class Agent:
    def __init__(self, username, password):
        self.client = TestClient(application.app)
        self.username = username
        self.password = password
        self.login()

    def login(self):
        r = self.client.post(LOGIN_PATH, data={"username":self.username,"password":self.password}, follow_redirects=False)
        if r.status_code not in (200,302,303):
            raise RuntimeError(f"Login failed: HTTP {r.status_code}")

    def ask(self, question, lang="EN", source="typed", retries=5):
        body={"message":question,"language":lang,"source":source,"evaluation_mode":True}
        last = None
        for i in range(retries):
            t0=time.perf_counter()
            r=self.client.post(CHAT_PATH,json=body)
            elapsed=time.perf_counter()-t0
            if r.status_code==401:
                self.login()
                continue
            try:
                data=r.json()
            except Exception:
                data={}
            last=(r.status_code,data,elapsed)
            reply=str(data.get("reply",""))
            if "temporarily busy" in reply.lower():
                time.sleep(6*(i+1))
                continue
            return last
        return last

def find_core_by_intent(core, intent_id, language="Urdu"):
    for q in core:
        if q.get("intent_id")==intent_id and q.get("language")==language:
            return q
    return None

def normalize_wer_text(s):
    s = str(s or "").strip().lower()
    s = re.sub(r"[^\w\u0600-\u06ff]+", " ", s, flags=re.UNICODE)
    return [w for w in s.split() if w]

def edit_distance(a,b):
    dp=list(range(len(b)+1))
    for i,x in enumerate(a,1):
        nd=[i]
        for j,y in enumerate(b,1):
            nd.append(min(nd[-1]+1,dp[j]+1,dp[j-1]+(x!=y)))
        dp=nd
    return dp[-1]

def word_error_rate(ref,hyp):
    r,h=normalize_wer_text(ref),normalize_wer_text(hyp)
    if not r:
        return None
    return 100.0*edit_distance(r,h)/len(r)

def voice_file_mime(path):
    ext=path.suffix.lower()
    return {".wav":"audio/wav",".webm":"audio/webm",".mp3":"audio/mpeg",".m4a":"audio/mp4"}.get(ext,"application/octet-stream")

def build_dynamic_diagnostics(conn, diagnostics):
    out=[]
    for d in diagnostics:
        item=dict(d)
        if not d.get("dynamic"):
            out.append(item); continue
        cur=conn.cursor()
        try:
            if d["purpose"]=="duplicate_names":
                cur.execute("""SELECT EMP_NAME, COUNT(*) c FROM EMPLOYEES GROUP BY EMP_NAME HAVING COUNT(*)>1 FETCH FIRST 1 ROW ONLY""")
                row=cur.fetchone()
                if row:
                    item["question"]=f"Show employee details for {row[0]}."
                    item["resolved_value"]=str(row[0])
                    item["applicable"]=True
                else:
                    item["applicable"]=False
            elif d["purpose"]=="null_value":
                # Try optional fields in a deterministic order.
                found=None
                for col in ("EMAIL","PHONE_NUMBER","ADDRESS","CITY"):
                    cur.execute(f"SELECT ECODE, {col} FROM EMPLOYEES WHERE {col} IS NULL FETCH FIRST 1 ROW ONLY")
                    row=cur.fetchone()
                    if row:
                        found=(row[0],col); break
                if found:
                    item["question"]=f"Show the {found[1].lower().replace('_',' ')} of employee {found[0]}."
                    item["resolved_value"]={"ecode":str(found[0]),"field":found[1]}
                    item["applicable"]=True
                else:
                    item["applicable"]=False
        finally:
            cur.close()
        out.append(item)
    return out

def main():
    global ORACLE_PASSWORD
    if not QUESTIONS_FILE.exists():
        raise SystemExit(f"Missing {QUESTIONS_FILE.name}")
    if not ORACLE_PASSWORD:
        ORACLE_PASSWORD = "NTU"

    Q=json.loads(QUESTIONS_FILE.read_text(encoding="utf-8"))
    core=Q.get("core",[])
    safety=Q.get("safety",[])
    voice=Q.get("voice",[])
    diagnostics=Q.get("diagnostics",[])

    print(f"Loaded {len(core)} core, {len(safety)} safety, {len(voice)} voice-manifest cases.")
    conn=oracle_conn()
    agent=Agent(ORACLE_USER, ORACLE_PASSWORD)

    results={"metadata":{
        "timestamp":time.strftime("%Y-%m-%d %H:%M:%S"),
        "oracle_user":ORACLE_USER,
        "oracle_dsn":ORACLE_DSN,
        "core_cases":len(core),
        "safety_cases":len(safety),
        "design":"Thesis-aligned paired English/Roman Urdu/Urdu evaluation"
    },"core_results":[],"safety_results":[],"voice_results":[],"diagnostic_results":[],"session_auth_results":[]}

    # ---------- CORE ----------
    print("\n=== CORE THESIS EVALUATION ===")
    for i,q in enumerate(core,1):
        gcols,grows=run_sql(conn,q["gold_sql"])
        code,data,dt=agent.ask(q["question"], LANG_MAP.get(q["language"],"EN"))
        reply=str(data.get("reply","") or "")
        gsql=str(data.get("sql","") or "").strip()
        gen_ok=False; gen_error=""; xcols=[]; xrows=[]
        if gsql and is_select(gsql):
            try:
                xcols,xrows=run_sql(conn,gsql); gen_ok=True
            except Exception as e:
                gen_error=str(e)
        elif gsql:
            gen_error="Non-read-only SQL returned"

        ex,match_mode=(equivalent_results(gcols,grows,xcols,xrows) if gen_ok else (False,"no_executable_sql"))
        schema_ok=tables_of(q["gold_sql"]).issubset(tables_of(gsql)) if gsql else False
        req_join=required_join_paths(q["gold_sql"])
        join_ok=(req_join.issubset(generated_join_paths(gsql)) if req_join else None)
        aa_auto=contains_all_answer_values(reply,grows) if ex else False
        # For >20-row grouping results, execution equivalence is the reliable answer correctness basis;
        # the UI may legitimately format many group rows, so preserve manual-review flag.
        aa = ex if aa_auto is None else bool(ex and aa_auto)

        entry={
            **q,"status_code":code,"generated_sql":gsql,"gold_rows":len(grows),
            "generated_rows":len(xrows) if gen_ok else 0,
            "execution_success":bool(gen_ok),"execution_accuracy":bool(ex),
            "result_match_mode":match_mode,"answer_accuracy":bool(aa),
            "answer_auto_check":aa_auto,"schema_accuracy":bool(schema_ok),
            "join_accuracy":join_ok,"response_time_sec":round(dt,3),
            "reply":reply,"error":gen_error
        }
        results["core_results"].append(entry)
        print(f"[{i:02d}/{len(core)}] {q['id']} | {q['category']:<23} | {q['language']:<11} | EX={int(ex)} AA={int(aa)} | {dt:.2f}s")
        time.sleep(1.0)

    # ---------- SAFETY / DOMAIN / FALSE-REJECTION ----------
    print("\n=== SECURITY / DOMAIN TESTS ===")
    for s in safety:
        code,data,dt=agent.ask(s["question"],"EN")
        reply=str(data.get("reply","") or "")
        sql=str(data.get("sql","") or "").strip()
        safe_select=bool(sql and is_select(sql))
        exp=s["expected"]
        if exp=="block":
            passed=not sql
        elif exp=="redirect":
            passed=not sql
        elif exp=="chat":
            passed=not sql and bool(reply.strip())
        elif exp=="allow":
            passed=safe_select
        else:
            passed=False
        e={**s,"passed":passed,"generated_sql":sql,"reply":reply,"response_time_sec":round(dt,3)}
        results["safety_results"].append(e)
        print(f"{s['id']} {exp}: {'PASS' if passed else 'FAIL'}")

    # ---------- VOICE ----------
    print("\n=== VOICE TESTS (only when audio files exist) ===")
    for v in voice:
        p=BASE_DIR / v["audio_file"]
        q=find_core_by_intent(core,v["core_intent_id"],"Urdu")
        if not p.exists() or not q:
            results["voice_results"].append({**v,"measured":False,"reason":"Audio file not present" if not p.exists() else "Paired core case not found"})
            continue
        t0=time.perf_counter()
        with open(p,"rb") as f:
            rr=agent.client.post(TRANSCRIBE_PATH,files={"audio":(p.name,f,voice_file_mime(p))})
        try:
            tdata=rr.json()
        except Exception:
            tdata={}
        transcript=str(tdata.get("transcript","") or "")
        code,data,chat_dt=agent.ask(transcript,"UR",source="voice")
        total_dt=time.perf_counter()-t0
        reply=str(data.get("reply","") or "")
        gsql=str(data.get("sql","") or "").strip()
        gcols,grows=run_sql(conn,q["gold_sql"])
        ex=False; xcols=[]; xrows=[]; err=""
        if gsql and is_select(gsql):
            try:
                xcols,xrows=run_sql(conn,gsql)
                ex,_=equivalent_results(gcols,grows,xcols,xrows)
            except Exception as e:
                err=str(e)
        wer=word_error_rate(v["reference_text"],transcript)
        trans_norm=" ".join(normalize_wer_text(transcript))
        entity_ok=all(" ".join(normalize_wer_text(ent)) in trans_norm for ent in v.get("entities",[]) if ent)
        results["voice_results"].append({
            **v,"measured":True,"transcript":transcript,"wer_pct":round(wer,2) if wer is not None else None,
            "entity_preservation":entity_ok,"voice_query_success":bool(ex),
            "final_reply":reply,"generated_sql":gsql,"voice_processing_latency_sec":round(total_dt,3),"error":err
        })

    # ---------- THESIS-REQUIRED EDGE DIAGNOSTICS ----------
    print("\n=== EDGE DIAGNOSTICS ===")
    dyn=build_dynamic_diagnostics(conn,diagnostics)
    for d in dyn:
        if d.get("applicable") is False:
            results["diagnostic_results"].append({**d,"measured":False,"reason":"No applicable row found in current database"})
            continue
        q=d.get("question","")
        code,data,dt=agent.ask(q,"EN")
        results["diagnostic_results"].append({
            **d,"measured":True,"reply":str(data.get("reply","") or ""),
            "generated_sql":str(data.get("sql","") or ""),"response_time_sec":round(dt,3),
            "manual_review_required":True
        })

    # ---------- AUTH / SESSION ----------
    print("\n=== AUTHENTICATION / SESSION ===")
    # Invalid username: avoids repeated bad-password attempts on RESEARCH user.
    bad_client=TestClient(application.app)
    bad_user="THESIS_NO_SUCH_USER_92741"
    t0=time.perf_counter()
    bad=bad_client.post(LOGIN_PATH,data={"username":bad_user,"password":"invalid"},follow_redirects=False)
    results["session_auth_results"].append({
        "test":"invalid_credentials_rejected","passed":bad.status_code != 302 and ("invalid" in bad.text.lower() or "error" in bad.text.lower() or bad.status_code == 401),
        "http_status":bad.status_code,"elapsed_sec":round(time.perf_counter()-t0,3)
    })

    # Verify configured timeout value from running app.
    configured=getattr(application,"SESSION_INACTIVITY_TIMEOUT",None)
    results["session_auth_results"].append({
        "test":"configured_inactivity_timeout","configured_seconds":configured,"passed":configured==60
    })

    # One real expiry test (~61 s) to verify API rejection after inactivity.
    if configured == 60:
        sess=Agent(ORACLE_USER,ORACLE_PASSWORD)
        time.sleep(61.2)
        r=sess.client.post(CHAT_PATH,json={"message":"How many employees are there?","language":"EN","evaluation_mode":True})
        try: rd=r.json()
        except Exception: rd={}
        results["session_auth_results"].append({
            "test":"one_minute_inactivity_expiry","passed":r.status_code==401 and rd.get("error")=="session_expired",
            "http_status":r.status_code,"response":rd
        })

    # ---------- SUMMARY ----------
    cr=results["core_results"]
    total=len(cr)
    execution_success=sum(x["execution_success"] for x in cr)
    ex=sum(x["execution_accuracy"] for x in cr)
    aa=sum(x["answer_accuracy"] for x in cr)
    ssa=sum(x["schema_accuracy"] for x in cr)
    join_cases=[x for x in cr if x["join_accuracy"] is not None]
    join_correct=sum(x["join_accuracy"] is True for x in join_cases)
    times=[x["response_time_sec"] for x in cr]

    categories={}
    for cat in sorted({x["category"] for x in cr}):
        ss=[x for x in cr if x["category"]==cat]
        categories[cat]={
            "attempted":len(ss),
            "execution_correct":sum(x["execution_accuracy"] for x in ss),
            "execution_accuracy_pct":pct(sum(x["execution_accuracy"] for x in ss),len(ss)),
            "answer_correct":sum(x["answer_accuracy"] for x in ss),
            "answer_accuracy_pct":pct(sum(x["answer_accuracy"] for x in ss),len(ss)),
            "mean_sec":round(statistics.mean(x["response_time_sec"] for x in ss),3)
        }

    languages={}
    for lang in ("Urdu","Roman Urdu","English"):
        ss=[x for x in cr if x["language"]==lang]
        languages[lang]={
            "attempted":len(ss),
            "execution_accuracy_pct":pct(sum(x["execution_accuracy"] for x in ss),len(ss)),
            "answer_accuracy_pct":pct(sum(x["answer_accuracy"] for x in ss),len(ss)),
            "mean_sec":round(statistics.mean(x["response_time_sec"] for x in ss),3)
        }

    groups={
        "Direct retrieval":["Direct retrieval"],
        "Relational JOIN":["Department relation","Designation relation","Multiple relations"],
        "Aggregate/grouped":["Aggregation","Grouping","Comparison and ranking"],
        "Date/multiple filters":["Date filtering","Multiple conditions"],
    }
    response_groups={}
    for name,cats in groups.items():
        ts=[x["response_time_sec"] for x in cr if x["category"] in cats]
        response_groups[name]={
            "timed_attempts":len(ts),
            "mean_sec":round(statistics.mean(ts),3) if ts else None,
            "median_sec":round(statistics.median(ts),3) if ts else None,
            "p95_sec":round(percentile(ts,95),3) if ts else None
        }
    vmeas=[x for x in results["voice_results"] if x.get("measured")]
    if vmeas:
        vts=[x["voice_processing_latency_sec"] for x in vmeas]
        response_groups["Urdu voice"]={
            "timed_attempts":len(vts),"mean_sec":round(statistics.mean(vts),3),
            "median_sec":round(statistics.median(vts),3),"p95_sec":round(percentile(vts,95),3)
        }
    else:
        response_groups["Urdu voice"]={"timed_attempts":0,"mean_sec":None,"median_sec":None,"p95_sec":None}

    unsafe=[x for x in results["safety_results"] if x["kind"]=="unsafe"]
    benign=[x for x in results["safety_results"] if x["kind"]=="benign_keyword"]
    voice_summary={
        "attempted":len(vmeas),
        "mean_wer_pct":round(statistics.mean(x["wer_pct"] for x in vmeas if x.get("wer_pct") is not None),2) if any(x.get("wer_pct") is not None for x in vmeas) else None,
        "entity_preservation_pct":pct(sum(x.get("entity_preservation") is True for x in vmeas),len(vmeas)) if vmeas else None,
        "voice_query_success_pct":pct(sum(x.get("voice_query_success") is True for x in vmeas),len(vmeas)) if vmeas else None,
    }

    results["summary"]={
        "overall":{
            "attempted":total,
            "sql_execution_success_rate_pct":pct(execution_success,total),
            "execution_accuracy_pct":pct(ex,total),
            "answer_accuracy_pct":pct(aa,total),
            "schema_accuracy_pct":pct(ssa,total),
            "join_accuracy_pct":pct(join_correct,len(join_cases)),
            "mean_response_time_sec":round(statistics.mean(times),3),
            "median_response_time_sec":round(statistics.median(times),3),
            "p95_response_time_sec":round(percentile(times,95),3),
        },
        "categories":categories,
        "languages":languages,
        "security":{
            "unsafe_attempts":len(unsafe),
            "unsafe_correctly_blocked":sum(x["passed"] for x in unsafe),
            "security_rejection_rate_pct":pct(sum(x["passed"] for x in unsafe),len(unsafe)),
            "benign_keyword_attempts":len(benign),
            "benign_keyword_allowed":sum(x["passed"] for x in benign),
            "benign_false_rejection_rate_pct":pct(len(benign)-sum(x["passed"] for x in benign),len(benign))
        },
        "voice":voice_summary,
        "response_time_groups":response_groups,
        "session_authentication":results["session_auth_results"]
    }

    RESULTS_FILE.write_text(json.dumps(results,ensure_ascii=False,indent=2),encoding="utf-8")

    # Human-readable thesis mapping summary.
    lines=[]
    s=results["summary"]
    lines += [
        "# Thesis Evaluation Summary",
        "",
        "## Table 3.4 / Overall Metrics",
        f"- Attempted retrieval requests (N): {total}",
        f"- Execution success rate: {s['overall']['sql_execution_success_rate_pct']}%",
        f"- Execution Accuracy: {s['overall']['execution_accuracy_pct']}%",
        f"- Answer Accuracy: {s['overall']['answer_accuracy_pct']}%",
        f"- Schema accuracy: {s['overall']['schema_accuracy_pct']}%",
        f"- JOIN Accuracy: {s['overall']['join_accuracy_pct']}%",
        f"- Mean response time: {s['overall']['mean_response_time_sec']} s",
        f"- Median response time: {s['overall']['median_response_time_sec']} s",
        f"- P95 response time: {s['overall']['p95_response_time_sec']} s",
        "",
        "## Table 4.4 / Query Category Results",
        "| Category | Attempted N | Execution Accuracy | Answer Accuracy |",
        "|---|---:|---:|---:|",
    ]
    for cat,st in categories.items():
        lines.append(f"| {cat} | {st['attempted']} | {st['execution_correct']}/{st['attempted']} ({st['execution_accuracy_pct']}%) | {st['answer_correct']}/{st['attempted']} ({st['answer_accuracy_pct']}%) |")
    lines += ["","## Table 4.5 / Language Results","| Language | Attempted | Execution Accuracy | Answer Accuracy |","|---|---:|---:|---:|"]
    for lang,st in languages.items():
        lines.append(f"| {lang} | {st['attempted']} | {st['execution_accuracy_pct']}% | {st['answer_accuracy_pct']}% |")
    lines += [
        "",
        "## Section 4.7 / JOIN Evaluation",
        f"- JOIN Accuracy: {s['overall']['join_accuracy_pct']}%",
        "",
        "## Table 4.7 / Urdu Voice Evaluation",
        f"- Attempted voice cases: {voice_summary['attempted']}",
        f"- Mean WER: {voice_summary['mean_wer_pct'] if voice_summary['mean_wer_pct'] is not None else 'Not measured (audio files missing)'}",
        f"- Entity preservation: {voice_summary['entity_preservation_pct'] if voice_summary['entity_preservation_pct'] is not None else 'Not measured (audio files missing)'}",
        f"- Voice query success: {voice_summary['voice_query_success_pct'] if voice_summary['voice_query_success_pct'] is not None else 'Not measured (audio files missing)'}",
        "",
        "## Table 4.8 / Security and Session",
        f"- Security Rejection Rate: {s['security']['security_rejection_rate_pct']}%",
        f"- Benign false-rejection rate: {s['security']['benign_false_rejection_rate_pct']}%",
    ]
    for x in results["session_auth_results"]:
        lines.append(f"- {x['test']}: {'PASS' if x.get('passed') else 'FAIL'}")
    lines += ["","## Table 4.9 / Response Time","| Request group | Timed attempts | Mean | Median | P95 |","|---|---:|---:|---:|---:|"]
    for grp,st in response_groups.items():
        lines.append(f"| {grp} | {st['timed_attempts']} | {st['mean_sec'] if st['mean_sec'] is not None else 'Not measured'} | {st['median_sec'] if st['median_sec'] is not None else 'Not measured'} | {st['p95_sec'] if st['p95_sec'] is not None else 'Not measured'} |")
    lines += [
        "",
        "## Notes",
        "- SQL Generation Correctness in Table 3.4 still requires researcher/expert semantic review of generated SQL; the runner records every generated SQL for that review.",
        "- Diagnostics for ambiguity, spelling variants, duplicate names and NULL values are reported separately and are not mixed into the primary paired-language accuracy denominator.",
        "- If voice audio files V01.wav–V09.wav are absent, voice metrics remain unmeasured rather than being fabricated.",
    ]
    SUMMARY_FILE.write_text("\n".join(lines),encoding="utf-8")

    conn.close()
    print(f"\nSaved: {RESULTS_FILE}")
    print(f"Saved: {SUMMARY_FILE}")

if __name__=="__main__":
    main()
