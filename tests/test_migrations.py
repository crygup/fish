from core.migrations import available_migrations


def test_migrations_are_unique_and_have_content_checksums() -> None:
    migrations = available_migrations()
    assert [item.version for item in migrations] == [1, 2, 3, 4, 5]
    assert len({item.checksum for item in migrations}) == len(migrations)
    assert all(len(item.checksum) == 64 for item in migrations)


def test_schema_no_longer_runs_from_bot_startup() -> None:
    source = (available_migrations()[0].path.parent / "core" / "bot.py").read_text()
    assert "check_migrations" in source
    assert "pool.execute(fp.read())" not in source
