from types import SimpleNamespace
from unittest import mock

import pytest
from plexapi.exceptions import NotFound, Unauthorized

from pakt.config import ServerConfig
from pakt.plex import account, client, media


@pytest.fixture(autouse=True)
def no_identity():
    """Skip the client identity setup, which reads and writes config.json."""
    with mock.patch.object(account, "identify"):
        yield


def item(guid: str, guids=()):
    """A Plex item with a main GUID and optional external GUIDs."""
    return SimpleNamespace(ratingKey=1, guid=guid, guids=[SimpleNamespace(id=g) for g in guids])


@pytest.mark.parametrize("guid, field, value", [
    ("com.plexapp.agents.imdb://tt0111161?lang=en", "imdb", "tt0111161"),
    ("com.plexapp.agents.themoviedb://278?lang=en", "tmdb", 278),
    ("com.plexapp.agents.thetvdb://81189/1/1?lang=en", "tvdb", 81189),
])
def test_legacy_agent_guids_give_ids(guid, field, value):
    """Items from the old Plex agents carry their id in the main GUID."""
    assert getattr(media.extract_plex_ids(item(guid)), field) == value


def test_external_guids_win_over_the_legacy_fallback():
    """New-agent items read their ids from the GUID list."""
    ids = media.extract_plex_ids(item("plex://movie/abc", ["imdb://tt1", "tmdb://2", "tvdb://3"]))
    assert (ids.imdb, ids.tmdb, ids.tvdb) == ("tt1", 2, 3)


def test_unsupported_agent_gives_no_ids():
    """An agent Trakt cannot match, such as HAMA, yields no ids."""
    ids = media.extract_plex_ids(item("com.plexapp.agents.hama://anidb-1"))
    assert (ids.imdb, ids.tmdb, ids.tvdb) == (None, None, None)


@pytest.mark.parametrize("url, lan", [
    ("http://192.168.1.5:32400", True),
    ("https://192-168-1-5.abc.plex.direct:32400", True),
    ("http://10.0.0.2:32400", True),
    ("http://localhost:32400", True),
    ("https://8-8-4-4.abc.plex.direct:32400", False),
    ("http://8.8.8.8:32400", False),
    ("https://plex.example.com", False),
])
def test_lan_urls(url, lan):
    """Private, loopback and private plex.direct addresses count as LAN."""
    assert account.is_lan_url(url) is lan


def test_best_connection_prefers_lan_direct_https():
    """The stored address is the LAN one, not relayed, over HTTPS when offered."""
    server = account.DiscoveredServer("s", "id", "server", False, [
        {"uri": "http://203.0.113.9:32400", "local": False, "relay": False},
        {"uri": "http://192.168.1.5:32400", "local": True, "relay": False},
        {"uri": "https://192-168-1-5.x.plex.direct:32400", "local": True, "relay": False},
        {"uri": "https://relay.plex.direct", "local": False, "relay": True},
    ])
    assert server.best_connection_url == "https://192-168-1-5.x.plex.direct:32400"


def shared_server(url="https://8-8-4-4.x.plex.direct:32400", token="ACCOUNT"):
    """A shared server whose saved token is still the account token."""
    return ServerConfig(name="Shared", server_name="Shared", url=url, token=token)


def test_account_token_stays_off_a_remote_shared_server():
    """With plex.tv down, a shared server's remote address never gets the account token."""
    with mock.patch.object(client, "MyPlexAccount", side_effect=RuntimeError("plex.tv down")), \
            mock.patch.object(client, "PlexServer") as server:
        with pytest.raises(ConnectionError):
            client.PlexClient(shared_server(), account_token="ACCOUNT").connect()
    server.assert_not_called()


def test_account_token_may_reach_a_lan_address():
    """With plex.tv down, the saved LAN address is still tried."""
    with mock.patch.object(client, "MyPlexAccount", side_effect=RuntimeError("plex.tv down")), \
            mock.patch.object(client, "PlexServer") as server, \
            mock.patch.object(client.PlexClient, "_persist_connection"):
        client.PlexClient(shared_server(url="http://192.168.1.5:32400"), account_token="ACCOUNT").connect()
    server.assert_called_once_with("http://192.168.1.5:32400", "ACCOUNT")


def test_direct_fallback_uses_the_servers_own_token():
    """When discovery finds the server but cannot connect, the direct try uses its own token."""
    resource = mock.Mock(owned=False, accessToken="SERVER")
    resource.connect.side_effect = NotFound("unreachable")
    plex_account = mock.Mock(**{"resource.return_value": resource})
    with mock.patch.object(client, "MyPlexAccount", return_value=plex_account), \
            mock.patch.object(client, "PlexServer") as server, \
            mock.patch.object(client.PlexClient, "_persist_connection"):
        client.PlexClient(shared_server(), account_token="ACCOUNT").connect()
    assert server.call_args.args[1] == "SERVER"
    tries = [call.kwargs for call in resource.connect.call_args_list]
    assert tries == [{"ssl": True}, {"ssl": False, "locations": ["local"]}]


def test_refused_token_raises_plex_auth_error():
    """A signed-out Plex token raises PlexAuthError instead of a raw HTTP error."""
    with mock.patch.object(client, "MyPlexAccount", side_effect=Unauthorized("401")):
        with pytest.raises(account.PlexAuthError):
            client.PlexClient(shared_server(), account_token="ACCOUNT").connect()


def test_watchlist_asks_for_ids_in_one_request():
    """The watchlist request includes GUIDs and turns off per-item reloads."""
    entry = SimpleNamespace(_autoReload=True)
    plex_account = mock.Mock(**{"watchlist.return_value": [entry]})
    plex = client.PlexClient(ServerConfig(name="s", token="t"))
    plex._account = plex_account
    assert plex.get_watchlist() == [entry]
    plex_account.watchlist.assert_called_once_with(includeGuids=1)
    assert entry._autoReload is False


def test_sections_are_counted_with_clashing_names_numbered():
    """Two libraries with one name get separate counts; other types are left out."""
    def section(title, kind, size):
        """A library section listing the given number of items."""
        listing = [SimpleNamespace() for _ in range(size)]
        return SimpleNamespace(title=title, type=kind, all=lambda container_size: listing)

    sections = [section("Movies", "movie", 2), section("Movies", "movie", 3), section("TV", "show", 4)]
    plex = client.PlexClient(ServerConfig(name="s", token="t"))
    plex._server = mock.Mock(**{"library.sections.return_value": sections})
    items, counts = plex.get_all_movies_with_counts()
    assert len(items) == 5 and counts == {"Movies": 2, "Movies (2)": 3}


def test_watchlist_batch_reports_failures():
    """Items Plex refuses come back with their error; the rest are added."""
    plex_account = mock.Mock()
    plex_account.addToWatchlist.side_effect = [None, ValueError("already listed")]
    plex = client.PlexClient(ServerConfig(name="s", token="t"))
    plex._account = plex_account
    first, second = SimpleNamespace(title="A"), SimpleNamespace(title="B")
    failed = plex.add_to_watchlist_batch([first, second])
    assert [entry for entry, _ in failed] == [second]
