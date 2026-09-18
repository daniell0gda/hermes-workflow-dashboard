from __future__ import annotations

from app.markdown_render import renderer


def resolve(reference: str) -> str | None:
    known = {
        "screenshots/shot.png": "/media/run-1/screenshots/shot.png",
        "shot.png": "/media/run-1/screenshots/shot.png",
    }

    return known.get(reference)


class TestSafety:
    def test_raw_html_is_escaped(self) -> None:
        html = renderer.render("Before <script>alert(1)</script> after")

        assert "<script>" not in html
        assert "&lt;script&gt;" in html

    def test_inline_html_tags_are_escaped(self) -> None:
        assert "<b>" not in renderer.render("a <b>bold</b> word")

    def test_javascript_url_is_not_emitted_as_a_link(self) -> None:
        """markdown-it refuses the unsafe scheme, so the source stays literal
        text; what matters is that no anchor ever carries that href."""
        html = renderer.render("[click](javascript:alert(1))")

        assert "<a" not in html
        assert "href" not in html
        assert "click" in html


class TestImages:
    def test_known_reference_resolves_to_media_url(self) -> None:
        html = renderer.render("![shot](screenshots/shot.png)", resolve)

        assert '<img src="/media/run-1/screenshots/shot.png"' in html
        assert 'alt="shot"' in html
        assert 'loading="lazy"' in html

    def test_bare_filename_also_resolves(self) -> None:
        assert "/media/run-1/screenshots/shot.png" in renderer.render("![s](shot.png)", resolve)

    def test_unknown_reference_degrades_to_alt_text(self) -> None:
        html = renderer.render("![missing thing](screenshots/nope.png)", resolve)

        assert "<img" not in html
        assert "missing thing" in html
        assert "hfcd-missing-media" in html

    def test_without_a_resolver_no_image_is_emitted(self) -> None:
        assert "<img" not in renderer.render("![s](screenshots/shot.png)")

    def test_absolute_url_passes_through(self) -> None:
        html = renderer.render("![x](https://example.com/a.png)", resolve)

        assert '<img src="https://example.com/a.png"' in html


class TestLinks:
    def test_external_links_get_noopener(self) -> None:
        html = renderer.render("[site](https://example.com)")

        assert 'rel="noopener noreferrer"' in html

    def test_unresolvable_relative_link_renders_as_plain_text(self) -> None:
        html = renderer.render("see [the report](report.md)")

        assert "href" not in html
        assert "the report" in html


class TestStructure:
    def test_headings_shift_below_the_page_outline(self) -> None:
        html = renderer.render("# Title\n\n## Section\n")

        assert "<h3>Title</h3>" in html
        assert "<h4>Section</h4>" in html

    def test_headings_never_exceed_h6(self) -> None:
        assert "<h6>" in renderer.render("###### Deep\n")
        assert "<h7" not in renderer.render("###### Deep\n")

    def test_tables_render(self) -> None:
        html = renderer.render("| a | b |\n|---|---|\n| 1 | 2 |\n")

        assert "<table>" in html and "<th>a</th>" in html

    def test_nested_lists_with_images_render(self) -> None:
        html = renderer.render("- outer\n  - ![shot](screenshots/shot.png)\n", resolve)

        assert html.count("<ul>") == 2
        assert "<img" in html

    def test_fenced_code_is_preserved_and_escaped(self) -> None:
        html = renderer.render("```python\nprint('<hi>')\n```\n")

        assert "<pre>" in html
        assert "&lt;hi&gt;" in html

    def test_inline_code_and_emphasis(self) -> None:
        html = renderer.render("**bold** and `code` and ~~gone~~")

        assert "<strong>bold</strong>" in html
        assert "<code>code</code>" in html
        assert "<s>gone</s>" in html

    def test_real_report_shape_renders(self) -> None:
        """The shape the Hermes team-leader report actually uses."""
        html = renderer.render(
            "# Team-leader report\n\n"
            "- **Result:** completed\n"
            "- **Classification:** pass\n\n"
            "## ✅ Done\n"
            "- Grass sampling covers the map.\n\n"
            "## ⬜ Pending\n\n"
            "## ❌ Impossible\n"
        )

        assert "<h3>Team-leader report</h3>" in html
        assert "<strong>Classification:</strong>" in html
        assert "✅" in html

    def test_empty_input_is_empty_output(self) -> None:
        assert renderer.render("") == ""
        assert renderer.render("   \n  ") == ""
