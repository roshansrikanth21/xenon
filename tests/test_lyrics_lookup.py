"""lyrics_lookup — LRC parsing, track-name parsing, ranking and agreement."""
from __future__ import annotations

from xenon.lyrics_lookup import (
    clean_artist,
    clean_title,
    find,
    most_agreed,
    parse_lrc,
    parse_track,
    rank,
    similarity,
    to_result,
)


class TestParseLrc:
    def test_basic_stamp(self):
        lines = parse_lrc("[00:12.34]Hello there")
        assert lines == [(12340, "Hello there")]

    def test_multiple_stamps_one_line(self):
        lines = parse_lrc("[00:01.00][01:02.00]Chorus")
        assert lines == [(1000, "Chorus"), (62000, "Chorus")]

    def test_offset_header(self):
        lines = parse_lrc("[offset:-500]\n[00:01.00]Late")
        assert lines == [(1500, "Late")]

    def test_word_stamps_dropped(self):
        lines = parse_lrc("[00:05.00]<00:05.00>Word <00:06.00>by word")
        assert lines == [(5000, "Word by word")]

    def test_unstamped_lines_ignored(self):
        lines = parse_lrc("[ti:Title]\n[by:Someone]\nJust plain text")
        assert lines == []

    def test_colon_decimal_and_sorting(self):
        lines = parse_lrc("[00:03:50]B\n[00:01.20]A")
        assert lines == [(1200, "A"), (3500, "B")]


class TestParseTrack:
    def test_video_title_browser(self):
        # No player app: the dash splits off the artist.
        track = parse_track({"title": "Artist - Song (Official Video)", "artist": "ArtistVEVO"})
        assert track["title"] == "Song"
        assert track["artist"] == "Artist"

    def test_player_title_keeps_artist_field(self):
        # Spotify reports title and artist separately; the dash in a title is
        # often part of the song name.
        track = parse_track({"title": "Weird Fishes - Live", "artist": "Radiohead", "app": "spotify"})
        assert track["title"] == "Weird Fishes - Live"
        assert track["artist"] == "Radiohead"

    def test_brackets_and_remasters(self):
        assert clean_title("Song (2011 Remaster)") == "Song"
        assert clean_title("Song [Lyric Video]") == "Song"
        assert clean_title("Song (feat. Someone)") == "Song"
        assert clean_title("Song - 2011 Remastered Version") == "Song"

    def test_channel_names_reduce_to_artist(self):
        assert clean_artist("Artist - Topic") == "Artist"
        assert clean_artist("ArtistVEVO") == "Artist"
        assert clean_artist("Artist, Other") == "Artist"


class TestScoring:
    def test_similarity_identical(self):
        assert similarity("weird fishes", "Weird Fishes") == 1.0

    def test_similarity_disjoint(self):
        assert similarity("aphex twin", "boards of canada") == 0.0

    def test_rank_rejects_far_durations(self):
        wanted = {"title": "song", "artist": "artist", "album": ""}
        candidate = {
            "trackName": "song", "artistName": "artist", "albumName": "x",
            "duration": 300, "syncedLyrics": "[00:01]a", "plainLyrics": None,
            "instrumental": False,
        }
        assert rank(wanted, 200000, candidate) == 0.0  # 100 s apart

    def test_rank_prefers_near_exact_synced(self):
        wanted = {"title": "song", "artist": "artist", "album": ""}
        candidate = {
            "trackName": "song", "artistName": "artist", "albumName": "x",
            "duration": 240, "syncedLyrics": "[00:01]a", "plainLyrics": None,
            "instrumental": False,
        }
        value = rank(wanted, 240000, candidate)
        assert value > 0.8

    def test_score_requires_title_and_artist_agreement(self):
        wanted = {"title": "song", "artist": "artist", "album": ""}
        assert score_of(wanted, "completely", "different") == 0.0


def score_of(wanted, title, artist):
    from xenon.lyrics_lookup import score

    return score(wanted, {"title": title, "artist": artist, "album": ""})


class TestToResult:
    def test_synced(self):
        result = to_result({"syncedLyrics": "[00:01]Hello", "plainLyrics": None, "instrumental": False})
        assert result["synced"] is True
        assert result["lines"] == [(1000, "Hello")]

    def test_untimed_plain(self):
        result = to_result({"syncedLyrics": None, "plainLyrics": "Just words", "instrumental": False})
        assert result["untimed"] is True
        assert result["synced"] is False

    def test_instrumental(self):
        result = to_result({"syncedLyrics": None, "plainLyrics": None, "instrumental": True})
        assert result["instrumental"] is True

    def test_nothing(self):
        assert to_result({"syncedLyrics": None, "plainLyrics": None, "instrumental": False}) is None


class TestMostAgreed:
    def _candidate(self, offset_ms: int):
        return {
            "syncedLyrics": "\n".join(
                f"[00:{10 + i}.{offset_ms // 10:02d}]line {i}" for i in range(6)
            )
        }

    def test_few_candidates_take_top_score(self):
        scored = [(self._candidate(0), 1.0), (self._candidate(1000), 0.99)]
        assert most_agreed(scored) == scored[0][0]

    def test_majority_timing_beats_one_oddball(self):
        # Three uploads timed alike, one running 2 s late on every line, all
        # scored identically: the agreement vote picks the majority timing.
        alike = [(self._candidate(0), 1.0)] * 3
        late = (self._candidate(2000), 1.0)
        winner = most_agreed(alike + [late])
        first_time = parse_lrc(winner["syncedLyrics"])[0][0]
        assert first_time == parse_lrc(self._candidate(0)["syncedLyrics"])[0][0]


class TestFindWithInjectedFetch:
    def test_prefers_synced_match(self):
        good = {
            "trackName": "Song", "artistName": "Artist", "albumName": "Album",
            "duration": 200, "syncedLyrics": "[00:01.00]Hello",
            "plainLyrics": None, "instrumental": False,
        }
        remix = {
            "trackName": "Song", "artistName": "Artist", "albumName": "Live",
            "duration": 500, "syncedLyrics": "[00:01.00]Hello", "plainLyrics": None,
            "instrumental": False,
        }

        def fetch(url: str):
            assert "lrclib.net/api/search" in url
            return [remix, good]

        wanted = {"title": "Song", "artist": "Artist", "album": "Album"}
        result = find({**wanted, "app": "music.ui"}, 200000, fetch)
        assert result == {"lines": [(1000, "Hello")], "synced": True, "instrumental": False}

    def test_no_candidates_returns_none(self):
        def fetch(url: str):
            return []

        result = find({"title": "Unknown", "artist": "", "album": ""}, 0, fetch)
        assert result is None
