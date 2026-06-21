from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urlsplit


ROOT = Path(__file__).resolve().parents[2]
PUBLIC = ROOT / "public"


class _AssetParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.assets = []

    def handle_starttag(self, tag, attrs):
        attr_map = dict(attrs)
        if tag == "script" and attr_map.get("src", "").startswith("/"):
            self.assets.append(attr_map["src"])
        if tag == "link" and attr_map.get("rel") == "stylesheet" and attr_map.get("href", "").startswith("/"):
            self.assets.append(attr_map["href"])


def _local_public_assets(html_path):
    parser = _AssetParser()
    parser.feed(html_path.read_text(encoding="utf-8"))
    return parser.assets


def test_public_html_css_and_js_assets_are_cache_busted_and_exist():
    html_paths = sorted(PUBLIC.glob("*.html"))
    assert html_paths

    for html_path in html_paths:
        for asset in _local_public_assets(html_path):
            parsed = urlsplit(asset)
            assert parsed.query.startswith("v="), f"{html_path.name} asset lacks ?v=: {asset}"
            target = PUBLIC / parsed.path.lstrip("/")
            assert target.is_file(), f"{html_path.name} asset is missing: {asset}"
