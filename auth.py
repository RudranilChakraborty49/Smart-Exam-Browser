"""
=============================================================
  AUTH MODULE (Upasana's Part)
  Handles user registration and login validation
  Uses SQLite database to store users
  + CSV IMPORT FEATURE ADDED
=============================================================
"""

import sqlite3
import hashlib
import os
import csv

DB_PATH = "users.db"


def init_db():
    """Create the users table if it doesn't exist. Add default users."""
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('''
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            role TEXT DEFAULT 'student'
        )
    ''')
    conn.commit()

    # Default users
    defaults = [
        ("student1", "pass1"),
        ("student2", "pass2"),
        ("student3", "pass3"),
        ("alice",    "alice123"),
        ("bob",      "bob123"),
    ]

    for uname, pwd in defaults:
        try:
            c.execute(
                "INSERT INTO users (username, password_hash) VALUES (?, ?)",
                (uname, hash_password(pwd))
            )
        except sqlite3.IntegrityError:
            pass

    conn.commit()
    conn.close()


def hash_password(password: str) -> str:
    """Hash a password using SHA-256."""
    return hashlib.sha256(password.encode()).hexdigest()


def authenticate_user(username: str, password: str) -> dict:
    init_db()
    if not username or not password:
        return {"success": False, "message": "Username and password are required."}

    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute(
        "SELECT username, password_hash FROM users WHERE username = ?",
        (username,)
    )
    row = c.fetchone()
    conn.close()

    if row is None:
        return {"success": False, "message": "User not found."}

    if row[1] == hash_password(password):
        return {"success": True, "message": "Login successful."}
    else:
        return {"success": False, "message": "Incorrect password."}


def create_user(username: str, password: str, role: str = "student") -> dict:
    init_db()
    if not username or not password:
        return {"success": False, "message": "Username and password required."}
    if len(password) < 4:
        return {"success": False, "message": "Password must be at least 4 characters."}

    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    try:
        c.execute(
            "INSERT INTO users (username, password_hash, role) VALUES (?, ?, ?)",
            (username, hash_password(password), role)
        )
        conn.commit()
        conn.close()
        return {"success": True, "message": f"User '{username}' created."}
    except sqlite3.IntegrityError:
        conn.close()
        return {"success": False, "message": f"Username '{username}' already exists."}


# ===========================
# 🔥 NEW FUNCTION: CSV IMPORT
# ===========================
def import_users_from_csv(file_path: str) -> dict:
    """
    Import users from CSV file.
    CSV format: username,password,role(optional)
    """
    init_db()

    if not os.path.exists(file_path):
        return {"success": False, "message": "CSV file not found."}

    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()

    added = 0
    skipped = 0

    try:
        with open(file_path, newline='', encoding='utf-8') as csvfile:
            reader = csv.reader(csvfile)

            for row in reader:
                if len(row) < 2:
                    skipped += 1
                    continue

                username = row[0].strip()
                password = row[1].strip()
                role = row[2].strip() if len(row) > 2 else "student"

                try:
                    c.execute(
                        "INSERT INTO users (username, password_hash, role) VALUES (?, ?, ?)",
                        (username, hash_password(password), role)
                    )
                    added += 1
                except sqlite3.IntegrityError:
                    skipped += 1

        conn.commit()
        conn.close()

        return {
            "success": True,
            "message": f"{added} users added, {skipped} skipped (duplicates/invalid)."
        }

    except Exception as e:
        conn.close()
        return {"success": False, "message": str(e)}


def get_all_users() -> list:
    init_db()
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT username, role FROM users")
    rows = c.fetchall()
    conn.close()
    return [{"username": r[0], "role": r[1]} for r in rows]


def delete_user(username: str) -> dict:
    init_db()
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("DELETE FROM users WHERE username = ?", (username,))
    conn.commit()
    affected = c.rowcount
    conn.close()

    if affected:
        return {"success": True, "message": f"User '{username}' deleted."}
    return {"success": False, "message": "User not found."}


# Initialize DB on import
init_db()

# 🔥 Auto import CSV (runs once)
import_users_from_csv("students.csv")