"""Render the repository README as the documentation landing page."""
from pathlib import Path


def on_page_markdown(markdown, page, config, **kwargs):
    if page.file.src_uri == "index.md":
        root = Path(config.config_file_path).resolve().parent
        return (root / "README.md").read_text(encoding="utf-8")
    return markdown
