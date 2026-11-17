import threading
from types import SimpleNamespace

import pytest

from pakt import __version__, updates
from pakt.config import Config, ServerConfig
from pakt.models import PlexIds, RatedItem, SyncResult, WatchedItem
from pakt.sync import collection, episodes, match, movies, report, run, watchlist
from pakt.sync.engine import PushedToTrakt, SyncEngine
from pakt.trakt import TraktAuthError


def plex_movie(imdb: str, watched: bool = False, rating: float | None = None, title: str = "Movie", score: int = 0):
    """A Plex movie as a library listing returns it, with an IMDb GUID."""
    media = [SimpleNamespace(videoResolution="4k" if score else "1080", audioCodec=None, audioChannels=0, parts=[])]
    return SimpleNamespace(
        ratingKey=imdb, guid=f"plex://movie/{imdb}", guids=[SimpleNamespace(id=f"imdb://{imdb}")],
        isWatched=watched, userRating=rating, lastViewedAt=None, lastRatedAt=None, addedAt=None,
        title=title, year=2000, media=media,
    )


def plex_episode(show_key: str, season, number, watched: bool = False, rating: float | None = None):
    """A Plex episode as a library listing returns it."""
    return SimpleNamespace(
        grandparentRatingKey=show_key, parentIndex=season, index=number, viewCount=1 if watched else 0,
        userRating=rating, grandparentTitle="Show", lastViewedAt=None, lastRatedAt=None, addedAt=None,
    )


class FakeTrakt:
    """TraktClient stand-in serving fixed data and recording every write."""

    def __init__(self, watched=(), ratings=()):
        self.watched = [WatchedItem(movie={"ids": {"imdb": i}}) for i in watched]
        self.ratings = [RatedItem(rated_at="2020-01-01T00:00:00Z", rating=r, movie={"ids": {"imdb": i}})
                        for i, r in ratings]
        self.posts: list[tuple[str, dict]] = []

    async def get_watched_movies(self):
        """Get the watched movies given to the fake."""
        return self.watched

    async def get_movie_ratings(self):
        """Get the movie ratings given to the fake."""
        return self.ratings

    async def add_to_history(self, **lists):
        """Record a history write."""
        self.posts.append(("history", lists))
        return {"added": {"movies": len(lists.get("movies") or [])}}

    async def add_ratings(self, **lists):
        """Record a ratings write."""
        self.posts.append(("ratings", lists))
        return {"added": {"movies": len(lists.get("movies") or [])}}


class FakePlex:
    """PlexClient stand-in serving fixed movies and recording every write."""

    def __init__(self, movies):
        self.movies = movies
        self.watched: list = []
        self.rated: list = []

    def get_all_movies_with_counts(self, libraries):
        """Get the movies given to the fake as one library."""
        return self.movies, {"Movies": len(self.movies)}

    def mark_watched_batch(self, items):
        """Record items marked watched; none fail."""
        self.watched.extend(items)
        return []

    def rate_batch(self, pairs):
        """Record ratings set; none fail."""
        self.rated.extend(pairs)
        return []


async def run_movies(trakt, plex, dry_run=False, pushed=None, priority="none") -> SyncResult:
    """Run phase 1 against the fakes and return its result."""
    config = Config()
    config.sync.rating_priority = priority
    engine = SyncEngine(config, trakt, plex, pushed=pushed)
    result = SyncResult()
    assert await movies.run(engine, result, dry_run)
    return result


def test_id_keys_keep_kinds_apart():
    """A TVDB and a TMDB id with the same number stay distinct keys."""
    ids = PlexIds(plex="1", tvdb=81189, tmdb=81189)
    assert match.id_keys(ids, match.SHOW_ID_TYPES) == [("tvdb", 81189), ("tmdb", 81189)]
    assert match.id_keys({"imdb": None, "tmdb": 5}, match.MOVIE_ID_TYPES) == [("tmdb", 5)]


def test_episodes_are_grouped_by_show_and_season():
    """Episode rows nest under one entry per show, seasons in order."""
    rows = [({"ids": {"tvdb": 1}}, 2, {"number": 1}, ""), ({"ids": {"tvdb": 1}}, 1, {"number": 2}, ""),
            ({"ids": {"tvdb": 1}}, 1, {"number": 1}, ""), ({"ids": {"tvdb": 9}}, 1, {"number": 1}, "")]
    shows = match.group_by_show(rows)
    assert len(shows) == 2
    assert [s["number"] for s in shows[0]["seasons"]] == [1, 2]
    assert [e["number"] for e in shows[0]["seasons"][0]["episodes"]] == [2, 1]


@pytest.mark.parametrize("plex, trakt, priority, expected", [
    (8, None, "none", (True, False)),
    (None, 7, "none", (False, True)),
    (8, 7, "none", (False, False)),
    (8, 7, "plex", (True, False)),
    (8, 7, "trakt", (False, True)),
    (8, 8, "plex", (False, False)),
])
def test_rating_direction(plex, trakt, priority, expected):
    """A rating flows to the side missing it; a conflict follows the priority setting."""
    assert match.rating_direction(plex, trakt, priority, True, True) == expected


@pytest.mark.parametrize("trakt, plex, expected", [
    ({"watched": 2, "rated": 1}, {"watched": 3, "rated": 0}, "Trakt +2 watched, +1 rated; Plex +3 watched"),
    ({"watched": 0}, {"rated": 4}, "Plex +4 rated"),
    ({"watched": 0}, {"watched": 0}, "no changes"),
])
def test_changes_name_only_what_moved(trakt, plex, expected):
    """A change summary leaves out zero counts and says when nothing changed."""
    assert report.changes(trakt, plex) == expected


@pytest.mark.parametrize("seconds, expected", [(5.43, "5.4s"), (109, "1m49s"), (3600, "60m00s")])
def test_durations_read_as_seconds_then_minutes(seconds, expected):
    """Durations under a minute show tenths; longer ones show minutes and seconds."""
    assert report.duration(seconds) == expected


def test_trakt_counts_read_as_plain_text():
    """A Trakt response's counts read with singular and plural nouns, and 'nothing' when empty."""
    assert report.counted({"movies": 1, "episodes": 3, "shows": 0}) == "1 movie, 3 episodes"
    assert report.counted({}) == "nothing"


def test_watched_index_skips_unwatched_progress_entries():
    """Episodes Trakt lists as not completed are not counted as watched."""
    show = WatchedItem(show={"ids": {"tvdb": 1, "imdb": "tt1"}}, seasons=[{"number": 1, "episodes": [
        {"number": 1, "completed": True}, {"number": 2, "completed": False}]}])
    index = episodes.episode_index([show])
    assert (("tvdb", 1), 1, 1) in index and (("imdb", "tt1"), 1, 1) in index
    assert (("tvdb", 1), 1, 2) not in index


def run_episodes(plex_episodes, watched=frozenset(), ratings=None, pushed=None, cancel=False, shows=None):
    """Run the episode worker, by default over one show with TVDB id 1."""
    event = threading.Event()
    if cancel:
        event.set()
    options = {k: True for k in ("watched_plex_to_trakt", "watched_trakt_to_plex",
                                 "ratings_plex_to_trakt", "ratings_trakt_to_plex")}
    return episodes.process_in_thread(
        plex_episodes, shows or {"s1": PlexIds(plex="s1", tvdb=1)}, set(watched), ratings or {}, options, "none",
        event, pushed or PushedToTrakt(),
    )


def test_episode_worker_sends_each_play_once():
    """A watched episode in two libraries is queued for Trakt once."""
    result = run_episodes([plex_episode("s1", 1, 1, watched=True), plex_episode("s1", 1, 1, watched=True)])
    assert len(result.watched_to_trakt) == 1


def test_episode_worker_marks_plex_from_trakt():
    """Every Plex copy of an episode watched on Trakt gets marked."""
    result = run_episodes([plex_episode("s1", 1, 1), plex_episode("s1", 1, 1)], watched={(("tvdb", 1), 1, 1)})
    assert len(result.watched_to_plex) == 2


def test_episode_worker_skips_items_another_server_sent():
    """An episode already sent this run is not queued again."""
    pushed = PushedToTrakt(watched_episodes={(("tvdb", 1), 1, 1)})
    assert not run_episodes([plex_episode("s1", 1, 1, watched=True)], pushed=pushed).watched_to_trakt


def test_episode_worker_matches_copies_on_any_shared_id():
    """Copies of a show carrying different id sets queue a play once, here and on a later server."""
    shows = {"s1": PlexIds(plex="s1", tvdb=1, tmdb=5), "s2": PlexIds(plex="s2", tmdb=5)}
    copies = [plex_episode("s1", 1, 1, watched=True), plex_episode("s2", 1, 1, watched=True)]
    assert len(run_episodes(copies, shows=shows).watched_to_trakt) == 1
    pushed = PushedToTrakt(watched_episodes={(("tmdb", 5), 1, 1)})
    assert not run_episodes(copies[:1], shows=shows, pushed=pushed).watched_to_trakt


def test_episode_worker_counts_episodes_it_cannot_place():
    """Episodes without a show id or a season or episode number are counted by show, not fatal."""
    result = run_episodes([plex_episode("unknown", 1, 1), plex_episode("s1", None, 1), plex_episode("s1", 1, None)])
    assert result.skipped_no_ids == {"Show": 1} and result.skipped_no_number == {"Show": 2}


def test_episode_worker_stops_on_cancel():
    """A cancel before the work starts ends it at once."""
    assert run_episodes([plex_episode("s1", 1, 1, watched=True)], cancel=True).cancelled


async def test_second_copy_of_a_movie_still_reaches_trakt():
    """A watch on the second library's copy is sent even though the first copy is unwatched."""
    trakt = FakeTrakt()
    plex = FakePlex([plex_movie("tt1"), plex_movie("tt1", watched=True)])
    result = await run_movies(trakt, plex)
    assert trakt.posts[0] == ("history", {"movies": [{"title": "Movie", "year": 2000, "ids": {"imdb": "tt1"}}]})
    assert result.added_to_trakt == 1


def movie_copies():
    """One movie twice: a copy with IMDb and TMDB GUIDs, and a legacy agent copy with TMDB only."""
    modern = plex_movie("tt1", watched=True)
    modern.guids.append(SimpleNamespace(id="tmdb://603"))
    legacy = plex_movie("legacy", watched=True)
    legacy.guid, legacy.guids = "com.plexapp.agents.themoviedb://603?lang=en", []
    return modern, legacy


async def test_movie_copies_with_different_ids_post_one_play():
    """Copies sharing any id post one play, in one library or across two servers."""
    modern, legacy = movie_copies()
    trakt = FakeTrakt()
    await run_movies(trakt, FakePlex([modern, legacy]))
    assert [len(lists["movies"]) for _, lists in trakt.posts] == [1]

    trakt, pushed = FakeTrakt(), PushedToTrakt()
    await run_movies(trakt, FakePlex([legacy]), pushed=pushed)
    await run_movies(trakt, FakePlex([modern]), pushed=pushed)
    assert len(trakt.posts) == 1


async def test_failed_history_post_still_counts_as_sent():
    """Items of a history post that failed are still recorded as sent for later servers."""
    trakt, pushed = FakeTrakt(), PushedToTrakt()

    async def gateway_error(**lists):
        """Fail as a post does after its retries."""
        raise RuntimeError("502")

    trakt.add_to_history = gateway_error
    with pytest.raises(RuntimeError):
        await run_movies(trakt, FakePlex([plex_movie("tt1", watched=True)]), pushed=pushed)
    assert pushed.watched_movies == {("imdb", "tt1")}


async def test_movie_watched_on_trakt_is_marked_on_every_plex_copy():
    """Each unwatched Plex copy of a movie watched on Trakt is marked."""
    plex = FakePlex([plex_movie("tt1"), plex_movie("tt1")])
    result = await run_movies(FakeTrakt(watched=["tt1"]), plex)
    assert len(plex.watched) == 2 and result.added_to_plex == 2


async def test_movie_ratings_follow_priority():
    """A rating conflict sends Plex's rating when Plex has priority."""
    trakt = FakeTrakt(ratings=[("tt1", 6)])
    await run_movies(trakt, FakePlex([plex_movie("tt1", rating=9.0)]), priority="plex")
    assert trakt.posts == [("ratings", {"movies": [
        {"title": "Movie", "year": 2000, "ids": {"imdb": "tt1"}, "rating": 9}]})]


async def test_dry_run_writes_nothing_but_counts_once():
    """A dry run sends nothing but records the items, which a second server then leaves out."""
    trakt = FakeTrakt()
    pushed = PushedToTrakt()
    await run_movies(trakt, FakePlex([plex_movie("tt1", watched=True)]), dry_run=True, pushed=pushed)
    assert trakt.posts == [] and pushed.watched_movies == {("imdb", "tt1")}


async def test_failed_phase_does_not_stop_the_next(monkeypatch):
    """An error in one phase is recorded and the later phases still run."""
    engine = SyncEngine(Config(), FakeTrakt(), FakePlex([]))
    ran = []

    async def boom(*args, **kwargs):
        """Fail the movie phase."""
        raise RuntimeError("boom")

    async def later(*args, **kwargs):
        """Note that a later phase ran."""
        ran.append(True)
        return True

    monkeypatch.setattr(movies, "run", boom)
    for phase in (episodes, collection, watchlist):
        monkeypatch.setattr(phase, "run", later)
    result = await engine.sync()
    assert result and result.errors == ["Movies: RuntimeError: boom"] and len(ran) == 3


async def test_auth_errors_stop_the_server(monkeypatch):
    """A refused token is not swallowed by the phase guard."""
    engine = SyncEngine(Config(), FakeTrakt(), FakePlex([]))

    async def refused(*args, **kwargs):
        """Fail as Trakt does on a dead token."""
        raise TraktAuthError()

    monkeypatch.setattr(movies, "run", refused)
    with pytest.raises(TraktAuthError):
        await engine.sync()


async def test_second_sync_is_refused_while_one_runs():
    """A sync started while another process holds the lock fails fast."""
    config = Config()
    config.servers = [ServerConfig(name="s", url="http://192.168.1.5:32400", token="t")]
    with run.sync_lock() as held:
        assert held
        result = await run.run_multi_server_sync(config)
    assert result.errors == ["Another sync is already running"]


def watchlist_plex(items):
    """PlexClient stand-in whose watchlist holds the given items and whose search finds them."""
    return SimpleNamespace(get_watchlist=lambda: items, search_discover=lambda title, libtype: items)


def watch_item(kind: str, guid: str, title: str = "Title"):
    """A Plex watchlist movie or show carrying one external GUID."""
    return SimpleNamespace(TYPE=kind, ratingKey=guid, guid="plex://x", guids=[SimpleNamespace(id=guid)],
                           title=title, year=2001)


def plan(plex, trakt_movies=(), trakt_shows=(), just_watched=frozenset(), to_trakt=True, to_plex=True):
    """Plan the watchlist with both directions on unless told otherwise."""
    options = {"watchlist_plex_to_trakt": to_trakt, "watchlist_trakt_to_plex": to_plex}
    return watchlist.plan(plex, list(trakt_movies), list(trakt_shows), set(just_watched), options, lambda: False)


def test_watchlist_keeps_movie_and_show_ids_apart():
    """A show whose TMDB id matches a watchlisted movie's is still added."""
    plex = watchlist_plex([watch_item("show", "tmdb://42")])
    movies, shows, _, _ = plan(plex, trakt_movies=[{"movie": {"ids": {"tmdb": 42}}}], to_plex=False)
    assert movies == [] and [s["ids"] for s in shows] == [{"tmdb": 42}]


def test_watchlist_skips_movies_just_marked_watched():
    """A movie sent to Trakt as watched this run is not added back to its watchlist."""
    plex = watchlist_plex([watch_item("movie", "imdb://tt1")])
    movies, _, _, _ = plan(plex, just_watched={("imdb", "tt1")}, to_plex=False)
    assert movies == []


def test_watchlist_adds_trakt_items_found_by_search():
    """A Trakt watchlist movie missing from Plex is matched by id from the search results."""
    found = watch_item("movie", "imdb://tt9", title="Found")
    plex = SimpleNamespace(get_watchlist=lambda: [], search_discover=lambda title, libtype: [found])
    _, _, to_plex, _ = plan(plex, trakt_movies=[{"movie": {"title": "Found", "ids": {"imdb": "tt9"}}}], to_trakt=False)
    assert to_plex == [found]


async def test_collection_uses_the_best_copy():
    """With a movie in two libraries, the collection entry carries the better copy's metadata."""
    class CollectionTrakt(FakeTrakt):
        """Fake Trakt with an empty collection, recording collection writes."""

        async def get_account_limits(self):
            """Get a VIP account's limits."""
            return SimpleNamespace(is_vip=True, collection_limit=0)

        async def get_collection_movies(self):
            """Get an empty movie collection."""
            return []

        async def get_collection_shows(self):
            """Get an empty show collection."""
            return []

        async def add_to_collection(self, **lists):
            """Record a collection write."""
            self.posts.append(("collection", lists))
            return {}

    config = Config()
    config.sync.collection_plex_to_trakt = True
    trakt = CollectionTrakt()
    plex = FakePlex([plex_movie("tt1"), plex_movie("tt1", score=1)])
    engine = SyncEngine(config, trakt, plex)
    assert await collection.run(engine, SyncResult(), dry_run=False, no_shows=True)
    sent = trakt.posts[0][1]["movies"]
    assert len(sent) == 1 and sent[0]["resolution"] == "uhd_4k"


class ShowLibrary(FakePlex):
    """PlexClient stand-in serving shows and their episodes."""

    def __init__(self, shows, episodes):
        super().__init__([])
        self.shows, self.episodes = shows, episodes

    def get_all_shows_with_counts(self, libraries):
        """Get the shows given to the fake as one library."""
        return self.shows, {"TV": len(self.shows)}

    def get_all_episodes_with_counts(self, libraries):
        """Get the episodes given to the fake as one library."""
        return self.episodes, {"TV": len(self.episodes)}


async def collect_episodes(collected_numbers, fix_dates=False):
    """Collect S01E01 and S01E02 of one show held twice under different ids; return numbers sent."""
    class EpisodeTrakt(FakeTrakt):
        """VIP TraktClient stand-in whose collection holds the given S01 episodes of TVDB show 1."""

        async def get_account_limits(self):
            """Get a VIP account's limits."""
            return SimpleNamespace(is_vip=True, collection_limit=0)

        async def get_collection_movies(self):
            """Get an empty movie collection."""
            return []

        async def get_collection_shows(self):
            """Get the show with its collected episodes."""
            if not collected_numbers:
                return []
            episodes = [{"number": n} for n in collected_numbers]
            return [{"show": {"ids": {"tvdb": 1}}, "seasons": [{"number": 1, "episodes": episodes}]}]

        async def add_to_collection(self, **lists):
            """Record a collection write."""
            self.posts.append(("collection", lists))
            return {}

    def show(key, guids):
        """A Plex show carrying the given external GUIDs."""
        return SimpleNamespace(ratingKey=key, guid=f"plex://show/{key}", guids=[SimpleNamespace(id=g) for g in guids],
                               title="Show", year=2000)

    shows = [show("s1", ["tvdb://1", "tmdb://5"]), show("s2", ["tmdb://5"])]
    episodes = [plex_episode(key, 1, n) for key in ("s1", "s2") for n in (1, 2)]
    config = Config()
    config.sync.collection_plex_to_trakt = True
    trakt = EpisodeTrakt()
    engine = SyncEngine(config, trakt, ShowLibrary(shows, episodes))
    engine.fix_collection_dates = fix_dates
    assert await collection.run(engine, SyncResult(), dry_run=False, no_movies=True)
    return [e["number"] for _, lists in trakt.posts for s in lists.get("shows") or []
            for season in s["seasons"] for e in season["episodes"]]


async def test_collection_sends_each_missing_episode_once():
    """Episodes of a show held twice under different ids go once."""
    assert await collect_episodes([]) == [1, 2]


async def test_fixing_collection_dates_resends_collected_episodes():
    """Fixing dates sends episodes Trakt already has, still once each."""
    assert await collect_episodes([1, 2], fix_dates=True) == [1, 2]


class TraktSession:
    """TraktClient stand-in for a whole run, opening without a network."""

    def __init__(self, config, on_token_refresh=None):
        self.config = config

    async def __aenter__(self):
        """Open the fake session."""
        return self

    async def __aexit__(self, *args):
        """Close the fake session."""


class ReachablePlex:
    """PlexClient stand-in that connects over the LAN."""

    def __init__(self, server_config, account_token=""):
        self.server = SimpleNamespace(friendlyName=server_config.name)
        self.route = "over LAN"

    def connect(self):
        """Connect at once."""


class UnreachablePlex(ReachablePlex):
    """PlexClient stand-in for a server that is down."""

    def connect(self):
        """Fail to connect, as a dropped connection does."""
        raise ConnectionError("server unreachable")


async def run_logged(monkeypatch, plex, trakt=TraktSession) -> tuple[SyncResult, list[str]]:
    """Run a sync of a server named Home on the fakes, returning the result and web UI lines."""
    monkeypatch.setattr(run, "TraktClient", trakt)
    monkeypatch.setattr(run, "PlexClient", plex)
    config = Config()
    config.servers = [ServerConfig(name="Home", url="http://192.168.1.5:32400", token="t")]
    lines: list[str] = []
    result = await run.run_multi_server_sync(config, log_callback=lines.append, started_by="web UI")
    return result, lines


async def test_run_opens_with_its_origin_and_closes_with_what_changed(monkeypatch):
    """A run names its version, origin and servers, and ends with one line of what changed."""
    async def changed(self, **kwargs):
        """Report two plays sent to Trakt and a rating sent to Plex."""
        return SyncResult(added_to_trakt=2, ratings_to_plex=1)

    monkeypatch.setattr(SyncEngine, "sync", changed)
    _, lines = await run_logged(monkeypatch, ReachablePlex)
    assert lines[:2] == [f"pakt {__version__} live sync started by web UI: Home", "[Home] Connected over LAN"]
    assert lines[-1].startswith("SUCCESS:Sync finished in ")
    assert lines[-1].endswith(", 1 of 1 server: Trakt +2 watched; Plex +1 rated")


async def test_unreachable_server_logs_an_error_and_fails_the_run(monkeypatch):
    """A server that cannot be reached logs an error under its name, and the run ends as failed."""
    result, lines = await run_logged(monkeypatch, UnreachablePlex)
    assert "ERROR:[Home] Sync failed: server unreachable" in lines
    assert lines[-1].startswith("ERROR:Sync finished in ")
    assert lines[-1].endswith(" with 1 error, 0 of 1 server: no changes")
    assert result.errors == ["[Home] server unreachable"]


async def test_failed_run_names_a_newer_release(monkeypatch):
    """A run that ends with errors names a newer release just above its closing line."""
    monkeypatch.setattr(updates, "_fetch_latest", lambda: "99.0.0")
    _, lines = await run_logged(monkeypatch, UnreachablePlex)
    assert lines[-2] == f"WARNING:pakt 99.0.0 is available and may fix these errors: {updates.release_url('99.0.0')}"
    assert lines[-1].startswith("ERROR:Sync finished in ")


async def test_clean_run_does_not_ask_pypi(monkeypatch):
    """A run without errors leaves the update check alone."""
    async def unchanged(self, **kwargs):
        """Report a sync that changed nothing."""
        return SyncResult()

    asked = []
    monkeypatch.setattr(updates, "_fetch_latest", lambda: asked.append(True) or "99.0.0")
    monkeypatch.setattr(SyncEngine, "sync", unchanged)
    _, lines = await run_logged(monkeypatch, ReachablePlex)
    assert asked == [] and lines[-1].startswith("SUCCESS:Sync finished in ")


async def test_dead_trakt_token_ends_the_run_with_an_error(monkeypatch):
    """An expired Trakt token whose refresh Trakt refuses logs an error and fails the run."""
    class Refused(TraktSession):
        """Trakt session whose token refresh is refused."""

        async def __aenter__(self):
            """Fail as an expired token with a dead refresh token does."""
            raise TraktAuthError("Trakt refused the refresh token")

    result, lines = await run_logged(monkeypatch, ReachablePlex, trakt=Refused)
    assert result.errors == ["Trakt refused the refresh token"]
    assert "ERROR:Trakt refused the refresh token" in lines and lines[-1].startswith("ERROR:Sync finished")
