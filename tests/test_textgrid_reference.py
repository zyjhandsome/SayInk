"""TextGrid → reference text / RTTM for the ASR and timeline benches."""
import pytest

from tools import textgrid_reference as tg
from tools.meeting_timeline_bench import read_rttm

SAMPLE = '''File type = "ooTextFile"
Object class = "TextGrid"

xmin = 0
xmax = 10
tiers? <exists>
size = 2
item []:
    item [1]:
        class = "IntervalTier"
        name = "001-M"
        xmin = 0
        xmax = 10
        intervals: size = 3
        intervals [1]:
            xmin = 0
            xmax = 1.5
            text = ""
        intervals [2]:
            xmin = 1.5
            xmax = 4
            text = "呃，这次<sil>咱们开会"
        intervals [3]:
            xmin = 4
            xmax = 10
            text = "<%>"
    item [2]:
        class = "IntervalTier"
        name = "002-F"
        xmin = 0
        xmax = 10
        intervals: size = 2
        intervals [1]:
            xmin = 0
            xmax = 3
            text = "好的"
        intervals [2]:
            xmin = 3
            xmax = 10
            text = "他说 ""可以"" 吧"
'''


@pytest.fixture
def grid(tmp_path):
    path = tmp_path / "m.TextGrid"
    path.write_text(SAMPLE, encoding="utf-8")
    return path


def test_utterances_are_time_ordered_across_tiers_with_tags_removed(grid):
    utterances = tg.read_textgrid(grid)
    assert [(u.start_ms, u.speaker, u.text) for u in utterances] == [
        (0, "002-F", "好的"), (1500, "001-M", "呃，这次咱们开会"), (3000, "002-F", '他说 "可以" 吧'),
    ]
    assert tg.reference_text(utterances) == '好的呃，这次咱们开会他说 "可以" 吧'
    assert tg.reference_text(utterances, until_ms=2000) == "好的呃，这次咱们开会"


def test_rttm_output_is_readable_by_the_timeline_bench(grid, tmp_path):
    utterances = tg.read_textgrid(grid)
    rttm = tmp_path / "m.rttm"
    rttm.write_text("\n".join(tg.rttm_lines(utterances, "m", until_ms=3500)) + "\n")
    # Segments running past the excerpt end are clipped so the RTTM stays inside the audio.
    assert read_rttm(rttm) == [(0, 3000, "002-F"), (1500, 3500, "001-M"), (3000, 3500, "002-F")]


def test_cli_writes_text_beside_the_textgrid_and_optional_rttm(grid, tmp_path):
    assert tg.main([str(grid), "--until", "2", "--rttm", str(tmp_path / "g.rttm"), "--uri", "rec"]) == 0
    assert (tmp_path / "m.txt").read_text(encoding="utf-8") == "好的呃，这次咱们开会"
    assert (tmp_path / "g.rttm").read_text(encoding="utf-8").startswith("SPEAKER rec 1 0.000 2.000 ")


def test_textgrid_without_words_is_rejected(tmp_path):
    path = tmp_path / "empty.TextGrid"
    path.write_text('item [1]:\n    name = "A"\n    intervals [1]:\n        xmin = 0\n        xmax = 1\n        text = "<sil>"\n')
    with pytest.raises(ValueError):
        tg.read_textgrid(path)
    assert tg.main([str(path)]) == 2
