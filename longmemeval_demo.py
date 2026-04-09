"""longmemeval_demo.py — CLI workflow to ingest and evaluate LongMemEval datasets.

Usage examples:
    uv run python longmemeval_demo.py
    uv run python longmemeval_demo.py --wiki-url http://localhost:8000 --bootstrap
    uv run python longmemeval_demo.py --download-only

Interactive commands:
    /download                      Download/update LongMemEval benchmark repo
    /install-dataset               Choose an installed dataset and ingest into wiki windows
    /run-test                      Choose a corpus, run Director tests, and score results
    /status                        Show local asset status
    /help                          Show command help
    quit / exit                    End session
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
    "https://github.com/THUDM/LongMemEval.git",
)
ASSET_ROOT = Path(os.environ.get("LONGMEMEVAL_ASSET_ROOT", ".longmemeval_assets"))
REPO_DIR = ASSET_ROOT / "LongMemEval"
DATASET_DIR = ASSET_ROOT / "datasets"
RESULTS_DIR = ASSET_ROOT / "results"

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
class SegmentProfile:
    name: str
    turns_per_window: int


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
        resp = self._http.post(
            f"{self._base}/conversations/windows/{window_id}/messages",
            json={"role": role, "content": content},
            headers=self._headers,
        )
        resp.raise_for_status()
        return resp.json()

    def flush(self, window_id: str) -> None:
        resp = self._http.post(
            f"{self._base}/conversations/windows/{window_id}/flush",
            headers=self._headers,
        )
        resp.raise_for_status()

    def get_window(self, window_id: str) -> dict[str, Any]:
        resp = self._http.get(
            f"{self._base}/conversations/windows/{window_id}",
            headers=self._headers,
        )
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
                    answer = evt.get("text", "")
        return answer

    def close(self) -> None:
        self._http.close()


def ensure_repo() -> Path:
    ASSET_ROOT.mkdir(parents=True, exist_ok=True)
    DATASET_DIR.mkdir(parents=True, exist_ok=True)
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)

    if REPO_DIR.exists():
        print(_c("[download] 更新 LongMemEval repo...", CYAN))
        cp = _run(["git", "pull", "--ff-only"], cwd=REPO_DIR)
        if cp.returncode != 0:
            raise RuntimeError(f"git pull 失敗: {cp.stderr.strip()}")
    else:
        print(_c("[download] 下載 LongMemEval repo...", CYAN))
        cp = _run(["git", "clone", LONGMEMEVAL_REPO_URL, str(REPO_DIR)])
        if cp.returncode != 0:
            raise RuntimeError(f"git clone 失敗: {cp.stderr.strip()}")

    return REPO_DIR


def discover_dataset_files(root: Path) -> list[Path]:
    candidates = sorted(
        [
            *root.rglob("*.json"),
            *root.rglob("*.jsonl"),
        ]
    )
    # 排除明顯非資料集的檔案
    return [p for p in candidates if "result" not in p.name.lower() and "report" not in p.name.lower()]


def choose_from_list(title: str, items: list[Path]) -> Path:
    if not items:
        raise RuntimeError(f"{title}：找不到可用項目")

    print(_c(f"\n{title}", BOLD, CYAN))
    for idx, item in enumerate(items, start=1):
        print(f"  {idx}. {item}")

    while True:
        raw = input(_c("請輸入編號: ", GREEN, BOLD)).strip()
        if raw.isdigit() and 1 <= int(raw) <= len(items):
            return items[int(raw) - 1]
        print(_c("無效輸入，請重試。", YELLOW))


def load_records(path: Path) -> list[dict[str, Any]]:
    if path.suffix == ".jsonl":
        records = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    else:
        raw = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(raw, list):
            records = raw
        elif isinstance(raw, dict):
            for key in ("data", "examples", "records", "items"):
                if isinstance(raw.get(key), list):
                    return raw[key]
            records = [raw]
        else:
            records = []
    return [r for r in records if isinstance(r, dict)]


def extract_turns(record: dict[str, Any]) -> list[dict[str, str]]:
    for key in ("messages", "conversation", "dialogue", "turns"):
        value = record.get(key)
        if isinstance(value, list):
            turns: list[dict[str, str]] = []
            for item in value:
                if not isinstance(item, dict):
                    continue
                role = str(item.get("role") or item.get("speaker") or "user").lower()
                content = str(item.get("content") or item.get("text") or item.get("utterance") or "").strip()
                if content:
                    turns.append({"role": "assistant" if "assistant" in role or role in {"bot", "model"} else "user", "content": content})
            if turns:
                return turns

    context = str(record.get("context") or record.get("history") or "").strip()
    question = str(record.get("question") or record.get("query") or "").strip()
    answer = str(record.get("answer") or record.get("target") or record.get("gold") or "").strip()

    turns: list[dict[str, str]] = []
    if context:
        turns.append({"role": "user", "content": context})
    if question:
        turns.append({"role": "user", "content": question})
    if answer:
        turns.append({"role": "assistant", "content": answer})
    return turns


def derive_profile(records: list[dict[str, Any]]) -> SegmentProfile:
    sample_turns = [extract_turns(r) for r in records[:20]]
    flat = [t for turns in sample_turns for t in turns]
    avg_len = (sum(len(t["content"]) for t in flat) / len(flat)) if flat else 120

    if avg_len > 320:
        return SegmentProfile(name="long-context", turns_per_window=3)
    if avg_len > 180:
        return SegmentProfile(name="medium-context", turns_per_window=5)
    return SegmentProfile(name="short-context", turns_per_window=8)


def segment_turns(turns: list[dict[str, str]], turns_per_window: int) -> list[list[dict[str, str]]]:
    if turns_per_window <= 0:
        raise ValueError("turns_per_window must be > 0")
    return [turns[i : i + turns_per_window] for i in range(0, len(turns), turns_per_window)]


def install_dataset_to_wiki(wiki: WikiClient, dataset_path: Path) -> None:
    records = load_records(dataset_path)
    if not records:
        raise RuntimeError("資料集沒有可處理記錄")

    profile = derive_profile(records)
    print(_c(f"[dataset] profile={profile.name}, turns/window={profile.turns_per_window}", CYAN))

    for ridx, record in enumerate(records, start=1):
        turns = extract_turns(record)
        if not turns:
            continue
        for pidx, phase in enumerate(segment_turns(turns, profile.turns_per_window), start=1):
            window = wiki.create_window(
                source_id=f"longmemeval:{dataset_path.stem}:record-{ridx}:phase-{pidx}"
            )
            window_id = window["id"]
            for msg in phase:
                wiki.add_message(window_id, msg["role"], msg["content"])
            wiki.flush(window_id)
            wait_for_flush_complete(wiki, window_id)
            print(_c(f"  - flushed record#{ridx} phase#{pidx} window={window_id[:8]}...", DIM))


def wait_for_flush_complete(wiki: WikiClient, window_id: str, timeout_seconds: int = 300) -> None:
    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        status = wiki.get_window(window_id).get("status")
        if status in {"cleared", "active"}:
            return
        time.sleep(1.5)
    raise TimeoutError(f"window {window_id} flush timeout")


def extract_test_case(record: dict[str, Any], index: int) -> dict[str, str]:
    question = str(record.get("question") or record.get("query") or record.get("prompt") or "").strip()
    ground_truth = str(record.get("answer") or record.get("target") or record.get("gold") or "").strip()
    context = str(record.get("context") or record.get("history") or "").strip()

    if not question:
        turns = extract_turns(record)
        if turns:
            question = turns[-1]["content"]
    if not question:
        question = f"[sample-{index}] Please summarize the memory in this record."

    return {
        "id": str(record.get("id") or record.get("qid") or f"sample-{index}"),
        "question": question,
        "ground_truth": ground_truth,
        "context": context,
    }


def run_longmemeval_tests(wiki_url: str, corpus_path: Path, out_dir: Path, limit: int | None = None) -> Path:
    records = load_records(corpus_path)
    if not records:
        raise RuntimeError("語料庫沒有可測試資料")

    out_dir.mkdir(parents=True, exist_ok=True)
    pred_path = out_dir / f"predictions_{corpus_path.stem}.jsonl"

    director = DirectorClient(wiki_url)
    session_id = director.create_session(title=f"LongMemEval::{corpus_path.stem}")

    try:
        with pred_path.open("w", encoding="utf-8") as f:
            for idx, record in enumerate(records, start=1):
                if limit and idx > limit:
                    break
                case = extract_test_case(record, idx)

                prompt = textwrap.dedent(
                    f"""
                    請根據以下記憶任務作答，避免多餘前言。

                    [Context]
                    {case['context']}

                    [Question]
                    {case['question']}
                    """
                ).strip()

                prediction = director.chat(session_id, prompt).strip()
                row = {
                    "id": case["id"],
                    "question": case["question"],
                    "prediction": prediction,
                    "ground_truth": case["ground_truth"],
                    "dataset": corpus_path.stem,
                }
                f.write(json.dumps(row, ensure_ascii=False) + "\n")
                print(_c(f"  - tested {case['id']}", DIM))
    finally:
        director.close()

    return pred_path


def simple_score(pred_path: Path) -> dict[str, Any]:
    rows = [json.loads(line) for line in pred_path.read_text(encoding="utf-8").splitlines() if line.strip()]
    if not rows:
        return {"total": 0, "exact_match": 0.0}

    hit = 0
    valid = 0
    for row in rows:
        gold = str(row.get("ground_truth") or "").strip().lower()
        pred = str(row.get("prediction") or "").strip().lower()
        if not gold:
            continue
        valid += 1
        if gold == pred:
            hit += 1

    return {
        "total": len(rows),
        "scored": valid,
        "exact_match": round((hit / valid), 4) if valid else 0.0,
    }


def try_run_official_scorer(pred_path: Path, report_path: Path) -> tuple[bool, str]:
    candidates = [
        REPO_DIR / "scripts" / "evaluate.py",
        REPO_DIR / "eval" / "evaluate.py",
        REPO_DIR / "evaluation" / "evaluate.py",
    ]

    for script in candidates:
        if not script.exists():
            continue
        cmd = [sys.executable, str(script), "--pred", str(pred_path), "--output", str(report_path)]
        cp = _run(cmd)
        if cp.returncode == 0:
            return True, f"官方評分腳本成功: {script}"
    return False, "找不到可直接呼叫的官方 evaluate.py，改用內建評分。"


def write_report(pred_path: Path, out_dir: Path) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    report_path = out_dir / f"report_{pred_path.stem}.json"

    ok, note = try_run_official_scorer(pred_path, report_path)
    if ok:
        return report_path

    metrics = simple_score(pred_path)
    payload = {
        "scorer": "built-in-fallback",
        "note": note,
        "metrics": metrics,
        "prediction_file": str(pred_path),
    }
    report_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
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
                print(_c("下載/更新完成。", GREEN))
            elif command == "/status":
                print(f"repo: {'ok' if REPO_DIR.exists() else 'missing'} @ {REPO_DIR}")
                print(f"datasets: {len(discover_dataset_files(DATASET_DIR))} files")
                print(f"results: {len(list(RESULTS_DIR.glob('*.json*')))} files")
            elif command == "/install-dataset":
                ensure_repo()
                src_files = discover_dataset_files(REPO_DIR)
                selected = choose_from_list("選擇要安裝的資料集", src_files)
                target = DATASET_DIR / selected.name
                target.write_text(selected.read_text(encoding="utf-8"), encoding="utf-8")

                wiki = WikiClient(wiki_url)
                try:
                    install_dataset_to_wiki(wiki, target)
                finally:
                    wiki.close()
                print(_c("資料集已安裝並完成分段 flush。", GREEN))
            elif command == "/run-test":
                ensure_repo()
                corpora = discover_dataset_files(DATASET_DIR)
                selected = choose_from_list("選擇要測試的語料", corpora)
                ts = time.strftime("%Y%m%d-%H%M%S", time.gmtime())
                run_dir = RESULTS_DIR / ts
                pred = run_longmemeval_tests(wiki_url, selected, run_dir)
                report = write_report(pred, run_dir)
                print(_c(f"測試完成\n  predictions: {pred}\n  report: {report}", GREEN))
            elif not command:
                continue
            else:
                print(_c("未知命令，輸入 /help 取得幫助。", YELLOW))
        except Exception as exc:
            print(_c(f"[error] {exc}", RED))


def main() -> None:
    parser = argparse.ArgumentParser(description="Run LongMemEval workflow with wolfina-wiki")
    parser.add_argument("--wiki-url", default="http://localhost:8000")
    parser.add_argument("--bootstrap", action="store_true", help="launch 時先下載/更新 LongMemEval")
    parser.add_argument("--download-only", action="store_true")
    args = parser.parse_args()

    if args.download_only:
        ensure_repo()
        print("done")
        return

    # 檢查 wiki 連線（失敗不直接中止，仍可先 /download）
    with httpx.Client(timeout=5.0) as http:
        try:
            http.get(f"{args.wiki_url.rstrip('/')}/docs")
        except Exception:
            print(_c("[warning] 暫時無法連上 wiki API，你仍可先下載資料。", YELLOW))

    interactive_shell(args.wiki_url, bootstrap=args.bootstrap)


if __name__ == "__main__":
    main()
