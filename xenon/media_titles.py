"""Tidy what a browser reports as "now playing".

A streaming site in a browser hands Windows its tab title, so the card
read "Watch I'm Game - JioHotstar", the lyrics lookup searched LRCLIB for
that, and the recent-songs list filled with shows. Here the site's name
and its "Watch ..." wording come off the title, and a video site (or an
episode / trailer title) is marked as a video, which the widget then
keeps out of the lyrics lookup, cover search and song history.

Music sites (YouTube, YouTube Music, Spotify, SoundCloud...) only get the
suffix trimmed: a music video there is still a song.
"""
from __future__ import annotations

import re
from dataclasses import replace

VIDEO_SITES = (
    "jiohotstar", "hotstar", "disney+ hotstar", "disney+", "netflix", "prime video", "amazon prime video",
    "sonyliv", "sony liv", "zee5", "hulu", "hbo max", "crunchyroll", "twitch", "apple tv",
    "apple tv+", "mubi", "sun nxt", "manoramamax", "manorama max", "jiocinema", "mx player",
    "voot", "erosnow", "peacock", "paramount+",
)
# Left out on purpose: names that are also artists ("Max", "Aha", "Kick").
MUSIC_SITES = ("youtube", "youtube music", "spotify", "soundcloud", "apple music", "jiosaavn", "gaana",
               "wynk music", "amazon music", "deezer", "tidal", "bandcamp", "audiomack")

_SITES = sorted(VIDEO_SITES + MUSIC_SITES, key=len, reverse=True)
_SUFFIX = re.compile(
    r"\s*(?:[-–—|•:]\s*)(" + "|".join(re.escape(s) for s in _SITES) + r")\s*$",
    re.IGNORECASE)
_WATCH = re.compile(r"^\s*(?:watch|stream|now streaming)\s+", re.IGNORECASE)
_TAIL_NOISE = re.compile(r"\s*[-|:]\s*(?:watch (?:full )?(?:movie|episode|online|now)(?: online)?.*|"
                         r"full (?:movie|episode)s?(?: online)?|online|free)\s*$", re.IGNORECASE)
_EPISODE = re.compile(r"\b(?:s\d{1,2}\s*e\d{1,3}|season \d+|episode \d+|ep\.? ?\d+|trailer|teaser)\b",
                      re.IGNORECASE)
_BROWSERS = re.compile(r"chrome|msedge|edge|firefox|opera|brave|vivaldi|arc", re.IGNORECASE)


def tidy_title(title: str) -> tuple[str, str | None]:
    """(clean title, site or None): "Watch I'm Game - JioHotstar" ->
    ("I'm Game", "jiohotstar")."""
    text = (title or "").strip()
    site = None
    for _ in range(2):                         # "X - Watch online - Site"
        match = _SUFFIX.search(text)
        if match:
            site = site or match.group(1).lower()
            text = text[:match.start()].rstrip()
        text = _TAIL_NOISE.sub("", text).rstrip()
    if site in VIDEO_SITES:                    # never "Watch Me" the song
        text = _WATCH.sub("", text)
    return (text or (title or "").strip()), site


def is_video(title: str, artist: str = "", app: str = "") -> bool:
    """A show, film or clip rather than a song: from a video site, or an
    episode / trailer title playing in a browser."""
    _clean, site = tidy_title(title)
    if site in VIDEO_SITES:
        return True
    if site in MUSIC_SITES:
        return False
    browser = bool(_BROWSERS.search(app or ""))
    return browser and not artist and bool(_EPISODE.search(title or ""))


def tidy(track):
    """The track with a clean title, and ``track.video`` set. Returns the
    same object for an unchanged title so identity checks keep working."""
    if track is None:
        return None
    clean, _site = tidy_title(track.title)
    video = is_video(track.title, track.artist, track.app)
    out = track if clean == track.title else replace(track, title=clean)
    out.video = video
    return out
