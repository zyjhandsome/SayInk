"""Speaker split / merge rates for a hand-labeled meeting."""

from tools.speaker_eval import parse_label_text, score_speakers


def test_perfect_labels_have_no_split_or_merge():
    rows = [("甲", "1"), ("甲", "1"), ("乙", "2"), ("乙", "2")]
    scored = score_speakers(rows)
    assert scored["split_rate"] == 0.0
    assert scored["merge_rate"] == 0.0
    assert scored["gold_speakers"] == 2
    assert scored["pred_ids"] == 2


def test_one_person_mapped_to_two_ids_is_a_split():
    rows = [("甲", "1"), ("甲", "2"), ("乙", "3")]
    scored = score_speakers(rows)
    assert scored["split_speakers"] == 1
    assert scored["split_rate"] == 0.5
    assert scored["merge_rate"] == 0.0
    assert scored["split_names"] == ["甲"]


def test_two_people_under_one_id_is_a_merge():
    rows = [("甲", "1"), ("乙", "1"), ("丙", "2")]
    scored = score_speakers(rows)
    assert scored["merge_ids"] == 1
    assert scored["merge_rate"] == 0.5
    assert scored["split_rate"] == 0.0
    assert scored["merged_pred_ids"] == ["1"]


def test_label_file_skips_header_and_comments():
    text = "# 脱敏后的一场\n序号\t真人\t预测\n1\t甲\t1\n2\t甲\t1\n"
    assert parse_label_text(text) == [("甲", "1"), ("甲", "1")]


def test_empty_labels_have_no_rate():
    scored = score_speakers([])
    assert scored["segments"] == 0
    assert scored["split_rate"] is None
    assert scored["merge_rate"] is None
