from __future__ import annotations

import hashlib

import pytest

from app import media
from app.config import Settings
from app.repository import Repository
from tests.conftest import animated_gif_bytes, png_bytes, still_gif_bytes


@pytest.fixture
def media_root(settings: Settings):
    settings.media_dir.mkdir(parents=True, exist_ok=True)

    return settings.media_dir


def make_run(repository: Repository, run_id: str = "run-1") -> str:
    repository.save_run(run_id, {"status": "completed"})

    return run_id


class TestPathSanitising:
    @pytest.mark.parametrize(
        ("given", "expected"),
        [
            ("screenshots/a.png", "screenshots/a.png"),
            ("/screenshots/a.png", "screenshots/a.png"),
            ("..\\..\\a.png", "a.png"),
            ("screenshots/../../etc/passwd.png", "screenshots/passwd.png"),
            ("a/b/c/d.png", "a/d.png"),
        ],
    )
    def test_reduces_to_a_safe_relative_path(self, given: str, expected: str) -> None:
        assert media.sanitize_path(given) == expected

    @pytest.mark.parametrize("given", ["", "..", "screenshots", "screenshots/noext", "/"])
    def test_rejects_paths_without_a_filename(self, given: str) -> None:
        assert media.sanitize_path(given) == ""

    def test_thumbs_directory_cannot_be_targeted(self) -> None:
        """Uploads must not be able to overwrite generated thumbnails."""
        assert media.sanitize_path("thumbs/evil.png") == "evil.png"


class TestStore:
    def test_writes_file_under_the_run_directory(self, repository, media_root) -> None:
        run_id = make_run(repository)

        record = media.store(repository, media_root, run_id, "screenshots/a.png", png_bytes())

        assert (media_root / run_id / "screenshots" / "a.png").is_file()
        assert record["mime"] == "image/png"
        assert (record["width"], record["height"]) == (1200, 800)

    def test_records_checksum_for_change_detection(self, repository, media_root) -> None:
        run_id = make_run(repository)
        data = png_bytes()

        media.store(repository, media_root, run_id, "screenshots/a.png", data)

        expected = hashlib.sha1(data).hexdigest()

        assert repository.artifact_manifest(run_id)["screenshots/a.png"] == expected

    def test_large_image_gets_a_thumbnail(self, repository, media_root) -> None:
        run_id = make_run(repository)

        record = media.store(repository, media_root, run_id, "screenshots/big.png", png_bytes((1600, 1000)))

        assert record["thumb_name"]
        assert (media_root / run_id / media.THUMB_DIRNAME / record["thumb_name"]).is_file()

    def test_small_image_serves_the_original(self, repository, media_root) -> None:
        run_id = make_run(repository)

        record = media.store(repository, media_root, run_id, "screenshots/s.png", png_bytes((100, 80)))

        assert record["thumb_name"] is None
        assert media.thumb_url_for(record) == media.url_for(run_id, "screenshots/s.png")

    def test_animated_gif_is_flagged_and_never_thumbnailed(self, repository, media_root) -> None:
        """Resizing an animated GIF would drop the animation."""
        run_id = make_run(repository)

        record = media.store(repository, media_root, run_id, "screenshots/clip.gif", animated_gif_bytes())

        assert record["animated"] == 1
        assert record["thumb_name"] is None

    def test_still_gif_is_not_flagged_as_animated(self, repository, media_root) -> None:
        run_id = make_run(repository)

        record = media.store(repository, media_root, run_id, "screenshots/one.gif", still_gif_bytes())

        assert record["animated"] == 0

    def test_reupload_updates_in_place(self, repository, media_root) -> None:
        run_id = make_run(repository)
        media.store(repository, media_root, run_id, "screenshots/a.png", png_bytes(colour=(1, 2, 3)))
        media.store(repository, media_root, run_id, "screenshots/a.png", png_bytes(colour=(9, 9, 9)))

        assert len(repository.artifacts_for(run_id)) == 1

    def test_natural_order_is_preserved(self, repository, media_root) -> None:
        run_id = make_run(repository)
        for name in ("10_last.png", "2_second.png", "1_first.png"):
            media.store(repository, media_root, run_id, f"screenshots/{name}", png_bytes((50, 50)))

        order = [artifact["file_name"] for artifact in repository.artifacts_for(run_id)]

        assert order == ["1_first.png", "2_second.png", "10_last.png"]


class TestStoreRejections:
    def test_rejects_unsupported_extension(self, repository, media_root) -> None:
        run_id = make_run(repository)

        with pytest.raises(media.MediaError) as error:
            media.store(repository, media_root, run_id, "screenshots/a.svg", b"<svg/>")

        assert error.value.status == 415

    def test_rejects_content_that_does_not_match_the_extension(self, repository, media_root) -> None:
        """An extension is not evidence: the bytes must really decode as that type."""
        run_id = make_run(repository)

        with pytest.raises(media.MediaError) as error:
            media.store(repository, media_root, run_id, "screenshots/a.png", still_gif_bytes())

        assert error.value.status == 415

    def test_rejects_non_image_bytes(self, repository, media_root) -> None:
        run_id = make_run(repository)

        with pytest.raises(media.MediaError):
            media.store(repository, media_root, run_id, "screenshots/a.png", b"not an image at all")

    def test_rejects_empty_body(self, repository, media_root) -> None:
        run_id = make_run(repository)

        with pytest.raises(media.MediaError):
            media.store(repository, media_root, run_id, "screenshots/a.png", b"")

    def test_rejects_unusable_path(self, repository, media_root) -> None:
        run_id = make_run(repository)

        with pytest.raises(media.MediaError):
            media.store(repository, media_root, run_id, "screenshots", png_bytes())


class TestDelete:
    def test_removes_the_run_directory(self, repository, media_root) -> None:
        run_id = make_run(repository)
        media.store(repository, media_root, run_id, "screenshots/a.png", png_bytes())

        media.delete_run(media_root, run_id)

        assert not (media_root / run_id).exists()

    def test_deleting_an_absent_run_is_harmless(self, media_root) -> None:
        media.delete_run(media_root, "never-existed")
