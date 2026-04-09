from pathlib import Path

from longmemeval_demo import (
    build_question_prompt,
    build_staged_history_chunks,
    flatten_session,
    segment_haystack_sessions,
    stage_profile_for_dataset,
)


def test_stage_profile_by_filename() -> None:
    assert stage_profile_for_dataset(Path("longmemeval_m_cleaned.json")).sessions_per_stage == 8
    assert stage_profile_for_dataset(Path("longmemeval_oracle.json")).sessions_per_stage == 4
    assert stage_profile_for_dataset(Path("longmemeval_s_cleaned.json")).sessions_per_stage == 3


def test_flatten_session_keeps_roles() -> None:
    session = [
        {"role": "user", "content": "hello"},
        {"role": "assistant", "content": "hi"},
    ]
    turns = flatten_session(session)
    assert turns == [
        {"role": "user", "content": "hello"},
        {"role": "assistant", "content": "hi"},
    ]


def test_segment_haystack_sessions() -> None:
    record = {
        "haystack_session_ids": ["id1", "id2", "id3"],
        "haystack_dates": ["d1", "d2", "d3"],
        "haystack_sessions": [
            [{"role": "user", "content": "s1"}],
            [{"role": "assistant", "content": "s2"}],
            [{"role": "user", "content": "s3"}],
        ]
    }
    stages = segment_haystack_sessions(record, sessions_per_stage=2)
    assert len(stages) == 2
    assert stages[0][0]["content"] == "[session_meta] id=id1 date=d1"
    assert stages[0][1]["content"] == "s1"
    assert stages[1][0]["content"] == "[session_meta] id=id3 date=d3"


def test_build_staged_history_chunks_obeys_size_limit() -> None:
    record = {
        "haystack_sessions": [
            [{"role": "user", "content": "A" * 200}],
            [{"role": "assistant", "content": "B" * 200}],
            [{"role": "user", "content": "C" * 200}],
        ]
    }
    chunks = build_staged_history_chunks(record, max_chars=260, max_sessions_per_stage=3)
    assert len(chunks) >= 2
    assert all(len(chunk) <= 260 for chunk in chunks)


def test_build_question_prompt_contains_key_fields() -> None:
    record = {
        "question_id": "q1",
        "question_date": "2024-10-01",
        "question": "What did I eat?",
    }
    prompt = build_question_prompt(record)
    assert "q1" in prompt
    assert "2024-10-01" in prompt
    assert "What did I eat?" in prompt
