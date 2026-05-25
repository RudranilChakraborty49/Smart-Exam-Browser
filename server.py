import eventlet
eventlet.monkey_patch()

"""
=============================================================
  SAFE EXAM BROWSER — CORE SERVER
  Features:
  - Per-student individual timer (multi-threading concept)
  - Each student gets their OWN 30min from login time
  - SQLite database for permanent storage
  - Auto-submit when individual timer expires
=============================================================
"""

from flask import Flask, request, jsonify, send_from_directory
from flask_socketio import SocketIO, emit, disconnect
from flask_cors import CORS
import time, os, sqlite3, io, csv

from auth import authenticate_user, create_user, get_all_users
from questions import get_shuffled_questions

app = Flask(__name__, static_folder='static')
app.config['SECRET_KEY'] = 'seb_secret_2024'
CORS(app)
socketio = SocketIO(
    app, cors_allowed_origins="*", async_mode="eventlet",
    logger=False, engineio_logger=False, ping_timeout=60, ping_interval=25
)


# ═══════════════════════════════════════════════════════════════
#  SQLITE DATABASE
# ═══════════════════════════════════════════════════════════════

DB_PATH = "exam_data.db"

def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()

    c.execute('''CREATE TABLE IF NOT EXISTS exam_sessions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        session_name TEXT,
        start_time TEXT,
        end_time TEXT,
        duration_minutes INTEGER,
        total_questions INTEGER,
        status TEXT DEFAULT 'active'
    )''')

    c.execute('''CREATE TABLE IF NOT EXISTS student_answers (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        session_id INTEGER,
        username TEXT,
        question_id TEXT,
        answer TEXT,
        submitted_at TEXT
    )''')

    # Now includes per-student timer tracking columns
    c.execute('''CREATE TABLE IF NOT EXISTS student_results (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        session_id INTEGER,
        username TEXT,
        score INTEGER DEFAULT 0,
        total_questions INTEGER DEFAULT 0,
        violations INTEGER DEFAULT 0,
        status TEXT DEFAULT 'active',
        login_time TEXT,
        submit_time TEXT,
        time_taken_seconds INTEGER DEFAULT 0
    )''')

    c.execute('''CREATE TABLE IF NOT EXISTS violations_log (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        session_id INTEGER,
        username TEXT,
        violation_type TEXT,
        violation_count INTEGER,
        timestamp TEXT
    )''')

    conn.commit()
    conn.close()


def db_conn():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn

def db_start_session(duration_minutes, total_questions):
    with db_conn() as conn:
        c = conn.cursor()
        c.execute(
            "INSERT INTO exam_sessions (session_name,start_time,duration_minutes,total_questions,status) VALUES (?,?,?,?,'active')",
            (f"Exam {time.strftime('%Y-%m-%d %H:%M')}", time.strftime('%Y-%m-%d %H:%M:%S'), duration_minutes, total_questions)
        )
        return c.lastrowid

def db_end_session(session_id):
    with db_conn() as conn:
        conn.execute("UPDATE exam_sessions SET end_time=?,status='ended' WHERE id=?",
                     (time.strftime('%Y-%m-%d %H:%M:%S'), session_id))

def db_save_answer(session_id, username, question_id, answer):
    with db_conn() as conn:
        c = conn.cursor()
        c.execute("SELECT id FROM student_answers WHERE session_id=? AND username=? AND question_id=?",
                  (session_id, username, str(question_id)))
        row = c.fetchone()
        now = time.strftime('%Y-%m-%d %H:%M:%S')
        if row:
            conn.execute("UPDATE student_answers SET answer=?,submitted_at=? WHERE id=?",
                         (answer, now, row[0]))
        else:
            conn.execute("INSERT INTO student_answers (session_id,username,question_id,answer,submitted_at) VALUES (?,?,?,?,?)",
                         (session_id, username, str(question_id), answer, now))

def db_save_result(session_id, username, score, total, violations, status, login_time, time_taken=0):
    with db_conn() as conn:
        c = conn.cursor()
        c.execute("SELECT id FROM student_results WHERE session_id=? AND username=?",
                  (session_id, username))
        row = c.fetchone()
        now = time.strftime('%Y-%m-%d %H:%M:%S')
        if row:
            conn.execute("""UPDATE student_results
                            SET score=?,violations=?,status=?,submit_time=?,time_taken_seconds=?
                            WHERE id=?""",
                         (score, violations, status, now, time_taken, row[0]))
        else:
            conn.execute("""INSERT INTO student_results
                            (session_id,username,score,total_questions,violations,status,login_time,submit_time,time_taken_seconds)
                            VALUES (?,?,?,?,?,?,?,?,?)""",
                         (session_id, username, score, total, violations, status, login_time, now, time_taken))

def db_save_violation(session_id, username, vtype, count):
    with db_conn() as conn:
        conn.execute("INSERT INTO violations_log (session_id,username,violation_type,violation_count,timestamp) VALUES (?,?,?,?,?)",
                     (session_id, username, vtype, count, time.strftime('%Y-%m-%d %H:%M:%S')))

def db_get_results(session_id=None):
    with db_conn() as conn:
        if session_id:
            rows = conn.execute(
                "SELECT * FROM student_results WHERE session_id=? ORDER BY score DESC", (session_id,)
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM student_results ORDER BY score DESC"
            ).fetchall()
        return [dict(r) for r in rows]

def db_get_sessions():
    with db_conn() as conn:
        rows = conn.execute("SELECT * FROM exam_sessions ORDER BY id DESC").fetchall()
        return [dict(r) for r in rows]


# ═══════════════════════════════════════════════════════════════
#  GLOBAL EXAM STATE
# ═══════════════════════════════════════════════════════════════

# students dict now includes per-student timer info
# Structure:
# {
#   socket_id: {
#     username, status, violations, answers, login_time,
#     login_epoch,        ← Unix timestamp when THIS student logged in
#     end_epoch,          ← Unix timestamp when THIS student's time ends
#     timer_thread,       ← This student's personal eventlet greenthread
#   }
# }
students = {}

exam_state = {
    "is_active": False,
    "questions": [],
    "start_time": None,
    "duration_minutes": 30,
    "session_id": None,
}


# ═══════════════════════════════════════════════════════════════
#  PER-STUDENT TIMER (THE MULTI-THREADING PART)
#
#  When a student logs in, we spawn a NEW greenthread just for them.
#  That thread counts down from duration_minutes independently.
#  When it hits 0, only THAT student gets auto-submitted.
#  Other students are unaffected — they have their own threads.
# ═══════════════════════════════════════════════════════════════

def start_student_timer(sid, username, duration_minutes):
    """
    Spawns a personal countdown timer for ONE student.
    This runs in its own greenthread (lightweight thread).
    
    Think of it like hiring a personal timekeeper for each student.
    Each timekeeper only watches their one student.
    """
    end_epoch = time.time() + (duration_minutes * 60)
    students[sid]["end_epoch"] = end_epoch

    def countdown():
        """
        This function runs in a separate greenthread per student.
        It ticks every second and sends the remaining time ONLY to this student.
        When time runs out, it auto-submits ONLY this student.
        """
        log(f"⏱️  [{username}] Personal timer started: {duration_minutes} min")

        while True:
            # Stop if student disconnected or exam ended
            if sid not in students:
                log(f"⏱️  [{username}] Timer stopped — student disconnected")
                break
            if not exam_state["is_active"] and students.get(sid, {}).get("status") == "active":
                break

            remaining = int(students[sid]["end_epoch"] - time.time())

            if remaining <= 0:
                # TIME'S UP FOR THIS STUDENT ONLY
                log(f"⏰ [{username}] Time expired — auto-submitting")
                auto_submit_student(sid)
                break

            # Send timer tick ONLY to this specific student (using 'to=sid')
            try:
                socketio.emit('timer_tick', {"remaining": remaining}, to=sid)
            except Exception:
                break

            # Send warnings at specific intervals to this student only
            if remaining in [600, 300, 180, 60, 30, 10]:
                mins = remaining // 60
                msg = f"⏰ {mins} minute(s) remaining!" if remaining >= 60 else f"⏰ {remaining} seconds remaining!"
                try:
                    socketio.emit('timer_warning', {"remaining": remaining, "message": msg}, to=sid)
                except Exception:
                    pass

            eventlet.sleep(1)  # Wait 1 second, then loop again

    # ── Spawn a new greenthread for this student ──
    # eventlet.spawn() creates a lightweight thread
    # Each student gets their own independent thread running countdown()
    thread = eventlet.spawn(countdown)
    students[sid]["timer_thread"] = thread


def stop_student_timer(sid):
    """Kill the timer thread for a specific student."""
    if sid in students and students[sid].get("timer_thread"):
        try:
            students[sid]["timer_thread"].kill()
        except Exception:
            pass
        students[sid]["timer_thread"] = None


def auto_submit_student(sid):
    """
    Auto-submit ONE specific student when their personal timer expires.
    Does not affect any other student.
    """
    if sid not in students:
        return
    s = students[sid]
    if s["status"] != "active":
        return  # Already submitted or kicked

    s["status"] = "auto_submitted"
    score = calculate_score(s["answers"])
    time_taken = int(time.time() - s["login_epoch"])

    # Save to database
    if exam_state["session_id"]:
        db_save_result(
            exam_state["session_id"], s["username"],
            score, len(exam_state["questions"]),
            s["violations"], "auto_submitted",
            s["login_time"], time_taken
        )

    log(f"⏰ [{s['username']}] Auto-submitted. Score: {score}. Time taken: {time_taken}s")

    # Notify only this student
    try:
        socketio.emit('exam_submitted', {
            "message": "⏰ Time's up! Your exam has been automatically submitted.",
            "score": score,
            "auto": True,
            "time_taken": time_taken
        }, to=sid)
    except Exception:
        pass

    # Update admin dashboard
    socketio.emit('student_submitted', {
        "username": s["username"],
        "socket_id": sid,
        "score": score,
        "auto": True
    })
    socketio.emit('update_student_list', get_student_list())


# ═══════════════════════════════════════════════════════════════
#  FLASK ROUTES
# ═══════════════════════════════════════════════════════════════

@app.route('/')
def serve_student():
    return send_from_directory('static', 'student.html')

@app.route('/admin')
def serve_admin():
    return send_from_directory('static', 'admin.html')

@app.route('/api/status')
def api_status():
    return jsonify({
        "server": "running",
        "exam_active": exam_state["is_active"],
        "students_online": len(students),
        "questions_loaded": len(exam_state["questions"]),
        "duration_minutes": exam_state["duration_minutes"],
    })

@app.route('/api/upload_csv', methods=['POST'])
def upload_csv():
    try:
        if 'file' not in request.files:
            return jsonify({"error": "No file found."}), 400
        f = request.files['file']
        if not f.filename.lower().endswith('.csv'):
            return jsonify({"error": "Only CSV files supported."}), 400
        content = f.read().decode('utf-8-sig')
        reader = csv.DictReader(io.StringIO(content))
        questions = []
        for i, row in enumerate(reader):
            row = {k.strip().lower(): v.strip() for k, v in row.items() if k}
            qt = row.get('question', '')
            if not qt: continue
            opts = [row.get(k,'').strip() for k in ['option_a','option_b','option_c','option_d'] if row.get(k,'').strip()]
            if len(opts) < 2: continue
            ra = row.get('answer','').strip().upper()
            am = {'A':0,'B':1,'C':2,'D':3}
            ans = opts[am[ra]] if ra in am and am[ra] < len(opts) else row.get('answer','')
            questions.append({"id": int(row.get('id', i+1)), "question": qt, "options": opts, "answer": ans})
        if not questions:
            return jsonify({"error": "No valid questions found. Check headers: id,question,option_a,option_b,option_c,option_d,answer"}), 400
        exam_state["questions"] = questions
        log(f"✅ {len(questions)} questions loaded")
        try: socketio.emit('questions_loaded', {"count": len(questions)})
        except: pass
        return jsonify({"success": True, "count": len(questions)})
    except Exception as e:
        import traceback; log(traceback.format_exc())
        return jsonify({"error": str(e)}), 500

@app.route('/api/results')
def api_results():
    sid = request.args.get('session_id', exam_state["session_id"])
    results = db_get_results(sid)
    if not results:
        results = [
            {
                "username": s["username"],
                "score": calculate_score(s["answers"]),
                "total_questions": len(exam_state["questions"]),
                "violations": s["violations"],
                "status": s["status"],
                "login_time": s["login_time"],
                "submit_time": "—",
                "time_taken_seconds": int(time.time() - s["login_epoch"]) if s.get("login_epoch") else 0
            }
            for s in students.values()
        ]
    return jsonify({"results": results})

@app.route('/api/sessions')
def api_sessions():
    return jsonify({"sessions": db_get_sessions()})

@app.route('/api/add_user', methods=['POST'])
def add_user():
    data = request.get_json()
    return jsonify(create_user(data.get("username"), data.get("password")))

@app.route('/api/users')
def list_users():
    return jsonify({"users": get_all_users()})


# ═══════════════════════════════════════════════════════════════
#  SOCKET.IO EVENTS
# ═══════════════════════════════════════════════════════════════

@socketio.on('connect')
def on_connect():
    log(f"🔌 Connected: {request.sid}")
    emit('connected', {"sid": request.sid})

@socketio.on('disconnect')
def on_disconnect():
    sid = request.sid
    if sid in students:
        s = students[sid]
        stop_student_timer(sid)  # Kill this student's personal timer
        if exam_state["session_id"] and s["status"] == "active":
            time_taken = int(time.time() - s["login_epoch"]) if s.get("login_epoch") else 0
            db_save_result(
                exam_state["session_id"], s["username"],
                calculate_score(s["answers"]), len(exam_state["questions"]),
                s["violations"], "disconnected", s["login_time"], time_taken
            )
        log(f"❌ Disconnected: {s['username']}")
        socketio.emit('student_disconnected', {"username": s["username"], "socket_id": sid})
        del students[sid]
        socketio.emit('update_student_list', get_student_list())

@socketio.on('login')
def handle_login(data):
    username = data.get("username", "").strip()
    password = data.get("password", "").strip()
    sid = request.sid

    if not username or not password:
        emit('login_response', {"success": False, "message": "Enter username and password."})
        return

    auth = authenticate_user(username, password)
    if not auth["success"]:
        emit('login_response', {"success": False, "message": auth["message"]})
        return

    if not exam_state["is_active"]:
        emit('login_response', {"success": False, "message": "Exam hasn't started yet. Wait for the host."})
        return

    for s_sid, s_data in students.items():
        if s_data["username"] == username:
            emit('login_response', {"success": False, "message": "Already logged in from another device."})
            return

    login_epoch = time.time()  # Record exact login timestamp

    # Register student with their own timer info
    students[sid] = {
        "username":     username,
        "status":       "active",
        "violations":   0,
        "answers":      {},
        "login_time":   time.strftime('%H:%M:%S'),
        "login_epoch":  login_epoch,          # ← When they logged in (Unix time)
        "end_epoch":    None,                 # ← Will be set when timer starts
        "timer_thread": None,                 # ← Will hold their personal thread
    }

    questions = get_shuffled_questions(exam_state["questions"], sid)
    duration  = exam_state["duration_minutes"]

    log(f"✅ Login: {username} | Starting personal {duration} min timer")

    # Send login response WITH their personal timer duration
    emit('login_response', {
        "success":          True,
        "username":         username,
        "questions":        questions,
        "total":            len(questions),
        "timer_remaining":  duration * 60,    # Full duration — starts fresh for them
        "duration_minutes": duration
    })

    # ── START THIS STUDENT'S PERSONAL TIMER THREAD ──
    # This is the multi-threading part sir mentioned
    # Each student gets their own independent countdown
    start_student_timer(sid, username, duration)

    socketio.emit('student_joined', {
        "username":   username,
        "socket_id":  sid,
        "login_time": students[sid]["login_time"],
        "duration":   duration
    })
    socketio.emit('update_student_list', get_student_list())

@socketio.on('submit_answer')
def handle_answer(data):
    sid = request.sid
    if sid not in students: return
    qid = str(data.get("question_id"))
    ans = data.get("answer")
    students[sid]["answers"][qid] = ans
    if exam_state["session_id"]:
        db_save_answer(exam_state["session_id"], students[sid]["username"], qid, ans)
    emit('answer_received', {"success": True, "question_id": qid})
    socketio.emit('update_student_list', get_student_list())

@socketio.on('submit_exam')
def handle_submit(data):
    sid = request.sid
    if sid not in students: return
    s = students[sid]
    if s["status"] != "active": return  # Already submitted

    stop_student_timer(sid)  # Kill their personal timer — they submitted early
    s["status"] = "submitted"
    score = calculate_score(s["answers"])
    time_taken = int(time.time() - s["login_epoch"]) if s.get("login_epoch") else 0

    if exam_state["session_id"]:
        db_save_result(
            exam_state["session_id"], s["username"],
            score, len(exam_state["questions"]),
            s["violations"], "submitted", s["login_time"], time_taken
        )

    mins = time_taken // 60
    secs = time_taken % 60
    log(f"📋 {s['username']} submitted. Score: {score}/{len(exam_state['questions'])}. Time: {mins}m {secs}s")

    emit('exam_submitted', {
        "message":    "Exam submitted successfully!",
        "score":      score,
        "time_taken": time_taken,
        "auto":       False
    })
    socketio.emit('student_submitted', {"username": s["username"], "socket_id": sid, "score": score})
    socketio.emit('update_student_list', get_student_list())

@socketio.on('violation')
def handle_violation(data):
    sid = request.sid
    if sid not in students: return
    vtype = data.get("type", "unknown")
    students[sid]["violations"] += 1
    count = students[sid]["violations"]
    username = students[sid]["username"]

    if exam_state["session_id"]:
        db_save_violation(exam_state["session_id"], username, vtype, count)

    log(f"⚠️  Violation #{count} [{username}]: {vtype}")
    socketio.emit('violation_alert', {"username": username, "socket_id": sid, "type": vtype, "count": count})

    if count >= 3:
        stop_student_timer(sid)  # Kill their timer
        emit('kicked', {"reason": f"Removed after {count} violations."})
        students[sid]["status"] = "kicked"
        time_taken = int(time.time() - students[sid]["login_epoch"]) if students[sid].get("login_epoch") else 0
        if exam_state["session_id"]:
            db_save_result(
                exam_state["session_id"], username,
                calculate_score(students[sid]["answers"]),
                len(exam_state["questions"]), count,
                "kicked", students[sid]["login_time"], time_taken
            )
        socketio.emit('student_kicked', {"username": username, "socket_id": sid})
        socketio.emit('update_student_list', get_student_list())
        disconnect()
    else:
        emit('warning', {
            "message": f"⚠️ Warning {count}/3: Do not switch tabs or leave the exam!",
            "count": count
        })
        socketio.emit('update_student_list', get_student_list())

@socketio.on('kick')
def handle_kick(data):
    target = data.get("socket_id")
    reason = data.get("reason", "Removed by admin")
    if target not in students: return
    s = students[target]
    stop_student_timer(target)  # Kill their timer
    socketio.emit('kicked', {"reason": reason}, to=target)
    s["status"] = "kicked"
    time_taken = int(time.time() - s["login_epoch"]) if s.get("login_epoch") else 0
    if exam_state["session_id"]:
        db_save_result(
            exam_state["session_id"], s["username"],
            calculate_score(s["answers"]), len(exam_state["questions"]),
            s["violations"], "kicked", s["login_time"], time_taken
        )
    socketio.emit('student_kicked', {"username": s["username"], "socket_id": target})
    del students[target]
    socketio.emit('update_student_list', get_student_list())
    log(f"🚫 Kicked: {s['username']}")

@socketio.on('start_exam')
def handle_start(data):
    if not exam_state["questions"]:
        emit('error', {"message": "No questions loaded! Upload a CSV first."})
        return
    duration = int(data.get("duration_minutes", 30))
    if not (1 <= duration <= 300):
        emit('error', {"message": "Duration must be 1–300 minutes."})
        return

    exam_state["is_active"]        = True
    exam_state["duration_minutes"] = duration
    exam_state["start_time"]       = time.strftime('%H:%M:%S')
    exam_state["session_id"]       = db_start_session(duration, len(exam_state["questions"]))

    log(f"🟢 Exam STARTED | {duration} min per student | Session {exam_state['session_id']}")

    # Note: NO global timer here anymore!
    # Each student's timer starts when THEY log in (in handle_login above)
    socketio.emit('exam_started', {
        "message":         "Exam has started! Students can now log in.",
        "total_questions": len(exam_state["questions"]),
        "duration_minutes": duration,
        "time":            exam_state["start_time"]
    })

@socketio.on('stop_exam')
def handle_stop(data):
    exam_state["is_active"] = False

    # Stop ALL student timers
    for sid in list(students.keys()):
        stop_student_timer(sid)

    if exam_state["session_id"]:
        db_end_session(exam_state["session_id"])

    log("🔴 Exam STOPPED by admin")
    socketio.emit('exam_stopped', {"message": "Exam ended by host."})

    results = db_get_results(exam_state["session_id"]) or [
        {"username": s["username"], "score": calculate_score(s["answers"]),
         "total_questions": len(exam_state["questions"]),
         "violations": s["violations"], "status": s["status"]}
        for s in students.values()
    ]
    socketio.emit('exam_results', {"results": results})


# ═══════════════════════════════════════════════════════════════
#  HELPERS
# ═══════════════════════════════════════════════════════════════

def get_student_list():
    result = []
    for sid, d in students.items():
        # Calculate remaining time for each student individually
        remaining = 0
        if d.get("end_epoch") and d["status"] == "active":
            remaining = max(0, int(d["end_epoch"] - time.time()))
        result.append({
            "socket_id":     sid,
            "username":      d["username"],
            "status":        d["status"],
            "violations":    d["violations"],
            "answers_count": len(d["answers"]),
            "login_time":    d["login_time"],
            "time_remaining": remaining,   # ← Per-student remaining time for admin
        })
    return result

def calculate_score(answers):
    if not exam_state["questions"]: return 0
    q_map = {str(q["id"]): q.get("answer","") for q in exam_state["questions"]}
    return sum(1 for qid, ans in answers.items()
               if q_map.get(qid,"").strip().lower() == ans.strip().lower())

def log(msg):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}")


# ═══════════════════════════════════════════════════════════════
#  DB INIT & STARTUP
# ═══════════════════════════════════════════════════════════════

def _safe_init_db():
    if os.path.exists(DB_PATH):
        try:
            conn = sqlite3.connect(DB_PATH)
            conn.execute("SELECT 1 FROM exam_sessions LIMIT 1")
            conn.close()
            log(f"✅ Database found: {DB_PATH}")
        except sqlite3.OperationalError:
            conn.close()
            os.remove(DB_PATH)
            log("♻️  Rebuilding database...")
            init_db()
    else:
        init_db()

_safe_init_db()

if __name__ == '__main__':
    print("=" * 55)
    print("  SEB CORE SERVER — Multi-Thread Timer Edition")
    print("  Student UI  → http://localhost:5000")
    print("  Admin Panel → http://localhost:5000/admin")
    print("  Database    → exam_data.db")
    print("  Each student gets their OWN personal timer!")
    print("=" * 55)
    socketio.run(app, host='0.0.0.0', port=5000, debug=False)
