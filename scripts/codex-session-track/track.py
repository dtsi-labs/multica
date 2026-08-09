#!/opt/homebrew/bin/python3
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any


HOME = Path.home()
SESSIONS_ROOT = HOME / ".codex" / "sessions"
STATE_DIR = HOME / ".multica" / "codex-session-track"
STATE_FILE = STATE_DIR / "state.json"
LOG_FILE = HOME / "Library" / "Logs" / "multica-codex-session-track.log"
SESSIONS_PROJECT_ID = "6fb91134-014e-42c9-9ca2-318ec60dd3fd"

UUID_RE = r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
CODEx_MARKER_RE = re.compile(rf"codex-track-v1\s+session_id=({UUID_RE})")
CODEx_SESSION_ID_RE = re.compile(rf"\*\*Session ID:\*\*\s*`({UUID_RE})`")

TITLE_TRUNCATE = 90
DEFAULT_RECENT_DAYS = 2
DEFAULT_IDLE_MINUTES = 90


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def iso_now() -> str:
    return now_utc().isoformat()


def parse_ts(raw: str | None) -> datetime | None:
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None


def format_local(raw: str | None) -> str:
    dt = parse_ts(raw)
    if not dt:
        return "unknown"
    return dt.astimezone().strftime("%Y-%m-%d %H:%M %Z")


def short_cwd(cwd: str | None) -> str:
    if not cwd:
        return "(unknown)"
    home = str(HOME)
    if cwd.startswith(home):
        return "~" + cwd[len(home):]
    return cwd


def clean_prompt(text: str | None) -> str:
    if not text:
        return ""
    flattened = re.sub(r"\s+", " ", text.strip())
    return flattened


def normalize_prompt_candidate(text: str | None) -> str:
    raw = (text or "").strip()
    if not raw:
        return ""

    if raw.startswith("# AGENTS.md instructions for "):
        return ""

    request_match = re.search(
        r"(?:^|\n)##\s*My request for Codex:\s*(.+)$",
        raw,
        flags=re.DOTALL,
    )
    if request_match:
        raw = request_match.group(1).strip()
    else:
        request_match = re.search(
            r"(?:^|\n)My request for Codex:\s*(.+)$",
            raw,
            flags=re.DOTALL,
        )
        if request_match:
            raw = request_match.group(1).strip()

    return clean_prompt(raw)


def prompt_rank(text: str) -> tuple[int, int]:
    if not text:
        return (0, 0)
    return (1, -len(text))


def read_stdin_json() -> dict[str, Any]:
    if sys.stdin.isatty():
        return {}
    raw = sys.stdin.read()
    if not raw.strip():
        return {}
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        log(f"WARN hook stdin was not valid json: {raw[:200]!r}")
        return {}
    return payload if isinstance(payload, dict) else {}


def run(cmd: list[str], stdin: str | None = None) -> tuple[int, str, str]:
    proc = subprocess.run(cmd, input=stdin, capture_output=True, text=True)
    return proc.returncode, proc.stdout, proc.stderr


def log(message: str) -> None:
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    with LOG_FILE.open("a", encoding="utf-8") as fh:
        fh.write(f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')} {message}\n")


def load_state() -> dict[str, Any]:
    if not STATE_FILE.exists():
        return {"version": 1, "sessions": {}}
    try:
        payload = json.loads(STATE_FILE.read_text())
    except json.JSONDecodeError:
        log("WARN state file was invalid json; resetting")
        return {"version": 1, "sessions": {}}
    if not isinstance(payload, dict):
        return {"version": 1, "sessions": {}}
    payload.setdefault("version", 1)
    payload.setdefault("sessions", {})
    return payload


def save_state(state: dict[str, Any]) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n")


def list_existing_issues() -> dict[str, dict[str, Any]]:
    seen: dict[str, dict[str, Any]] = {}
    offset = 0
    while True:
        rc, out, err = run(
            [
                "multica",
                "issue",
                "list",
                "--project",
                SESSIONS_PROJECT_ID,
                "--output",
                "json",
                "--limit",
                "200",
                "--offset",
                str(offset),
            ]
        )
        if rc != 0:
            log(f"WARN issue list failed at offset={offset}: {(err or out).strip()}")
            break
        try:
            payload = json.loads(out)
        except json.JSONDecodeError:
            log(f"WARN issue list was not json at offset={offset}")
            break
        if isinstance(payload, dict):
            issues = payload.get("issues") or []
            has_more = bool(payload.get("has_more"))
        elif isinstance(payload, list):
            issues = payload
            has_more = len(issues) >= 200
        else:
            break
        if not issues:
            break
        for issue in issues:
            if not isinstance(issue, dict):
                continue
            desc = issue.get("description") or ""
            session_id = None
            match = CODEx_MARKER_RE.search(desc)
            if match:
                session_id = match.group(1)
            elif "Auto-tracked Codex session." in desc:
                match = CODEx_SESSION_ID_RE.search(desc)
                if match:
                    session_id = match.group(1)
            if not session_id:
                continue
            seen[session_id] = {
                "issue_id": issue.get("id"),
                "status": issue.get("status"),
                "title": issue.get("title"),
            }
        if not has_more:
            break
        offset += len(issues)
    return seen


def session_files(recent_days: int, session_id: str | None) -> list[Path]:
    if session_id:
        return sorted(SESSIONS_ROOT.rglob(f"*{session_id}*.jsonl"))

    cutoff = now_utc() - timedelta(days=recent_days)
    files: list[Path] = []
    for path in SESSIONS_ROOT.rglob("*.jsonl"):
        try:
            modified = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
        except OSError:
            continue
        if modified >= cutoff:
            files.append(path)
    return sorted(files)


def extract_user_text(payload: dict[str, Any]) -> str:
    content = payload.get("content")
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for block in content:
        if not isinstance(block, dict):
            continue
        if block.get("type") == "input_text" and isinstance(block.get("text"), str):
            parts.append(block["text"])
        elif block.get("type") == "text" and isinstance(block.get("text"), str):
            parts.append(block["text"])
    return "\n".join(parts)


def parse_session_file(path: Path) -> dict[str, Any] | None:
    summary: dict[str, Any] = {
        "session_id": None,
        "cwd": None,
        "originator": None,
        "cli_version": None,
        "source": None,
        "started_at": None,
        "first_prompt": None,
        "last_event_at": None,
        "last_task_complete_at": None,
        "transcript_path": str(path),
    }
    fallback_user_prompt = ""

    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError as exc:
        log(f"WARN could not read {path}: {exc}")
        return None

    for raw in lines:
        try:
            record = json.loads(raw)
        except json.JSONDecodeError:
            continue
        record_type = record.get("type")
        top_ts = record.get("timestamp")
        if isinstance(top_ts, str):
            summary["last_event_at"] = top_ts

        if record_type == "session_meta":
            payload = record.get("payload") or {}
            if isinstance(payload, dict):
                summary["session_id"] = payload.get("id") or summary["session_id"]
                summary["cwd"] = payload.get("cwd") or summary["cwd"]
                summary["originator"] = payload.get("originator") or summary["originator"]
                summary["cli_version"] = payload.get("cli_version") or summary["cli_version"]
                summary["source"] = payload.get("source") or summary["source"]
                summary["started_at"] = payload.get("timestamp") or summary["started_at"]
            continue

        if record_type == "event_msg":
            payload = record.get("payload") or {}
            if not isinstance(payload, dict):
                continue
            event_type = payload.get("type")
            if event_type == "user_message" and not summary["first_prompt"]:
                summary["first_prompt"] = normalize_prompt_candidate(payload.get("message"))
            elif event_type == "task_complete":
                summary["last_task_complete_at"] = top_ts
            continue

        if record_type == "response_item" and not summary["first_prompt"]:
            payload = record.get("payload") or {}
            if (
                isinstance(payload, dict)
                and payload.get("type") == "message"
                and payload.get("role") == "user"
            ):
                text = normalize_prompt_candidate(extract_user_text(payload))
                if prompt_rank(text) > prompt_rank(fallback_user_prompt):
                    fallback_user_prompt = text

    if not summary["session_id"]:
        summary["session_id"] = path.stem.split("rollout-")[-1]

    if not summary["first_prompt"] and fallback_user_prompt:
        summary["first_prompt"] = fallback_user_prompt

    prompt = normalize_prompt_candidate(summary["first_prompt"])
    summary["first_prompt"] = prompt

    try:
        summary["file_mtime_at"] = datetime.fromtimestamp(
            path.stat().st_mtime, tz=timezone.utc
        ).isoformat()
    except OSError:
        summary["file_mtime_at"] = iso_now()

    return summary


def build_title(summary: dict[str, Any]) -> str:
    date_prefix = (summary.get("started_at") or "")[:10]
    prompt = clean_prompt(summary.get("first_prompt")) or "(no prompt captured)"
    if len(prompt) > TITLE_TRUNCATE:
        prompt = prompt[: TITLE_TRUNCATE - 1] + "…"
    return f"[{date_prefix}] Session: {prompt}" if date_prefix else f"Session: {prompt}"


def build_description(summary: dict[str, Any]) -> str:
    source_bits = [summary.get("originator"), summary.get("cli_version")]
    source_line = " · ".join(part for part in source_bits if part)
    if not source_line:
        source_line = "Codex"
    return (
        "Auto-tracked Codex session.\n\n"
        f"**CWD:** `{short_cwd(summary.get('cwd'))}`\n"
        f"**Session ID:** `{summary.get('session_id')}`\n"
        f"**Started:** {format_local(summary.get('started_at'))}\n"
        f"**Source:** {source_line}\n"
        f"**Transcript:** `{summary.get('transcript_path')}`\n\n"
        "**First prompt:**\n"
        f"{summary.get('first_prompt') or '(no prompt captured)'}\n\n"
        f"<!-- codex-track-v1 session_id={summary.get('session_id')} -->"
    )


def create_issue(summary: dict[str, Any], dry_run: bool) -> str | None:
    title = build_title(summary)
    description = build_description(summary)
    if dry_run:
        log(f"DRY create {summary['session_id']} {title}")
        return None

    rc, out, err = run(
        [
            "multica",
            "issue",
            "create",
            "--title",
            title,
            "--description-stdin",
            "--project",
            SESSIONS_PROJECT_ID,
            "--status",
            "in_progress",
            "--priority",
            "none",
            "--output",
            "json",
        ],
        stdin=description,
    )
    if rc != 0:
        log(f"ERROR create failed for {summary['session_id']}: {(err or out).strip()}")
        return None
    try:
        payload = json.loads(out)
    except json.JSONDecodeError:
        log(f"ERROR create returned non-json for {summary['session_id']}: {out[:200]}")
        return None
    issue_id = payload.get("id")
    log(f"OK created {issue_id} for {summary['session_id']}")
    return issue_id


def set_issue_status(issue_id: str, status: str, dry_run: bool) -> bool:
    if dry_run:
        log(f"DRY status {issue_id} -> {status}")
        return True
    rc, out, err = run(["multica", "issue", "status", issue_id, status, "--output", "json"])
    if rc != 0:
        log(f"ERROR status {issue_id} -> {status}: {(err or out).strip()}")
        return False
    return True


def update_issue(issue_id: str, summary: dict[str, Any], dry_run: bool) -> bool:
    title = build_title(summary)
    description = build_description(summary)
    if dry_run:
        log(f"DRY update {issue_id} title={title}")
        return True
    rc, out, err = run(
        [
            "multica",
            "issue",
            "update",
            issue_id,
            "--title",
            title,
            "--description-stdin",
            "--output",
            "json",
        ],
        stdin=description,
    )
    if rc != 0:
        log(f"ERROR update failed for {issue_id}: {(err or out).strip()}")
        return False
    log(f"OK updated {issue_id} for {summary['session_id']}")
    return True


def add_comment(issue_id: str, content: str, dry_run: bool) -> bool:
    if dry_run:
        log(f"DRY comment {issue_id}: {content[:120]}")
        return True
    rc, out, err = run(
        ["multica", "issue", "comment", "add", issue_id, "--content-stdin", "--output", "json"],
        stdin=content,
    )
    if rc != 0:
        log(f"ERROR comment add failed for {issue_id}: {(err or out).strip()}")
        return False
    return True


def idle_should_close(summary: dict[str, Any], idle_minutes: int) -> bool:
    last_complete = parse_ts(summary.get("last_task_complete_at"))
    if not last_complete:
        return False
    last_event = parse_ts(summary.get("last_event_at")) or parse_ts(summary.get("file_mtime_at"))
    if not last_event:
        return False
    cutoff = now_utc() - timedelta(minutes=idle_minutes)
    return last_complete <= cutoff and last_event <= cutoff


def close_comment(summary: dict[str, Any], reason: str) -> str:
    return (
        f"Codex session closed by tracker at {datetime.now().astimezone().strftime('%Y-%m-%d %H:%M %Z')}.\n"
        f"Reason: {reason}.\n"
        f"Last activity: {format_local(summary.get('last_event_at'))}."
    )


def reopen_comment(summary: dict[str, Any]) -> str:
    return (
        f"Codex session resumed at {datetime.now().astimezone().strftime('%Y-%m-%d %H:%M %Z')}.\n"
        f"Latest activity: {format_local(summary.get('last_event_at'))}."
    )


def ensure_issue(
    summary: dict[str, Any],
    state: dict[str, Any],
    existing_issues: dict[str, dict[str, Any]],
    dry_run: bool,
) -> None:
    session_id = summary["session_id"]
    sessions = state.setdefault("sessions", {})
    entry = sessions.get(session_id)

    if not entry and session_id in existing_issues:
        existing = existing_issues[session_id]
        entry = {
            "issue_id": existing.get("issue_id"),
            "status": existing.get("status"),
            "title": existing.get("title"),
        }
        sessions[session_id] = entry

    if not entry:
        if not summary.get("first_prompt"):
            log(f"SKIP create for {session_id}: no prompt captured yet")
            return
        if dry_run:
            log(f"DRY would create issue for {session_id}")
            return
        issue_id = create_issue(summary, dry_run)
        if not issue_id:
            return
        entry = {
            "issue_id": issue_id,
            "status": "in_progress",
            "title": build_title(summary),
            "created_at": iso_now(),
        }
        sessions[session_id] = entry

    last_seen_at = parse_ts(entry.get("last_seen_at"))
    latest_seen = parse_ts(summary.get("last_event_at")) or parse_ts(summary.get("file_mtime_at"))
    if entry.get("status") == "done" and latest_seen and (
        not last_seen_at or latest_seen > last_seen_at
    ):
        if set_issue_status(entry["issue_id"], "in_progress", dry_run):
            add_comment(entry["issue_id"], reopen_comment(summary), dry_run)
            entry["status"] = "in_progress"
            entry["reopened_at"] = iso_now()
            log(f"OK reopened {entry['issue_id']} for {session_id}")

    expected_title = build_title(summary)
    if summary.get("first_prompt") and entry.get("title") != expected_title:
        if update_issue(entry["issue_id"], summary, dry_run):
            entry["title"] = expected_title

    entry["last_seen_at"] = summary.get("last_event_at") or summary.get("file_mtime_at")
    entry["last_task_complete_at"] = summary.get("last_task_complete_at")
    entry["cwd"] = summary.get("cwd")
    entry["transcript_path"] = summary.get("transcript_path")
    entry["first_prompt"] = summary.get("first_prompt")


def maybe_idle_close(
    summary: dict[str, Any],
    state: dict[str, Any],
    idle_minutes: int,
    dry_run: bool,
) -> None:
    session_id = summary["session_id"]
    entry = state.setdefault("sessions", {}).get(session_id)
    if not entry or entry.get("status") == "done":
        return
    if not idle_should_close(summary, idle_minutes):
        return
    if set_issue_status(entry["issue_id"], "done", dry_run):
        add_comment(entry["issue_id"], close_comment(summary, f"idle for >= {idle_minutes} minutes"), dry_run)
        entry["status"] = "done"
        entry["closed_at"] = iso_now()
        log(f"OK idle-closed {entry['issue_id']} for {session_id}")


def force_close(session_id: str, state: dict[str, Any], dry_run: bool) -> None:
    entry = state.setdefault("sessions", {}).get(session_id)
    if not entry or entry.get("status") == "done":
        return
    summary = {
        "session_id": session_id,
        "last_event_at": entry.get("last_seen_at"),
    }
    if set_issue_status(entry["issue_id"], "done", dry_run):
        add_comment(entry["issue_id"], close_comment(summary, "SessionEnd hook"), dry_run)
        entry["status"] = "done"
        entry["closed_at"] = iso_now()
        log(f"OK force-closed {entry['issue_id']} for {session_id}")


def scan(recent_days: int, idle_minutes: int, session_id: str | None, dry_run: bool) -> int:
    if not SESSIONS_ROOT.exists():
        log(f"ERROR sessions root missing: {SESSIONS_ROOT}")
        return 1

    state = load_state()
    existing_issues = list_existing_issues()
    files = session_files(recent_days=recent_days, session_id=session_id)

    created = reopened = closed = touched = 0
    for path in files:
        summary = parse_session_file(path)
        if not summary or not summary.get("session_id"):
            continue

        before = state.setdefault("sessions", {}).get(summary["session_id"], {}).get("status")
        had_issue = bool(state["sessions"].get(summary["session_id"]))
        ensure_issue(summary, state, existing_issues, dry_run)
        after_ensure = state["sessions"].get(summary["session_id"], {}).get("status")
        if not had_issue and after_ensure:
            created += 1
        elif before == "done" and after_ensure == "in_progress":
            reopened += 1

        before_close = after_ensure
        maybe_idle_close(summary, state, idle_minutes=idle_minutes, dry_run=dry_run)
        after_close = state["sessions"].get(summary["session_id"], {}).get("status")
        if before_close == "in_progress" and after_close == "done":
            closed += 1

        touched += 1

    if not dry_run:
        save_state(state)
    log(
        "scan complete "
        f"touched={touched} created={created} reopened={reopened} closed={closed} "
        f"recent_days={recent_days} idle_minutes={idle_minutes} session_id={session_id or '-'}"
    )
    return 0


def handle_hook(event_name: str, dry_run: bool) -> int:
    payload = read_stdin_json()
    session_id = payload.get("session_id") or payload.get("id")
    log(f"hook event={event_name} session_id={session_id or '-'}")

    if event_name == "SessionEnd" and session_id:
        rc = scan(recent_days=DEFAULT_RECENT_DAYS, idle_minutes=DEFAULT_IDLE_MINUTES, session_id=session_id, dry_run=dry_run)
        state = load_state()
        force_close(session_id, state, dry_run=dry_run)
        save_state(state)
        return rc

    return scan(
        recent_days=DEFAULT_RECENT_DAYS,
        idle_minutes=DEFAULT_IDLE_MINUTES,
        session_id=session_id,
        dry_run=dry_run,
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Track Codex sessions into Multica")
    subparsers = parser.add_subparsers(dest="command", required=True)

    scan_parser = subparsers.add_parser("scan", help="Scan Codex sessions and sync to Multica")
    scan_parser.add_argument("--recent-days", type=int, default=DEFAULT_RECENT_DAYS)
    scan_parser.add_argument("--idle-minutes", type=int, default=DEFAULT_IDLE_MINUTES)
    scan_parser.add_argument("--session-id", default=None)
    scan_parser.add_argument("--dry-run", action="store_true")

    hook_parser = subparsers.add_parser("hook", help="Handle a Codex hook callback")
    hook_parser.add_argument("event_name", choices=["SessionStart", "Stop", "SessionEnd"])
    hook_parser.add_argument("--dry-run", action="store_true")

    close_parser = subparsers.add_parser("force-close", help="Close a tracked session now")
    close_parser.add_argument("session_id")
    close_parser.add_argument("--dry-run", action="store_true")

    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.command == "scan":
        return scan(
            recent_days=args.recent_days,
            idle_minutes=args.idle_minutes,
            session_id=args.session_id,
            dry_run=args.dry_run,
        )
    if args.command == "hook":
        return handle_hook(args.event_name, dry_run=args.dry_run)
    if args.command == "force-close":
        state = load_state()
        force_close(args.session_id, state, dry_run=args.dry_run)
        save_state(state)
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
