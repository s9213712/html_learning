from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]


def test_uiverse_galaxy_layer_is_local_attributed_and_cache_busted():
    index_html = (ROOT / "public" / "index.html").read_text(encoding="utf-8")
    galaxy_css = (ROOT / "public" / "uiverse-galaxy.css").read_text(encoding="utf-8")
    server_py = (ROOT / "server.py").read_text(encoding="utf-8")

    assert '/uiverse-galaxy.css?v=__ASSET_VERSION__' in index_html
    assert index_html.index('/uiverse-galaxy.css?v=__ASSET_VERSION__') > index_html.index('/styles.css?v=__ASSET_VERSION__')
    assert "uiverse-io/galaxy" in galaxy_css
    assert "MIT" in galaxy_css
    assert "Buttons/0x-Sarthak_hungry-penguin-30.html" in galaxy_css
    assert "Cards/05akalan57_thin-sloth-31.html" in galaxy_css
    assert "Forms/3bdel3ziz-T_helpless-wasp-32.html" in galaxy_css
    assert "@import" not in galaxy_css
    assert "url(" not in galaxy_css
    assert '"/uiverse-galaxy.css"' in server_py


def test_uiverse_galaxy_layer_preserves_accessibility_and_mobile_motion_policy():
    galaxy_css = (ROOT / "public" / "uiverse-galaxy.css").read_text(encoding="utf-8")

    assert ".btn-primary" in galaxy_css
    assert ".field:focus-within" in galaxy_css
    assert ".btn:focus-visible" in galaxy_css
    assert "@media (max-width: 720px)" in galaxy_css
    assert "@media (prefers-reduced-motion: reduce)" in galaxy_css
    assert "body.app-authenticated .admin-tools" in galaxy_css
