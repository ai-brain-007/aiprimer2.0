"""save_config must keep the comments of config/pipeline.yaml: they are the owner's documentation of every knob."""

from __future__ import annotations

from pipeline.config import load_settings, save_config

COMMENTED = """# AI Primer pipeline configuration. Safe to commit: contains no secrets.

apify:
  metadata_actor: "apidojo/youtube-scraper"
  # Field names of the two actors; `setup fetch-apify-schemas` rewrites this block.
  input_templates:
    metadata:
      start_urls_key: startUrls
      max_results_key: maxResults
      extra: {}
    transcript:
      start_urls_key: startUrls
      extra: {}

kb:
  # Chunk size for helper agents.
  chunk_tokens: 12000

notion:
  version: "2022-06-28"
"""


def test_save_config_keeps_comments_and_quoting_while_updating_values(tmp_path):
    path = tmp_path / "pipeline.yaml"
    path.write_text(COMMENTED, encoding="utf-8")
    settings = load_settings(repo_root=tmp_path, config_path=path)
    tpl = settings.config["apify"]["input_templates"]
    tpl["metadata"].update({"max_results_key": "maxItems", "start_urls_format": "strings"})
    tpl["transcript"]["start_urls_key"] = "urls"
    del settings.config["kb"]["chunk_tokens"]
    settings.config["kb"]["parallel_helpers"] = 4
    save_config(settings, path)
    text = path.read_text(encoding="utf-8")
    for kept in ("# AI Primer pipeline configuration. Safe to commit: contains no secrets.", "# Field names of the two actors", "# Chunk size for helper agents.", 'metadata_actor: "apidojo/youtube-scraper"', 'version: "2022-06-28"'):
        assert kept in text, kept
    assert "max_results_key: maxItems" in text and "start_urls_format: strings" in text and "start_urls_key: urls" in text
    assert "chunk_tokens" not in text and "parallel_helpers: 4" in text
    assert load_settings(repo_root=tmp_path, config_path=path).config == settings.config


def test_save_config_writes_a_new_file_without_a_template(tmp_path):
    path = tmp_path / "fresh.yaml"
    settings = load_settings(repo_root=tmp_path, config_path=path)
    settings.config["apify"] = {"metadata_actor": "a/b"}
    save_config(settings, path)
    assert load_settings(repo_root=tmp_path, config_path=path).config == {"apify": {"metadata_actor": "a/b"}}
