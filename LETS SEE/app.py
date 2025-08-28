import os
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from flask import (
    Flask, render_template, request, redirect,
    url_for, session, send_file, flash, make_response
)
from dotenv import load_dotenv
import pandas as pd
from supabase import create_client, Client
import csv
from io import StringIO

# -------------------- ENV & FLASK --------------------
load_dotenv()
APP_SECRET = os.getenv("FLASK_SECRET", "devkey")

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")
if not SUPABASE_URL or not SUPABASE_KEY:
    raise RuntimeError("Missing SUPABASE_URL / SUPABASE_KEY in .env")

app = Flask(__name__, template_folder="templates", static_folder="static")
app.secret_key = APP_SECRET

# Supabase client
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

# Optional Excel seed file (single sheet with S.NO, NAME, DEPT, SEC)
DATA_FILE = os.path.join("data", "FIRST YEARS.xlsx")

# -------------------- CONSTANTS --------------------
JARGON_OPTIONS = ["MTI", "NI OV IMP", "NI SL IMP", "FL", "ST", "STM", "FLG", "FLGB", "CFG", "NV"]

LEVEL_SCORE = {"C": 0, "B": 1, "A": 2}

DIMENSIONS = [
    ("fluency",     [("FLOW_MED","B"), ("FLOW_GOOD","A"), ("FLOW_REFINED","A")]),
    ("stammering",  [("STAMMERING_CONT","C"), ("STAMMERING_MED","B"), ("STAMMER_FREE","A")]),
    ("stuck",       [("STUCK_OFTEN","C"), ("STUCK_FEW","B")]),
    ("mti",         [("MTI_STRONG","C"), ("MTI_SLIGHT","B"), ("MTI_NEARZERO","A")]),
    ("confidence",  [("CONF_PANIC","C"), ("CONF_OK","B"), ("CONF_IMMACULATE","A")]),
    ("improvement", [("NI_OV_IMP","C"), ("NI_SL_IMP","B")]),
    ("filler",      [("FL","B")]),
    ("nervous",     [("NV","B")]),
]

# -------------------- HELPERS --------------------
def format_time(value):
    if not value:
        return ""
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value)
        except Exception:
            return value
    return value.strftime("%d-%m-%Y %I:%M %p")

app.jinja_env.globals.update(format_time=format_time)

def is_admin():
    return session.get("role") == "admin"

def is_mentor():
    return session.get("role") == "mentor"

def login_required(role=None):
    def wrapper(fn):
        def inner(*args, **kwargs):
            if "user" not in session:
                return redirect(url_for("login"))
            if role and session.get("role") != role:
                return redirect(url_for("login"))
            return fn(*args, **kwargs)
        inner.__name__ = fn.__name__
        return inner
    return wrapper

def compute_category(selected_codes: dict) -> str | None:
    if not selected_codes:
        return None
    scores = []
    for dim, options in DIMENSIONS:
        code = selected_codes.get(dim)
        if not code:
            continue
        level = next((lvl for c, lvl in options if c == code), None)
        if level:
            scores.append(LEVEL_SCORE[level])
    if not scores:
        return None
    avg = sum(scores) / len(scores)
    if avg < 0.7:
        return "C"
    elif avg < 1.5:
        return "B"
    else:
        return "A"

def sanitize(val):
    if val is None:
        return "-"
    s = str(val).strip()
    if s.lower() in ("nan", "none", ""):
        return "-"
    return s

def to_ist_human(ts_iso: str | None) -> str:
    if not ts_iso:
        return "-"
    try:
        dt = datetime.fromisoformat(str(ts_iso).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        ist = dt.astimezone(ZoneInfo("Asia/Kolkata"))
        return ist.strftime("%d-%b-%Y %I:%M %p IST")
    except Exception:
        return str(ts_iso)

def natural_roll_key(roll):
    if roll is None:
        return (1, "ZZZZ")
    s = str(roll).strip()
    if s.isdigit():
        try:
            return (0, int(s))
        except Exception:
            return (0, s)
    num = ""
    for ch in s:
        if ch.isdigit():
            num += ch
        else:
            break
    if num:
        try:
            return (0, int(num), s)
        except Exception:
            pass
    return (0, s)

def apply_status_filter(query, status: str | None):
    if status == "completed":
        query = query.in_("category", ["A", "B", "C"])
    elif status == "pending":
        query = query.is_("category", "null")
    return query

def normalize_filters(args):
    f = {
        "dept": (args.get("dept") or "").strip().upper(),
        "sec": (args.get("sec") or "").strip().upper(),
        "category": (args.get("category") or "").strip().upper(),
        "status": (args.get("status") or "").strip().lower(),
        "q": (args.get("q") or "").strip(),
    }
    for k, v in list(f.items()):
        if v == "":
            f[k] = None
    return f

def apply_filters(q, f):
    if f.get("dept"):
        q = q.ilike("dept", f"%{f['dept']}%")
    if f.get("sec"):
        q = q.ilike("sec", f"%{f['sec']}%")
    if f.get("category"):
        q = q.eq("category", f["category"])
    if f.get("status"):
        q = apply_status_filter(q, f["status"])
    if f.get("q"):
        like = f"%{f['q']}%"
        q = q.or_(f"name.ilike.{like},roll.ilike.{like}")
    return q

def fetch_students(filters):
    base = supabase.table("students").select("*")
    # normalize
    f = normalize_filters(filters)
    base = apply_filters(base, f)
    PAGE = 1000
    start, rows = 0, []
    while True:
        chunk = base.range(start, start + PAGE - 1).execute().data or []
        rows.extend(chunk)
        if len(chunk) < PAGE:
            break
        start += PAGE
    rows.sort(key=lambda r: (
        (r.get("dept") or "").strip().upper(),
        (r.get("sec")  or "").strip().upper(),
        natural_roll_key(r.get("roll"))
    ))
    return rows

def load_students_if_empty():
    try:
        count = supabase.table("students").select("id", count="exact").execute().count or 0
    except Exception:
        count = 0
    if count > 0:
        return
    if not os.path.exists(DATA_FILE):
        print(f"[seed] Excel not found: {DATA_FILE} (skipping seed)")
        return
    df = pd.read_excel(DATA_FILE)
    rename = {}
    for c in df.columns:
        u = str(c).strip().upper()
        if u in {"S.NO", "SNO", "S NO", "ROLL", "ROLL NO", "S_NO"}:
            rename[c] = "roll"
        elif u == "NAME":
            rename[c] = "name"
        elif u == "DEPT":
            rename[c] = "dept"
        elif u in {"SEC", "SECTION", "CLASS", "CLASS_NAME"}:
            rename[c] = "sec"
    df = df.rename(columns=rename)
    keep = [c for c in ["roll", "name", "dept", "sec"] if c in df.columns]
    df = df[keep].dropna(subset=["roll", "name"])
    records = []
    for _, r in df.iterrows():
        records.append({
            "roll": str(r.get("roll")).strip(),
            "name": str(r.get("name")).strip(),
            "dept": str(r.get("dept") if pd.notna(r.get("dept")) else ""),
            "sec": str(r.get("sec") if pd.notna(r.get("sec")) else ""),
        })
    if records:
        supabase.table("students").insert(records).execute()
        print(f"[seed] Inserted {len(records)} students from Excel")

def get_depts_secs():
    PAGE = 1000
    start = 0
    depts_set = set()
    secs_set  = set()
    def norm(v):
        return (str(v) if v is not None else "").replace("\u00A0", " ").strip().upper()
    while True:
        res = supabase.table("students").select("dept,sec").range(start, start + PAGE - 1).execute()
        chunk = res.data or []
        for r in chunk:
            d = norm(r.get("dept"))
            s = norm(r.get("sec"))
            if d: depts_set.add(d)
            if s: secs_set.add(s)
        if len(chunk) < PAGE:
            break
        start += PAGE
    depts = sorted(depts_set)
    secs  = sorted(secs_set)
    return depts, secs

def kpi_counts(filters):
    f = normalize_filters(filters)
    base = supabase.table("students")

    # total matching subset
    total = apply_filters(base.select("id", count="exact"), f).execute().count or 0

    # category counts on the same filtered subset
    a = apply_filters(base.select("id", count="exact").eq("category", "A"), f).execute().count or 0
    b = apply_filters(base.select("id", count="exact").eq("category", "B"), f).execute().count or 0
    c = apply_filters(base.select("id", count="exact").eq("category", "C"), f).execute().count or 0

    completed = a + b + c
    pending = max(total - completed, 0)
    return {"total": total, "a": a, "b": b, "c": c, "completed": completed, "pending": pending}


# -------------------- ROUTES --------------------
@app.route("/", methods=["GET"])
def home():
    return redirect(url_for('login'))
    
@app.route("/health")
def health(): 
    return "ok", 200

@app.route("/login", methods=["GET", "POST"])
def login():
    # If already authenticated, redirect to respective dashboard
    if "user" in session and session.get("role") in ("admin","mentor"):
        return redirect(url_for("admin_dashboard" if session["role"] == "admin" else "mentor_dashboard"))

    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "").strip()

        # Admin (hardcoded)
        if username == "ADMIN_MENTORING" and password == "MENTORING123":
            session["user"] = username
            session["role"] = "admin"
            return redirect(url_for("admin_dashboard"))

        # Mentor (Supabase table)
        res = supabase.table("mentors").select("*").eq("username", username).execute()
        if res.data:
            mentor = res.data[0]
            if mentor.get("password") == password:
                session["user"] = mentor["username"]
                session["role"] = "mentor"
                return redirect(url_for("mentor_dashboard"))

        flash("Invalid credentials", "error")
    return render_template("login.html")


@app.route("/change_password", methods=["GET", "POST"])
@login_required(role="mentor")
def change_password():
    if request.method == "POST":
        old_pw = request.form.get("old_password", "").strip()
        new_pw = request.form.get("new_password", "").strip()
        confirm_pw = request.form.get("confirm_password", "").strip()

        res = supabase.table("mentors").select("*").eq("username", session["user"]).single().execute()
        mentor = res.data
        if not mentor:
            flash("Mentor not found", "error")
            return redirect(url_for("change_password"))

        if mentor.get("password") != old_pw:
            flash("Old password is incorrect", "error")
        elif new_pw != confirm_pw:
            flash("New passwords do not match", "error")
        elif not new_pw:
            flash("Password cannot be empty", "error")
        else:
            supabase.table("mentors").update({"password": new_pw}).eq("username", session["user"]).execute()
            flash("Password changed successfully", "success")
            return redirect(url_for("mentor_dashboard"))

    return render_template("change_password.html")

@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))

@app.route("/admin", methods=["GET"])
@login_required(role="admin")
def admin_dashboard():
    load_students_if_empty()
    filters = {
        "dept": request.args.get("dept") or None,
        "sec": request.args.get("sec") or None,
        "category": request.args.get("category") or None,
        "status": request.args.get("status") or None,
        "q": request.args.get("q") or None,
    }
    # Filtered rows and filtered KPIs
    students = fetch_students(filters)
    kpis = kpi_counts(filters)
    depts, secs = get_depts_secs()
    for s in students:
        s["_saved_at_hr"] = to_ist_human(s.get("saved_at"))
    return render_template(
        "admin_dashboard.html",
        students=students,
        filters=normalize_filters(filters),
        kpis=kpis,
        depts=depts,
        secs=secs,
        sanitize=sanitize
    )

@app.route("/mentor", methods=["GET"])
@login_required(role="mentor")
def mentor_dashboard():
    load_students_if_empty()
    status = request.args.get("status") or "pending"
    filters = {
        "dept": request.args.get("dept") or None,
        "sec": request.args.get("sec") or None,
        "category": request.args.get("category") or None,
        "status": status,
        "q": request.args.get("q") or None,
    }
    students = fetch_students(filters)
    depts, secs = get_depts_secs()
    for s in students:
        s["_saved_at_hr"] = to_ist_human(s.get("saved_at"))
    return render_template(
        "mentor_dashboard.html",
        students=students,
        filters=normalize_filters(filters),
        depts=depts,
        secs=secs,
        sanitize=sanitize
    )

@app.route("/student/<int:student_id>", methods=["GET", "POST"])
@login_required()
def student_detail(student_id: int):
    res = supabase.table("students").select("*").eq("id", student_id).single().execute()
    student = res.data
    if not student:
        flash("Student not found", "error")
        return redirect(url_for("admin_dashboard" if is_admin() else "mentor_dashboard"))

    if request.method == "POST":
        update_data = {
            "fluency": request.form.get("fluency"),
            "stammering": request.form.get("stammering"),
            "stuck": request.form.get("stuck"),
            "mti": request.form.get("mti"),
            "confidence": request.form.get("confidence"),
            # DO NOT update 'remarks' here; remarks are handled in /save_assessment
            "mentor": session.get("user"),
            "updated_at": datetime.utcnow().isoformat()
        }
        supabase.table("students").update(update_data).eq("id", student_id).execute()
        flash("Student record updated successfully!", "success")
        return redirect(url_for("student_detail", student_id=student_id))

    student["_saved_at_hr"] = to_ist_human(student.get("saved_at"))
    raw_remarks = student.get("remarks") or ""
    remark_tags = [t for t in [r.strip() for r in raw_remarks.split(",")] if t]

    return render_template(
        "student_details.html",
        student=student,
        dims=DIMENSIONS,
        jargon_options=JARGON_OPTIONS,
        remark_tags=remark_tags,
        sanitize=sanitize,
        is_admin=is_admin(),
        is_mentor=is_mentor()
    )

@app.route("/save_assessment", methods=["POST"])
@login_required(role="mentor")
def save_assessment():
    student_id = request.form.get("student_id")
    if not student_id:
        flash("Missing student id", "error")
        return redirect(url_for("mentor_dashboard"))

    # 1) Gather current selections in a stable order
    order = ["fluency","stammering","stuck","mti","confidence","improvement","filler","nervous"]
    selected = {}
    for dim in order:
        val = request.form.get(dim)
        if val:
            selected[dim] = val

    # 2) Compute category based on current selections
    cat = compute_category(selected)

    # 3) Get the current textarea content (mentor sees existing remarks and edits it)
    typed_raw = (request.form.get("remarks") or "").strip()
    # Normalize if the mentor leaves "MENTOR REVIEW:" in the box
    typed = typed_raw
    if typed_raw.upper().startswith("MENTOR REVIEW:"):
        typed = typed_raw.split(":", 1)[1].strip()

    # 4) Build fresh remarks (REPLACE behavior every save)
    codes_line = ",".join(f"{k.upper()}:{v}" for k, v in selected.items()) if selected else ""
    mentor_line = f"MENTOR REVIEW:{typed}" if typed else ""
    remarks_out = "\n".join([x for x in (codes_line, mentor_line) if x]).strip()

    # 5) Write everything in one update (overwrite remarks)
    update_payload = {
        "fluency": selected.get("fluency"),
        "stammering": selected.get("stammering"),
        "stuck": selected.get("stuck"),
        "mti": selected.get("mti"),
        "confidence": selected.get("confidence"),
        "improvement": selected.get("improvement"),
        "filler": selected.get("filler"),
        "nervous": selected.get("nervous"),
        "category": cat,
        "remarks": remarks_out,  # overwrite with fresh content
        "saved_at": datetime.utcnow().isoformat(),
        "saved_by": session.get("user"),
        "mentor": session.get("user"),
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }

    supabase.table("students").update(update_payload).eq("id", student_id).execute()
    return redirect(url_for("student_detail", student_id=student_id))


from io import BytesIO
from openpyxl.chart import PieChart, Reference

@app.route("/export_excel", methods=["GET"])
@login_required(role="admin")
def export_excel():
    filters = {
        "dept": request.args.get("dept") or None,
        "sec": request.args.get("sec") or None,
        "category": request.args.get("category") or None,
        "status": request.args.get("status") or None,
        "q": request.args.get("q") or None,
    }
    data = fetch_students(filters)
    if not data:
        flash("No data to export with current filters.", "error")
        return redirect(url_for("admin_dashboard"))

    # Human-readable timestamp
    for s in data:
        s["saved_at_ist"] = to_ist_human(s.get("saved_at"))

    cols = ["roll","name","dept","sec","category","saved_by","saved_at_ist","remarks"]
    present_cols = [c for c in cols if c in (data[0].keys() if data else [])]
    df = pd.DataFrame(data)
    if not present_cols:
        present_cols = list(df.columns)
    df = df[present_cols]

    # Natural sort
    if "roll" in df.columns:
        df["_roll_key"] = df["roll"].apply(natural_roll_key)
        sort_keys = [c for c in ["dept","sec","_roll_key"] if c in df.columns]
        if sort_keys:
            df = df.sort_values(by=sort_keys, kind="mergesort")
        if "_roll_key" in df.columns:
            df = df.drop(columns=["_roll_key"])

    # A/B/C counts from filtered subset
    cat_series = df["category"].fillna("").str.strip().str.upper() if "category" in df.columns else pd.Series(dtype=str)
    a_count = int((cat_series == "A").sum())
    b_count = int((cat_series == "B").sum())
    c_count = int((cat_series == "C").sum())

    # Excel in memory
    output = BytesIO()
    with pd.ExcelWriter(output, engine="openpyxl") as writer:
        # Sheet 1: data
        df.to_excel(writer, index=False, sheet_name="Students")

        # Sheet 2: Summary table (for chart source)
        summary_df = pd.DataFrame({"Category": ["A","B","C"], "Count": [a_count, b_count, c_count]})
        summary_df.to_excel(writer, index=False, sheet_name="Summary", startrow=0, startcol=0)

        wb = writer.book
        ws = writer.sheets["Summary"]

        # Single pie chart for A/B/C
        pie = PieChart()
        pie.title = "Category Distribution (A/B/C)"
        labels = Reference(ws, min_col=1, min_row=2, max_row=4)  # A,B,C
        data   = Reference(ws, min_col=2, min_row=1, max_row=4)  # header + 3 rows
        pie.add_data(data, titles_from_data=True)
        pie.set_categories(labels)
        ws.add_chart(pie, "E2")

    output.seek(0)
    return send_file(
        output,
        as_attachment=True,
        download_name="students_export.xlsx",
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    )


if __name__ == "__main__":
    app.run(debug=True)

