"""Image preprocessing tests (AIO-003): synthetic page fixtures with
known defects, quality metrics, honest change recording, bounded and
deterministic processing, and the retained original."""

import io
import random

import pytest
from PIL import Image, ImageDraw

from soa_worker.image_preprocessing import (
    PreprocessError,
    PreprocessOptions,
    preprocess_page_image,
)

WIDTH, HEIGHT = 600, 800


def png_of(image: Image.Image) -> bytes:
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def text_page(*, ink: int = 0, paper: int = 255) -> Image.Image:
    """Horizontal 'text lines': dark bars with word-like gaps."""
    image = Image.new("RGB", (WIDTH, HEIGHT), (paper, paper, paper))
    draw = ImageDraw.Draw(image)
    gaps = random.Random(7)
    for top in range(60, HEIGHT - 60, 28):
        x = 50
        while x < WIDTH - 60:
            word = gaps.randint(30, 90)
            draw.rectangle([x, top, min(x + word, WIDTH - 50), top + 10], fill=(ink, ink, ink))
            x += word + 14
    return image


def rotated_page() -> bytes:
    return png_of(text_page().transpose(Image.Transpose.ROTATE_90))


def skewed_page(angle: float) -> bytes:
    return png_of(
        text_page().rotate(angle, resample=Image.Resampling.BICUBIC, fillcolor=(255, 255, 255))
    )


def blank_page() -> bytes:
    image = Image.new("RGB", (WIDTH, HEIGHT), (255, 255, 255))
    ImageDraw.Draw(image).rectangle([300, 400, 302, 402], fill=(0, 0, 0))  # a speck
    return png_of(image)


def low_contrast_page() -> bytes:
    return png_of(text_page(ink=120, paper=200))


def noisy_page() -> bytes:
    image = text_page()
    salt = random.Random(11)
    pixels = image.load()
    for _ in range(2500):  # isolated salt-and-pepper specks
        x, y = salt.randrange(WIDTH), salt.randrange(HEIGHT)
        pixels[x, y] = (0, 0, 0)
    return png_of(image)


class TestOrientation:
    def test_sideways_text_is_turned_upright_and_recorded(self) -> None:
        source = rotated_page()
        result = preprocess_page_image(source)
        assert result.report.rotation_degrees == 90
        assert (result.width_px, result.height_px) == (WIDTH, HEIGHT)  # turned back
        assert any("rotated 90" in op for op in result.report.operations)
        # The 180° ambiguity is stated, not silently guessed away.
        assert any("cannot be told apart" in note for note in result.report.notes)

    def test_an_upright_page_is_not_rotated(self) -> None:
        result = preprocess_page_image(png_of(text_page()))
        assert result.report.rotation_degrees == 0

    def test_orientation_can_be_disabled(self) -> None:
        result = preprocess_page_image(rotated_page(), PreprocessOptions(orient=False))
        assert result.report.rotation_degrees == 0


class TestDeskew:
    def test_a_skewed_page_is_straightened_within_tolerance(self) -> None:
        result = preprocess_page_image(skewed_page(3.0))
        # The fixture was rotated +3°; the correction must be ≈ -3°.
        assert result.report.deskew_degrees == pytest.approx(-3.0, abs=1.0)
        assert any("deskewed" in op for op in result.report.operations)
        assert (result.width_px, result.height_px) == (WIDTH, HEIGHT)  # no expand

    def test_a_straight_page_is_left_alone(self) -> None:
        result = preprocess_page_image(png_of(text_page()))
        assert result.report.deskew_degrees == 0.0
        assert result.report.operations == ()

    def test_deskew_can_be_disabled(self) -> None:
        result = preprocess_page_image(skewed_page(3.0), PreprocessOptions(deskew=False))
        assert result.report.deskew_degrees == 0.0


class TestQualityAndBlank:
    def test_a_blank_page_is_reported_and_kept(self) -> None:
        result = preprocess_page_image(blank_page())
        assert result.report.quality.blank is True
        assert result.report.quality.ink_fraction < 0.002
        assert any("appears blank" in note for note in result.report.notes)
        assert result.image_png  # kept, not discarded

    def test_a_text_page_is_not_blank(self) -> None:
        result = preprocess_page_image(png_of(text_page()))
        assert result.report.quality.blank is False
        assert result.report.quality.ink_fraction > 0.01

    def test_quality_is_measured_on_the_final_image(self) -> None:
        enhanced = preprocess_page_image(low_contrast_page(), PreprocessOptions(contrast=True))
        untouched = preprocess_page_image(low_contrast_page())
        assert enhanced.report.quality.dynamic_range > untouched.report.quality.dynamic_range


class TestConservativeOptions:
    def test_contrast_is_opt_in(self) -> None:
        result = preprocess_page_image(low_contrast_page())
        assert result.report.contrast_applied is False
        assert result.report.operations == ()

    def test_contrast_applies_only_when_the_range_is_poor(self) -> None:
        poor = preprocess_page_image(low_contrast_page(), PreprocessOptions(contrast=True))
        assert poor.report.contrast_applied is True
        assert any("stretched contrast" in op for op in poor.report.operations)
        good = preprocess_page_image(png_of(text_page()), PreprocessOptions(contrast=True))
        assert good.report.contrast_applied is False
        assert any("not needed" in note for note in good.report.notes)

    def test_noise_filter_is_opt_in_and_removes_specks(self) -> None:
        untouched = preprocess_page_image(noisy_page())
        assert untouched.report.noise_filter_applied is False
        filtered = preprocess_page_image(noisy_page(), PreprocessOptions(noise_filter=True))
        assert filtered.report.noise_filter_applied is True
        assert filtered.report.quality.ink_fraction < untouched.report.quality.ink_fraction, (
            "isolated specks must be gone from the final image"
        )


class TestHonestyAndBounds:
    def test_the_input_bytes_are_never_modified(self) -> None:
        source = rotated_page()
        keep = bytes(source)
        preprocess_page_image(source)
        assert source == keep

    def test_a_clean_page_returns_the_input_byte_identical(self) -> None:
        source = png_of(text_page())
        result = preprocess_page_image(source)
        assert result.report.operations == ()
        assert result.image_png == source

    def test_every_change_is_recorded_in_order(self) -> None:
        source = png_of(
            text_page(ink=120, paper=200).transpose(Image.Transpose.ROTATE_90),
        )
        result = preprocess_page_image(source, PreprocessOptions(contrast=True, noise_filter=True))
        kinds = [op.split()[0] for op in result.report.operations]
        assert kinds[0] == "rotated"
        assert "stretched" in kinds
        assert kinds[-1] == "applied"
        assert result.image_png != source

    def test_processing_is_deterministic(self) -> None:
        first = preprocess_page_image(skewed_page(3.0))
        second = preprocess_page_image(skewed_page(3.0))
        assert first == second

    def test_oversized_rasters_are_refused_with_the_budget_named(self) -> None:
        huge = png_of(Image.new("RGB", (6000, 5000), (255, 255, 255)))
        with pytest.raises(PreprocessError, match="pixel budget"):
            preprocess_page_image(huge)

    def test_undecodable_bytes_are_refused_safely(self) -> None:
        with pytest.raises(PreprocessError, match="could not be decoded"):
            preprocess_page_image(b"not a png at all")
