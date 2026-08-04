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


def test_global_tracking_and_history_settings_are_separate() -> None:
    cache = db_cache()
    cache.add_opt_out(42, "avatar")
    assert cache.user_tracking_opted_out(42, "avatar")
    assert not cache.user_tracking_opted_out(42, "status")
    assert cache.user_history_is_public(42)

    cache.tracking_disabled_users.add(42)
    cache.private_history_users.add(42)
    assert cache.user_tracking_opted_out(42, "status")
    assert not cache.user_history_is_public(42)
