"""The `python -m vinyl_archive` entry point binds what the config says."""

import uvicorn

from vinyl_archive import __main__


def test_main_binds_host_and_port_from_config(tmp_path, monkeypatch):
    cfg = tmp_path / "config.toml"
    cfg.write_text(f'[server]\nhost = "127.0.0.1"\nport = 9123\n\n'
                   f'[paths]\ndata_dir = "{tmp_path}"\n\n'
                   f'[capture]\nauto_start = false\n')
    monkeypatch.setenv("VINYL_ARCHIVE_CONFIG", str(cfg))

    captured = {}
    monkeypatch.setattr(uvicorn, "run",
                        lambda app, **kw: captured.update(kw, app=app))
    __main__.main()

    assert (captured["host"], captured["port"]) == ("127.0.0.1", 9123)
    # The app must carry the same config the binding came from.
    assert captured["app"].state.config.server.port == 9123
    assert captured["app"].state.config.data_dir == tmp_path
