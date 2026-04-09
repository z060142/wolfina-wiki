from longmemeval_demo import derive_profile, extract_turns, segment_turns


def test_extract_turns_from_messages() -> None:
    record = {
        "messages": [
            {"role": "user", "content": "hello"},
            {"role": "assistant", "content": "hi"},
        ]
    }
    turns = extract_turns(record)
    assert turns == [
        {"role": "user", "content": "hello"},
        {"role": "assistant", "content": "hi"},
    ]


def test_extract_turns_fallback_context_question_answer() -> None:
    record = {
        "context": "Alice likes tea.",
        "question": "What does Alice like?",
        "answer": "tea",
    }
    turns = extract_turns(record)
    assert len(turns) == 3
    assert turns[-1]["role"] == "assistant"


def test_segment_turns() -> None:
    turns = [{"role": "user", "content": str(i)} for i in range(10)]
    windows = segment_turns(turns, 4)
    assert [len(w) for w in windows] == [4, 4, 2]


def test_profile_long_context() -> None:
    records = [
        {
            "messages": [
                {"role": "user", "content": "x" * 400},
                {"role": "assistant", "content": "y" * 400},
            ]
        }
    ]
    profile = derive_profile(records)
    assert profile.turns_per_window == 3
