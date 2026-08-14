from core.cache import db_cache


def test_cache_state_is_not_shared_between_instances() -> None:
    first = db_cache()
    second = db_cache()
    first.add_prefix(1, "!")
    first.add_adl(2)
    assert second.prefixes == {}
    assert second.auto_downloads == set()


def test_prefixes_and_opt_outs_are_idempotent() -> None:
    cache = db_cache()
    cache.add_prefix(1, "!")
    cache.add_prefix(1, "!")
    cache.add_opt_out(2, "commands")
    cache.add_opt_out(2, "commands")
    assert cache.prefixes[1] == ["!"]
    assert cache.opted_out[2] == ["commands"]


def test_removals_tolerate_stale_cache() -> None:
    cache = db_cache()
    assert cache.remove_prefix(1, "missing") == []
    assert cache.remove_pinboard(1, 2) is None
    cache.remove_adl(3)
    cache.remove_poketwo(3)
    cache.remove_reaction_guilds(3)


def test_account_cache_updates_lastfm_and_anilist_together() -> None:
    cache = db_cache()
    cache.update_accounts(42, last_fm="lastfm-user", anilist="anilist-user")
    assert cache.lastfm[42] == "lastfm-user"
    assert cache.anilist[42] == "anilist-user"

    cache.update_accounts(42, last_fm=None, anilist=None)
    assert 42 not in cache.lastfm
    assert 42 not in cache.anilist


def test_global_tracking_and_history_settings_are_separate() -> None:
    cache = db_cache()
    cache.add_opt_out(42, "avatar")
    assert cache.user_tracking_opted_out(42, "avatar")
    assert not cache.user_tracking_opted_out(42, "status")
    assert not cache.user_history_is_public(42)

    cache.tracking_disabled_users.add(42)
    cache.set_history_public(42, False)
    assert cache.user_tracking_opted_out(42, "status")
    assert not cache.user_history_is_public(42)
    cache.set_history_public(42, True)
    assert cache.user_history_is_public(42)

    assert not cache.user_game_history_is_public(42)
    cache.set_game_history_public(42, True)
    assert cache.user_game_history_is_public(42)
    assert not cache.guild_history_is_public(7)
    cache.set_guild_history_public(7, True)
    assert cache.guild_history_is_public(7)


def test_reaction_tracking_is_explicit_and_respects_global_disable() -> None:
    cache = db_cache()
    assert not cache.reaction_tracking_enabled(42)
    cache.enable_reaction_tracking(42)
    assert cache.reaction_tracking_enabled(42)
    cache.tracking_disabled_users.add(42)
    assert not cache.reaction_tracking_enabled(42)
    cache.tracking_disabled_users.remove(42)
    cache.disable_reaction_tracking(42)
    assert not cache.reaction_tracking_enabled(42)
