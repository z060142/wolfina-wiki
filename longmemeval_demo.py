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

MAX_STAGE_CHARS = int(os.environ.get("LONGMEMEVAL_MAX_STAGE_CHARS", "12000"))

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

    def add_message(self, window_id: str, role: str, content: str) -> None:
        resp = self._http.post(
            f"{self._base}/conversations/windows/{window_id}/messages",
            json={"role": role, "content": content},
            headers=self._headers,
        )
        resp.raise_for_status()

    def flush(self, window_id: str) -> None:
        resp = self._http.post(f"{self._base}/conversations/windows/{window_id}/flush", headers=self._headers)
        resp.raise_for_status()

    def get_window(self, window_id: str) -> dict[str, Any]:
        resp = self._http.get(f"{self._base}/conversations/windows/{window_id}", headers=self._headers)
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


def download_official_datasets() -> list[Path]:
    ensure_repo()
    saved: list[Path] = []
    with httpx.Client(timeout=120.0, follow_redirects=True) as client:
        for filename, url in OFFICIAL_FILES.items():
            target = DATA_DIR / filename
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


def load_records(path: Path) -> list[dict[str, Any]]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, list):
        raise ValueError(f"{path.name} 不是 list 格式")
    return [x for x in raw if isinstance(x, dict)]


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

    stage_turns: list[list[dict[str, str]]] = []
    for i in range(0, len(sessions), sessions_per_stage):
        slice_sessions = sessions[i : i + sessions_per_stage]
        turns: list[dict[str, str]] = []
        for sess in slice_sessions:
            turns.extend(flatten_session(sess))
        if turns:
            stage_turns.append(turns)
    return stage_turns


def wait_for_flush_complete(wiki: WikiClient, window_id: str, timeout_seconds: int = 300) -> None:
    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        status = wiki.get_window(window_id).get("status")
        if status in {"cleared", "active"}:
            return
        time.sleep(1.0)
    raise TimeoutError(f"window {window_id} flush timeout")


def install_dataset_to_wiki(wiki: WikiClient, dataset_path: Path, limit: int | None = None) -> None:
    records = load_records(dataset_path)
    profile = stage_profile_for_dataset(dataset_path)
    print(_c(f"[dataset] {dataset_path.name} -> {profile.name}, sessions/stage={profile.sessions_per_stage}", CYAN))

    for ridx, rec in enumerate(records, start=1):
        if limit and ridx > limit:
            break
        qid = str(rec.get("question_id") or f"q-{ridx}")
        stages = segment_haystack_sessions(rec, sessions_per_stage=profile.sessions_per_stage)
        for sidx, turns in enumerate(stages, start=1):
            window = wiki.create_window(source_id=f"longmemeval:{dataset_path.stem}:{qid}:stage-{sidx}")
            window_id = window["id"]
            for msg in turns:
                wiki.add_message(window_id, msg["role"], msg["content"])
            wiki.flush(window_id)
            wait_for_flush_complete(wiki, window_id)
        print(_c(f"  - ingested {qid} with {len(stages)} stages", DIM))


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

    for idx, session in enumerate(sessions, start=1):
        block = _format_session_block(session, idx)
        if not block:
            continue
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
    records = load_records(dataset_path)
    out_dir.mkdir(parents=True, exist_ok=True)
    hypothesis_path = out_dir / f"hypothesis_{dataset_path.stem}.jsonl"
    debug_path = out_dir / f"debug_{dataset_path.stem}.jsonl"

    director = DirectorClient(wiki_url)
    try:
        with hypothesis_path.open("w", encoding="utf-8") as hypo_f, debug_path.open("w", encoding="utf-8") as dbg_f:
            for idx, rec in enumerate(records, start=1):
                if limit and idx > limit:
                    break
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
