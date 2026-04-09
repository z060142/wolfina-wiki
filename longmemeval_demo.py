"""longmemeval_demo.py — LongMemEval workflow CLI for wolfina-wiki.

Aligned with official LongMemEval instructions:
- Repo: https://github.com/xiaowu0162/LongMemEval
- Data files under LongMemEval/data/
- Evaluation script: LongMemEval/src/evaluation/evaluate_qa.py

Usage:
    uv run python longmemeval_demo.py --bootstrap

Interactive commands:
    /download         clone/pull LongMemEval repo
    /install-dataset  download official cleaned datasets and ingest into wiki windows
    /run-test         run Director on selected corpus and call official evaluate_qa.py
    /status           show local status
    /help             help
    quit / exit       leave
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import textwrap
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

LONGMEMEVAL_REPO_URL = os.environ.get(
    "LONGMEMEVAL_REPO_URL",
    "https://github.com/xiaowu0162/LongMemEval.git",
)
ASSET_ROOT = Path(os.environ.get("LONGMEMEVAL_ASSET_ROOT", ".longmemeval_assets"))
REPO_DIR = ASSET_ROOT / "LongMemEval"
DATA_DIR = REPO_DIR / "data"
RESULTS_DIR = ASSET_ROOT / "results"

HF_BASE = "https://huggingface.co/datasets/xiaowu0162/longmemeval-cleaned/resolve/main"
OFFICIAL_FILES = {
    "longmemeval_oracle.json": f"{HF_BASE}/longmemeval_oracle.json",
    "longmemeval_s_cleaned.json": f"{HF_BASE}/longmemeval_s_cleaned.json",
    "longmemeval_m_cleaned.json": f"{HF_BASE}/longmemeval_m_cleaned.json",
}

MAX_STAGE_CHARS = int(os.environ.get("LONGMEMEVAL_MAX_STAGE_CHARS", "5000"))
WINDOW_COOLDOWN_SECONDS = float(os.environ.get("LONGMEMEVAL_WINDOW_COOLDOWN_SECONDS", "1.5"))
MESSAGE_COOLDOWN_SECONDS = float(os.environ.get("LONGMEMEVAL_MESSAGE_COOLDOWN_SECONDS", "0.2"))
RECORD_COOLDOWN_SECONDS = float(os.environ.get("LONGMEMEVAL_RECORD_COOLDOWN_SECONDS", "0.8"))
FLUSH_MAX_CHARS = int(os.environ.get("LONGMEMEVAL_FLUSH_MAX_CHARS", "5000"))
FLUSH_MAX_TURNS = int(os.environ.get("LONGMEMEVAL_FLUSH_MAX_TURNS", "10"))
MAX_CONCURRENT_FLUSHES = int(os.environ.get("LONGMEMEVAL_MAX_CONCURRENT_FLUSHES", "1"))

RESET = "\033[0m"
BOLD = "\033[1m"
DIM = "\033[2m"
GREEN = "\033[32m"
CYAN = "\033[36m"
YELLOW = "\033[33m"
RED = "\033[31m"


def _c(text: str, *codes: str) -> str:
    if sys.stdout.isatty():
        return "".join(codes) + text + RESET
    return text


def _run(cmd: list[str], cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        cmd,
        cwd=str(cwd) if cwd else None,
        text=True,
        capture_output=True,
        check=False,
    )


@dataclass
class StageProfile:
    name: str
    sessions_per_stage: int


class WikiClient:
    def __init__(self, base_url: str, agent_id: str = "longmemeval-demo") -> None:
        self._base = base_url.rstrip("/")
        self._headers = {"X-Agent-ID": agent_id}
        self._http = httpx.Client(timeout=60.0)

    def create_window(self, source_id: str) -> dict[str, Any]:
        resp = self._http.post(
            f"{self._base}/conversations/windows",
            json={"external_source_id": source_id},
            headers=self._headers,
        )
        resp.raise_for_status()
        return resp.json()

    def add_message(self, window_id: str, role: str, content: str) -> dict[str, Any]:
        for attempt in range(7):
            resp = self._http.post(
                f"{self._base}/conversations/windows/{window_id}/messages",
                json={"role": role, "content": content},
                headers=self._headers,
            )
            if resp.status_code == 409:
                time.sleep(min(8.0, 0.5 * (2 ** attempt)))
                continue
            resp.raise_for_status()
            return resp.json()
        raise RuntimeError(f"add_message 連續衝突失敗: window={window_id}")

    def flush(self, window_id: str) -> None:
        for attempt in range(7):
            resp = self._http.post(f"{self._base}/conversations/windows/{window_id}/flush", headers=self._headers)
            if resp.status_code == 409:
                time.sleep(min(8.0, 0.5 * (2 ** attempt)))
                continue
            resp.raise_for_status()
            return
        raise RuntimeError(f"flush 連續衝突失敗: window={window_id}")

    def get_window(self, window_id: str) -> dict[str, Any]:
        resp = self._http.get(f"{self._base}/conversations/windows/{window_id}", headers=self._headers)
        resp.raise_for_status()
        return resp.json()

    def list_windows(self, status: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
        params: dict[str, Any] = {"limit": limit}
        if status:
            params["status"] = status
        resp = self._http.get(f"{self._base}/conversations/windows", headers=self._headers, params=params)
        resp.raise_for_status()
        return resp.json()

    def close(self) -> None:
        self._http.close()


class DirectorClient:
    def __init__(self, base_url: str) -> None:
        self._base = base_url.rstrip("/")
        self._http = httpx.Client(timeout=300.0)

    def create_session(self, title: str) -> str:
        resp = self._http.post(f"{self._base}/director/sessions", json={"title": title})
        resp.raise_for_status()
        return resp.json()["id"]

    def chat(self, session_id: str, message: str) -> str:
        answer = ""
        with self._http.stream(
            "POST",
            f"{self._base}/director/sessions/{session_id}/chat",
            json={"message": message},
        ) as resp:
            resp.raise_for_status()
            for line in resp.iter_lines():
                if not line.startswith("data: "):
                    continue
                raw = line[6:].strip()
                if not raw:
                    continue
                try:
                    evt = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                if evt.get("type") == "reply":
                    answer = str(evt.get("text") or "")
        return answer

    def close(self) -> None:
        self._http.close()


def ensure_repo() -> Path:
    ASSET_ROOT.mkdir(parents=True, exist_ok=True)
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)

    if REPO_DIR.exists():
        cp = _run(["git", "pull", "--ff-only"], cwd=REPO_DIR)
        if cp.returncode != 0:
            raise RuntimeError(f"git pull 失敗: {cp.stderr.strip()}")
    else:
        cp = _run(["git", "clone", LONGMEMEVAL_REPO_URL, str(REPO_DIR)])
        if cp.returncode != 0:
            raise RuntimeError(f"git clone 失敗: {cp.stderr.strip()}")

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    return REPO_DIR


def download_official_datasets(force: bool = False) -> list[Path]:
    ensure_repo()
    saved: list[Path] = []
    with httpx.Client(timeout=120.0, follow_redirects=True) as client:
        for filename, url in OFFICIAL_FILES.items():
            target = DATA_DIR / filename
            if target.exists() and not force:
                print(_c(f"[dataset] skip existing {filename}", DIM))
                saved.append(target)
                continue
            print(_c(f"[dataset] download {filename}", CYAN))
            resp = client.get(url)
            resp.raise_for_status()
            target.write_bytes(resp.content)
            saved.append(target)
    return saved


def list_official_dataset_files() -> list[Path]:
    return [DATA_DIR / name for name in OFFICIAL_FILES if (DATA_DIR / name).exists()]


def choose_from_list(title: str, items: list[Path]) -> Path:
    if not items:
        raise RuntimeError(f"{title}：找不到可用項目")
    print(_c(f"\n{title}", BOLD, CYAN))
    for idx, item in enumerate(items, start=1):
        print(f"  {idx}. {item.name}")
    while True:
        raw = input(_c("請輸入編號: ", GREEN, BOLD)).strip()
        if raw.isdigit() and 1 <= int(raw) <= len(items):
            return items[int(raw) - 1]
        print(_c("無效輸入，請重試。", YELLOW))


def _iter_records_stream(path: Path) -> Any:
    """Stream records from a large JSON array file.

    LongMemEval_M can be several GB, so this parser avoids loading the whole
    file into memory.
    """
    decoder = json.JSONDecoder()
    with path.open("r", encoding="utf-8") as f:
        buf = ""
        idx = 0
        started = False
        eof = False

        while True:
            if idx >= len(buf) and eof:
                return
            if idx >= len(buf) - 1 and not eof:
                chunk = f.read(1024 * 1024)
                if chunk:
                    if idx > 0:
                        buf = buf[idx:] + chunk
                        idx = 0
                    else:
                        buf += chunk
                else:
                    eof = True

            while idx < len(buf) and buf[idx].isspace():
                idx += 1
            if idx >= len(buf):
                continue

            if not started:
                if buf[idx] != "[":
                    raise ValueError(f"{path.name} 不是 JSON array 格式")
                started = True
                idx += 1
                continue

            while idx < len(buf) and buf[idx].isspace():
                idx += 1
            if idx >= len(buf):
                continue
            if buf[idx] == "]":
                return
            if buf[idx] == ",":
                idx += 1
                continue

            try:
                obj, end = decoder.raw_decode(buf, idx)
                idx = end
                if isinstance(obj, dict):
                    yield obj
            except json.JSONDecodeError:
                if eof:
                    raise
                chunk = f.read(1024 * 1024)
                if not chunk:
                    eof = True
                else:
                    if idx > 0:
                        buf = buf[idx:] + chunk
                        idx = 0
                    else:
                        buf += chunk


def iter_records(path: Path, limit: int | None = None) -> Any:
    count = 0
    for rec in _iter_records_stream(path):
        yield rec
        count += 1
        if limit and count >= limit:
            return


def stage_profile_for_dataset(path: Path) -> StageProfile:
    name = path.name.lower()
    if "_m_" in name or name.endswith("m_cleaned.json"):
        return StageProfile(name="LongMemEval_M", sessions_per_stage=8)
    if "oracle" in name:
        return StageProfile(name="LongMemEval_Oracle", sessions_per_stage=4)
    return StageProfile(name="LongMemEval_S", sessions_per_stage=3)


def flatten_session(session: Any) -> list[dict[str, str]]:
    if not isinstance(session, list):
        return []
    turns: list[dict[str, str]] = []
    for turn in session:
        if not isinstance(turn, dict):
            continue
        role = str(turn.get("role") or "user").lower()
        content = str(turn.get("content") or "").strip()
        if not content:
            continue
        turns.append({"role": "assistant" if role == "assistant" else "user", "content": content})
    return turns


def segment_haystack_sessions(record: dict[str, Any], sessions_per_stage: int) -> list[list[dict[str, str]]]:
    sessions = record.get("haystack_sessions")
    if not isinstance(sessions, list):
        return []
    session_ids = record.get("haystack_session_ids")
    session_dates = record.get("haystack_dates")

    stage_turns: list[list[dict[str, str]]] = []
    for i in range(0, len(sessions), sessions_per_stage):
        slice_sessions = sessions[i : i + sessions_per_stage]
        turns: list[dict[str, str]] = []
        for j, sess in enumerate(slice_sessions, start=i):
            sid = session_ids[j] if isinstance(session_ids, list) and j < len(session_ids) else f"session_{j+1}"
            sdate = session_dates[j] if isinstance(session_dates, list) and j < len(session_dates) else ""
            turns.append({"role": "user", "content": f"[session_meta] id={sid} date={sdate}"})
            turns.extend(flatten_session(sess))
        if turns:
            stage_turns.append(turns)
    return stage_turns


def iter_flush_batches(
    turns: list[dict[str, str]],
    *,
    max_chars: int = FLUSH_MAX_CHARS,
    max_turns: int = FLUSH_MAX_TURNS,
) -> list[list[dict[str, str]]]:
    batches: list[list[dict[str, str]]] = []
    current: list[dict[str, str]] = []
    char_count = 0
    turn_count = 0

    for msg in turns:
        msg_chars = len(msg.get("content", ""))
        need_split = current and (char_count + msg_chars > max_chars or turn_count >= max_turns)
        if need_split:
            batches.append(current)
            current = []
            char_count = 0
            turn_count = 0

        current.append(msg)
        char_count += msg_chars
        turn_count += 1

    if current:
        batches.append(current)
    return batches


def wait_for_flush_complete(wiki: WikiClient, window_id: str, timeout_seconds: int = 300) -> None:
    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        status = wiki.get_window(window_id).get("status")
        if status in {"cleared", "active"}:
            return
        time.sleep(1.0)
    raise TimeoutError(f"window {window_id} flush timeout")


def wait_for_flush_slot(
    wiki: WikiClient,
    max_concurrent_flushes: int = MAX_CONCURRENT_FLUSHES,
    timeout_seconds: int = 300,
) -> None:
    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        flushing = wiki.list_windows(status="flushing", limit=200)
        if len(flushing) < max_concurrent_flushes:
            return
        time.sleep(1.0)
    raise TimeoutError("等待 flush slot 超時")


def install_dataset_to_wiki(wiki: WikiClient, dataset_path: Path, limit: int | None = None) -> None:
    profile = stage_profile_for_dataset(dataset_path)
    print(_c(f"[dataset] {dataset_path.name} -> {profile.name}, sessions/stage={profile.sessions_per_stage}", CYAN))

    for ridx, rec in enumerate(iter_records(dataset_path, limit=limit), start=1):
        qid = str(rec.get("question_id") or f"q-{ridx}")
        stages = segment_haystack_sessions(rec, sessions_per_stage=profile.sessions_per_stage)
        for sidx, turns in enumerate(stages, start=1):
            # 每個 stage 使用不同 source_id，避免同 source 長時間聚合在同窗口
            window = wiki.create_window(source_id=f"longmemeval:{dataset_path.stem}:{qid}:stage-{sidx}:{int(time.time())}")
            window_id = window["id"]
            batches = iter_flush_batches(turns, max_chars=FLUSH_MAX_CHARS, max_turns=FLUSH_MAX_TURNS)
            for bidx, batch in enumerate(batches, start=1):
                wait_for_flush_slot(wiki, max_concurrent_flushes=MAX_CONCURRENT_FLUSHES)
                flush_triggered = False
                for msg in batch:
                    result = wiki.add_message(window_id, msg["role"], msg["content"])
                    flush_triggered = flush_triggered or bool(result.get("flush_triggered"))
                    time.sleep(MESSAGE_COOLDOWN_SECONDS)
                if flush_triggered:
                    wait_for_flush_complete(wiki, window_id)
                else:
                    # 若未達到自動 flush 門檻，手動 flush 當前批次
                    wiki.flush(window_id)
                    wait_for_flush_complete(wiki, window_id)
                print(_c(f"    stage#{sidx} batch#{bidx} flushed", DIM))
                time.sleep(WINDOW_COOLDOWN_SECONDS)
        print(_c(f"  - ingested {qid} with {len(stages)} stages", DIM))
        time.sleep(RECORD_COOLDOWN_SECONDS)


def _format_session_block(session: Any, idx: int) -> str:
    turns = flatten_session(session)
    if not turns:
        return ""
    lines = [f"Session {idx}:"]
    for t in turns:
        lines.append(f"- {t['role']}: {t['content']}")
    return "\n".join(lines)


def build_staged_history_chunks(
    record: dict[str, Any],
    *,
    max_chars: int,
    max_sessions_per_stage: int,
) -> list[str]:
    sessions = record.get("haystack_sessions")
    if not isinstance(sessions, list):
        return []

    chunks: list[str] = []
    current: list[str] = []
    current_chars = 0
    current_count = 0

    session_ids = record.get("haystack_session_ids")
    session_dates = record.get("haystack_dates")

    for idx, session in enumerate(sessions, start=1):
        block = _format_session_block(session, idx)
        if not block:
            continue
        sid = session_ids[idx - 1] if isinstance(session_ids, list) and idx - 1 < len(session_ids) else f"session_{idx}"
        sdate = session_dates[idx - 1] if isinstance(session_dates, list) and idx - 1 < len(session_dates) else ""
        block = f"[session_meta] id={sid} date={sdate}\n{block}"
        block_len = len(block)

        will_overflow_chars = current and (current_chars + block_len + 2 > max_chars)
        will_overflow_count = current and (current_count >= max_sessions_per_stage)
        if will_overflow_chars or will_overflow_count:
            chunks.append("\n\n".join(current))
            current = []
            current_chars = 0
            current_count = 0

        if block_len > max_chars:
            # 單一 session 超長，硬切片避免超過單次輸入上限
            for i in range(0, block_len, max_chars):
                piece = block[i : i + max_chars]
                if current:
                    chunks.append("\n\n".join(current))
                    current = []
                    current_chars = 0
                    current_count = 0
                chunks.append(piece)
            continue

        current.append(block)
        current_chars += block_len + 2
        current_count += 1

    if current:
        chunks.append("\n\n".join(current))
    return chunks


def build_question_prompt(record: dict[str, Any]) -> str:
    qid = str(record.get("question_id") or "unknown")
    question_date = str(record.get("question_date") or "")
    question = str(record.get("question") or "").strip()

    return textwrap.dedent(
        f"""
        你是記憶測試助手。你已經在前面讀過多個歷史片段。
        現在請直接回答問題；若歷史裡沒有答案，請明確回答不知道。

        [Question ID]
        {qid}

        [Question Date]
        {question_date}

        [Question]
        {question}
        """
    ).strip()


def run_longmemeval_tests(wiki_url: str, dataset_path: Path, out_dir: Path, limit: int | None = None) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    hypothesis_path = out_dir / f"hypothesis_{dataset_path.stem}.jsonl"
    debug_path = out_dir / f"debug_{dataset_path.stem}.jsonl"

    director = DirectorClient(wiki_url)
    try:
        with hypothesis_path.open("w", encoding="utf-8") as hypo_f, debug_path.open("w", encoding="utf-8") as dbg_f:
            for idx, rec in enumerate(iter_records(dataset_path, limit=limit), start=1):
                qid = str(rec.get("question_id") or f"q-{idx}")
                sid = director.create_session(title=f"LongMemEval::{qid}")

                profile = stage_profile_for_dataset(dataset_path)
                chunks = build_staged_history_chunks(
                    rec,
                    max_chars=MAX_STAGE_CHARS,
                    max_sessions_per_stage=profile.sessions_per_stage,
                )
                for cidx, chunk in enumerate(chunks, start=1):
                    memory_prompt = textwrap.dedent(
                        f"""
                        這是記憶歷史片段 {cidx}/{len(chunks)}，請先記住，不要回答分析。
                        只需回覆：已記住。

                        [History Chunk]
                        {chunk}
                        """
                    ).strip()
                    director.chat(sid, memory_prompt)
                    time.sleep(MESSAGE_COOLDOWN_SECONDS)

                prompt = build_question_prompt(rec)
                hypothesis = director.chat(sid, prompt).strip()

                hypo_row = {"question_id": qid, "hypothesis": hypothesis}
                hypo_f.write(json.dumps(hypo_row, ensure_ascii=False) + "\n")

                dbg_row = {
                    "question_id": qid,
                    "question": rec.get("question"),
                    "answer": rec.get("answer"),
                    "hypothesis": hypothesis,
                }
                dbg_f.write(json.dumps(dbg_row, ensure_ascii=False) + "\n")
                print(_c(f"  - tested {qid}", DIM))
                time.sleep(RECORD_COOLDOWN_SECONDS)
    finally:
        director.close()

    return hypothesis_path


def run_official_evaluation(hypothesis_path: Path, dataset_path: Path) -> tuple[bool, str, Path]:
    eval_dir = REPO_DIR / "src" / "evaluation"
    eval_script = eval_dir / "evaluate_qa.py"
    if not eval_script.exists():
        return False, "找不到官方 evaluate_qa.py", hypothesis_path.with_suffix(".log")

    model_name = os.environ.get("LONGMEMEVAL_EVAL_MODEL", "gpt-4o")
    cmd = [sys.executable, "evaluate_qa.py", model_name, str(hypothesis_path), str(dataset_path)]
    cp = _run(cmd, cwd=eval_dir)

    log_path = Path(str(hypothesis_path) + ".log")
    if cp.returncode != 0:
        msg = cp.stderr.strip() or cp.stdout.strip() or "evaluate_qa.py failed"
        return False, msg, log_path

    return True, "官方評分完成", log_path


def write_report(hypothesis_path: Path, dataset_path: Path, out_dir: Path) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    ok, note, log_path = run_official_evaluation(hypothesis_path, dataset_path)

    report = {
        "dataset": str(dataset_path),
        "hypothesis_file": str(hypothesis_path),
        "official_eval_ok": ok,
        "note": note,
        "log_file": str(log_path),
        "date_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }

    report_path = out_dir / f"report_{dataset_path.stem}.json"
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return report_path


def interactive_shell(wiki_url: str, bootstrap: bool) -> None:
    print(_c("LongMemEval Plugin Demo", BOLD, CYAN))
    print(_c(f"wiki-url: {wiki_url}", DIM))
    print(_c("輸入 /help 查看命令。", DIM))

    if bootstrap:
        ensure_repo()

    while True:
        try:
            command = input(_c("lme> ", GREEN, BOLD)).strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break

        if command in {"quit", "exit"}:
            break

        if command in {"/help", "help"}:
            print("/download | /install-dataset | /run-test | /status | quit")
            continue

        try:
            if command == "/download":
                ensure_repo()
                print(_c("LongMemEval repo 已完成下載/更新。", GREEN))

            elif command == "/install-dataset":
                files = download_official_datasets()
                selected = choose_from_list("選擇要匯入到 wiki 的資料集", files)
                wiki = WikiClient(wiki_url)
                try:
                    install_dataset_to_wiki(wiki, selected)
                finally:
                    wiki.close()
                print(_c("資料集匯入完成（已分 stage flush）。", GREEN))

            elif command == "/run-test":
                ensure_repo()
                choices = list_official_dataset_files()
                selected = choose_from_list("選擇要測試的語料", choices)
                run_dir = RESULTS_DIR / time.strftime("%Y%m%d-%H%M%S", time.gmtime())
                hypo = run_longmemeval_tests(wiki_url, selected, run_dir)
                report = write_report(hypo, selected, run_dir)
                print(_c(f"測試完成\n  hypothesis: {hypo}\n  report: {report}", GREEN))

            elif command == "/status":
                print(f"repo: {'ok' if REPO_DIR.exists() else 'missing'} @ {REPO_DIR}")
                files = list_official_dataset_files()
                print(f"official data files: {len(files)}")
                for p in files:
                    print(f"  - {p.name}")
                print(f"results dirs: {len([p for p in RESULTS_DIR.glob('*') if p.is_dir()])}")

            elif not command:
                continue
            else:
                print(_c("未知命令，輸入 /help 取得幫助。", YELLOW))

        except Exception as exc:
            print(_c(f"[error] {exc}", RED))


def main() -> None:
    parser = argparse.ArgumentParser(description="Run official LongMemEval workflow with wolfina-wiki")
    parser.add_argument("--wiki-url", default="http://localhost:8000")
    parser.add_argument("--bootstrap", action="store_true", help="啟動時先 clone/pull LongMemEval")
    parser.add_argument("--download-only", action="store_true", help="只下載/更新 LongMemEval repo")
    args = parser.parse_args()

    if args.download_only:
        ensure_repo()
        print("done")
        return

    with httpx.Client(timeout=5.0) as client:
        try:
            client.get(f"{args.wiki_url.rstrip('/')}/docs")
        except Exception:
            print(_c("[warning] 暫時無法連上 wiki API，你仍可先 /download。", YELLOW))

    interactive_shell(args.wiki_url, bootstrap=args.bootstrap)


if __name__ == "__main__":
    main()
