from __future__ import annotations

from pathlib import Path

import extensions.fun.burger as burger_module


def test_burger_write_targets_never_use_the_seed_tree(monkeypatch, tmp_path) -> None:
    seed = tmp_path / "seed"
    runtime = tmp_path / "runtime"
    seed_catalog = seed / "catalog.json"
    runtime_catalog = tmp_path / "runtime-catalog.json"

    monkeypatch.setattr(burger_module, "BURGER_SEED_ROOT", seed)
    monkeypatch.setattr(burger_module, "BURGER_SEED_CATALOG", seed_catalog)
    monkeypatch.setattr(burger_module, "BURGER_ROOT", seed)
    monkeypatch.setattr(burger_module, "BURGER_CATALOG", seed_catalog)
    monkeypatch.setattr(burger_module, "BURGER_RUNTIME_ROOT", runtime)
    monkeypatch.setattr(burger_module, "BURGER_RUNTIME_CATALOG", runtime_catalog)

    assert burger_module._write_targets() == ((runtime, runtime_catalog),)


def test_select_write_target_continues_after_read_only_probe_cleanup(
    monkeypatch, tmp_path
) -> None:
    seed = tmp_path / "seed"
    runtime = tmp_path / "runtime"
    seed_catalog = seed / "catalog.json"
    runtime_catalog = tmp_path / "runtime-catalog.json"
    seed.mkdir()
    runtime.mkdir()

    # Simulate an older process that still reports the bundled seed first.
    monkeypatch.setattr(
        burger_module,
        "_write_targets",
        lambda: ((seed, seed_catalog), (runtime, runtime_catalog)),
    )
    real_write_bytes = Path.write_bytes
    real_unlink = Path.unlink

    def write_bytes(path: Path, data: bytes) -> int:
        if path.parent.resolve() == seed.resolve():
            raise OSError("read-only filesystem")
        return real_write_bytes(path, data)

    def unlink(path: Path, *, missing_ok: bool = False) -> None:
        if path.parent.resolve() == seed.resolve():
            raise OSError("read-only filesystem")
        real_unlink(path, missing_ok=missing_ok)

    monkeypatch.setattr(Path, "write_bytes", write_bytes)
    monkeypatch.setattr(Path, "unlink", unlink)

    selected_root, selected_catalog = burger_module._select_write_target()

    assert selected_root == runtime
    assert selected_catalog == runtime_catalog
