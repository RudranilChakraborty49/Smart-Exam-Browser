"""
=============================================================
  QUESTION ENGINE (Adi's Part)
  Reads questions from CSV, shuffles uniquely per student
=============================================================
"""


import csv
import random
import hashlib
import json
import os


def load_questions_from_csv(filepath: str) -> list:
    """
    Load questions from a CSV file.

    Expected CSV columns:
    id, question, option_a, option_b, option_c, option_d, answer

    Returns a list of question dicts.
    """
    questions = []

    if not os.path.exists(filepath):
        print(f"[Questions] CSV not found: {filepath}")
        return []

    with open(filepath, newline='', encoding='utf-8') as f:
        reader = csv.DictReader(f)
        for row in reader:
            # Build options list from CSV columns
            options = []
            for key in ['option_a', 'option_b', 'option_c', 'option_d']:
                if key in row and row[key].strip():
                    options.append(row[key].strip())

            questions.append({
                "id": int(row.get("id", len(questions) + 1)),
                "question": row.get("question", "").strip(),
                "options": options,
                "answer": row.get("answer", "").strip()  # Kept server-side only
            })

    print(f"[Questions] Loaded {len(questions)} questions from {filepath}")
    return questions


def load_questions_from_json(filepath: str) -> list:
    """Load questions from a JSON file."""
    if not os.path.exists(filepath):
        return []
    with open(filepath, 'r', encoding='utf-8') as f:
        return json.load(f)


def get_shuffled_questions(questions: list, student_id: str) -> list:
    """
    Returns a uniquely shuffled copy of questions for a student.
    - Questions are shuffled in a different order per student
    - Options within each question are also shuffled
    - Answer field is REMOVED before sending to student

    Args:
        questions: Full question list from server
        student_id: Socket ID used as shuffle seed

    Returns:
        List of questions without answers, uniquely ordered
    """
    if not questions:
        return []

    # Create a deterministic-but-unique seed per student
    seed_str = f"{student_id}-{len(questions)}"
    seed = int(hashlib.md5(seed_str.encode()).hexdigest(), 16) % (2**32)

    rng = random.Random(seed)

    # Deep copy + shuffle question order
    shuffled = questions.copy()
    rng.shuffle(shuffled)

    # For each question, shuffle options and remove answer
    result = []
    for q in shuffled:
        options = q["options"].copy()
        rng.shuffle(options)

        result.append({
            "id": q["id"],
            "question": q["question"],
            "options": options
            # ← answer is intentionally excluded
        })

    return result


def questions_to_csv(questions: list, filepath: str):
    """Save questions list back to CSV (utility function)."""
    if not questions:
        return
    os.makedirs(os.path.dirname(filepath) or ".", exist_ok=True)
    with open(filepath, 'w', newline='', encoding='utf-8') as f:
        fieldnames = ['id', 'question', 'option_a', 'option_b', 'option_c', 'option_d', 'answer']
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for q in questions:
            opts = q.get("options", [])
            writer.writerow({
                "id": q["id"],
                "question": q["question"],
                "option_a": opts[0] if len(opts) > 0 else "",
                "option_b": opts[1] if len(opts) > 1 else "",
                "option_c": opts[2] if len(opts) > 2 else "",
                "option_d": opts[3] if len(opts) > 3 else "",
                "answer": q.get("answer", "")
            })
