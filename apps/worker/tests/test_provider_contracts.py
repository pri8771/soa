"""Provider contract tests (AIO-001): the DTO honesty invariants, the
shared validators, the reusable contract suites proven against minimal
deterministic reference adapters, and the structural guarantee that no
vendor SDK crosses the contract modules."""

import ast
import inspect
import sys
import uuid

import pytest

import soa_worker.providers.builtin as builtin_module
import soa_worker.providers.capabilities as capabilities_module
import soa_worker.providers.classification as classification_module
import soa_worker.providers.contract as contract_module
import soa_worker.providers.native_text as native_text_module
import soa_worker.providers.ocr as ocr_module
from soa_worker.providers.classification import (
    ClassificationRequest,
    ClassificationResult,
    ClassifierProvider,
    SplitProposal,
    SplitRequest,
    SplitResult,
    SplitterProvider,
    validate_classification_result,
    validate_split_result,
)
from soa_worker.providers.contract import (
    ClassifierProviderContract,
    NativeTextProviderContract,
    OcrProviderContract,
    SplitterProviderContract,
    classification_fixture,
    split_fixture,
)
from soa_worker.providers.native_text import (
    NativeTextError,
    NativeTextFailure,
    NativeTextPage,
    NativeTextProvider,
    NativeTextRequest,
    NativeTextResult,
    TextSpan,
    validate_native_text_result,
)
from soa_worker.providers.ocr import (
    OcrBlock,
    OcrLine,
    OcrPageInput,
    OcrPageResult,
    OcrProvider,
    OcrRequest,
    OcrResult,
    OcrWord,
    validate_ocr_result,
)

DOC_ID = uuid.UUID("7c9e4d10-0000-4000-8000-000000000001")
SHA = "a" * 64


def rect(x: float, y: float, w: float, h: float) -> tuple[tuple[float, float], ...]:
    return ((x, y), (x + w, y), (x + w, y + h), (x, y + h))


# ---------------------------------------------------------------------------
# Reference adapters: minimal, deterministic, honest. They exist to prove
# the shared contract suites actually run and enforce what they claim —
# real adapters (AIO-002+) subclass the same suites with real fixtures.
# ---------------------------------------------------------------------------


class LineNativeText:
    """Reads utf-8 lines from the request bytes; refuses marked fixtures
    the way a real PDF adapter refuses encrypted/corrupt files."""

    name = "ref-native-text"
    WIDTH, HEIGHT = 1000, 1400

    async def read(self, request: NativeTextRequest) -> NativeTextResult:
        if request.data.startswith(b"%ENCRYPTED"):
            raise NativeTextError(
                "the document is password-protected and cannot be read",
                failure=NativeTextFailure.ENCRYPTED,
            )
        if request.data.startswith(b"%CORRUPT"):
            raise NativeTextError(
                "the document structure is damaged and cannot be parsed",
                failure=NativeTextFailure.CORRUPT,
            )
        lines = [line for line in request.data.decode("utf-8").splitlines() if line.strip()]
        spans = tuple(
            TextSpan(text=line, polygon=rect(40, 40 + 30 * index, 400, 22))
            for index, line in enumerate(lines)
        )
        page = NativeTextPage(
            page_number=1, width_px=self.WIDTH, height_px=self.HEIGHT, spans=spans, coverage=1.0
        )
        return NativeTextResult(provider=self.name, pages=(page,))


class UtfGridOcr:
    """Treats the fixture 'image' bytes as utf-8 text and lays the words
    on a deterministic grid inside the page bounds."""

    name = "ref-ocr"

    async def recognize(self, request: OcrRequest) -> OcrResult:
        pages = []
        for page in sorted(request.pages, key=lambda p: p.page_number):
            tokens = page.image.decode("utf-8").split()
            words = tuple(
                OcrWord(text=token, polygon=rect(10 + 90 * index, 10, 80, 20), confidence=0.93)
                for index, token in enumerate(tokens)
            )
            line = OcrLine(
                text=" ".join(tokens),
                words=words,
                polygon=rect(10, 10, 10 + 90 * len(tokens), 20),
                confidence=0.93,
            )
            block = OcrBlock(lines=(line,), polygon=rect(0, 0, page.width_px, page.height_px))
            pages.append(
                OcrPageResult(
                    page_number=page.page_number,
                    blocks=(block,),
                    languages=(request.languages[0],),
                )
            )
        return OcrResult(provider=self.name, pages=tuple(pages))


class KeywordClassifier:
    """Closed-vocabulary keyword matcher; abstains on anything else."""

    name = "ref-classifier"

    async def classify(self, request: ClassificationRequest) -> ClassificationResult:
        text = " ".join(page.text or "" for page in request.pages).lower()
        for label, needle in (("purchase_order", "purchase order"), ("invoice", "invoice")):
            if needle in text and label in request.labels:
                return ClassificationResult(provider=self.name, label=label, confidence=0.9)
        return ClassificationResult(provider=self.name, label=None, confidence=0.0)


class MarkerSplitter:
    """Starts a new proposal at every page whose text begins BEGIN."""

    name = "ref-splitter"

    async def split(self, request: SplitRequest) -> SplitResult:
        pages = sorted(request.pages, key=lambda p: p.page_number)
        starts = [
            page.page_number
            for index, page in enumerate(pages)
            if index == 0 or (page.text or "").startswith("BEGIN")
        ]
        ends = [start - 1 for start in starts[1:]] + [pages[-1].page_number]
        proposals = tuple(
            SplitProposal(start_page=start, end_page=end, label=None, confidence=0.75)
            for start, end in zip(starts, ends, strict=True)
        )
        return SplitResult(provider=self.name, proposals=proposals)


# ---------------------------------------------------------------------------
# The shared suites, run against the references.
# ---------------------------------------------------------------------------


class TestReferenceNativeTextContract(NativeTextProviderContract):
    async def make_provider(self) -> NativeTextProvider:
        return LineNativeText()

    def contract_request(self) -> NativeTextRequest:
        return NativeTextRequest(
            document_id=DOC_ID,
            document_sha256=SHA,
            content_type="application/pdf",
            data=b"PURCHASE ORDER 4711\nAcme GmbH",
        )

    def rejection_fixtures(self) -> dict[NativeTextFailure, NativeTextRequest]:
        def fixture(data: bytes) -> NativeTextRequest:
            return NativeTextRequest(
                document_id=DOC_ID,
                document_sha256=SHA,
                content_type="application/pdf",
                data=data,
            )

        return {
            NativeTextFailure.ENCRYPTED: fixture(b"%ENCRYPTED payload"),
            NativeTextFailure.CORRUPT: fixture(b"%CORRUPT payload"),
        }


class TestReferenceOcrContract(OcrProviderContract):
    async def make_provider(self) -> OcrProvider:
        return UtfGridOcr()

    def contract_request(self) -> OcrRequest:
        return OcrRequest(
            document_id=DOC_ID,
            document_sha256=SHA,
            pages=(
                OcrPageInput(
                    page_number=1,
                    width_px=1240,
                    height_px=1754,
                    image=b"PO-4711 Acme GmbH",
                    content_type="image/png",
                ),
            ),
            languages=("en", "de"),
        )


class TestReferenceClassifierContract(ClassifierProviderContract):
    async def make_provider(self) -> ClassifierProvider:
        return KeywordClassifier()

    def contract_request(self) -> ClassificationRequest:
        return classification_fixture(("purchase order 4711 from Acme GmbH",))


class TestReferenceSplitterContract(SplitterProviderContract):
    async def make_provider(self) -> SplitterProvider:
        return MarkerSplitter()

    def contract_request(self) -> SplitRequest:
        return split_fixture(
            ("BEGIN purchase order 4711", "continued line items", "BEGIN invoice 99")
        )


# ---------------------------------------------------------------------------
# DTO honesty invariants and the shared validators.
# ---------------------------------------------------------------------------


class TestNativeTextInvariants:
    def test_spans_must_stay_inside_their_page(self) -> None:
        with pytest.raises(ValueError, match="bounds"):
            NativeTextPage(
                page_number=1,
                width_px=100,
                height_px=100,
                spans=(TextSpan("x", rect(90, 90, 50, 50)),),
                coverage=1.0,
            )

    def test_coverage_is_bounded(self) -> None:
        with pytest.raises(ValueError, match="coverage"):
            NativeTextPage(page_number=1, width_px=10, height_px=10, spans=(), coverage=1.5)

    def test_pages_are_unique_and_ordered(self) -> None:
        page = NativeTextPage(page_number=2, width_px=10, height_px=10, spans=(), coverage=0.0)
        first = NativeTextPage(page_number=1, width_px=10, height_px=10, spans=(), coverage=0.0)
        with pytest.raises(ValueError, match="ascending"):
            NativeTextResult(provider="p", pages=(page, first))

    def test_validator_enforces_the_page_cap(self) -> None:
        request = NativeTextRequest(
            document_id=DOC_ID,
            document_sha256=SHA,
            content_type="application/pdf",
            data=b"x",
            max_pages=1,
        )
        pages = tuple(
            NativeTextPage(page_number=n, width_px=10, height_px=10, spans=(), coverage=0.0)
            for n in (1, 2)
        )
        result = NativeTextResult(provider="p", pages=pages)
        assert any("capped" in v for v in validate_native_text_result(request, result))

    def test_failures_carry_a_classification(self) -> None:
        error = NativeTextError("cannot read", failure=NativeTextFailure.FONTS_UNMAPPABLE)
        assert error.failure is NativeTextFailure.FONTS_UNMAPPABLE
        assert error.retryable is False


class TestOcrInvariants:
    def make_request(self) -> OcrRequest:
        return OcrRequest(
            document_id=DOC_ID,
            document_sha256=SHA,
            pages=(
                OcrPageInput(
                    page_number=1,
                    width_px=100,
                    height_px=100,
                    image=b"i",
                    content_type="image/png",
                ),
            ),
            languages=("en",),
        )

    def result_with(self, page: OcrPageResult) -> OcrResult:
        return OcrResult(provider="p", pages=(page,))

    def test_requests_require_lowercase_languages(self) -> None:
        with pytest.raises(ValueError, match="lowercase"):
            OcrRequest(
                document_id=DOC_ID,
                document_sha256=SHA,
                pages=(),
                languages=("EN",),
            )

    def test_validator_rejects_unknown_pages(self) -> None:
        page = OcrPageResult(page_number=9, blocks=(), languages=("en",))
        violations = validate_ocr_result(self.make_request(), self.result_with(page))
        assert any("unknown page 9" in v for v in violations)

    def test_validator_rejects_unallowed_languages(self) -> None:
        page = OcrPageResult(page_number=1, blocks=(), languages=("fr",))
        violations = validate_ocr_result(self.make_request(), self.result_with(page))
        assert any("did not allow" in v for v in violations)

    def test_validator_rejects_out_of_bounds_geometry(self) -> None:
        word = OcrWord(text="x", polygon=rect(90, 90, 50, 20), confidence=0.5)
        line = OcrLine(text="x", words=(word,), polygon=rect(90, 90, 50, 20), confidence=0.5)
        block = OcrBlock(lines=(line,), polygon=rect(0, 0, 100, 100))
        page = OcrPageResult(page_number=1, blocks=(block,), languages=("en",))
        violations = validate_ocr_result(self.make_request(), self.result_with(page))
        assert any("bounds" in v for v in violations)

    def test_confidence_is_bounded(self) -> None:
        with pytest.raises(ValueError, match="confidence"):
            OcrWord(text="x", polygon=rect(0, 0, 1, 1), confidence=1.2)


class TestClassificationInvariants:
    def test_abstention_is_never_confident(self) -> None:
        with pytest.raises(ValueError, match="never confident"):
            ClassificationResult(provider="p", label=None, confidence=0.4)

    def test_labels_outside_the_vocabulary_are_violations(self) -> None:
        request = classification_fixture(("anything",))
        result = ClassificationResult(provider="p", label="receipt", confidence=0.9)
        violations = validate_classification_result(request, result)
        assert any("outside the request vocabulary" in v for v in violations)

    def test_request_pages_must_be_contiguous(self) -> None:
        pages = classification_fixture(("a", "b")).pages
        with pytest.raises(ValueError, match="contiguous"):
            ClassificationRequest(
                document_id=DOC_ID,
                document_sha256=SHA,
                pages=(pages[0], pages[1].__class__(page_number=5, width_px=10, height_px=10)),
                labels=("purchase_order",),
            )


class TestSplitInvariants:
    def request(self) -> SplitRequest:
        return split_fixture(("one", "two", "three"))

    def proposals(self, *ranges: tuple[int, int]) -> SplitResult:
        return SplitResult(
            provider="p",
            proposals=tuple(
                SplitProposal(start_page=start, end_page=end, label=None, confidence=0.5)
                for start, end in ranges
            ),
        )

    def test_an_exact_partition_is_conformant(self) -> None:
        assert validate_split_result(self.request(), self.proposals((1, 2), (3, 3))) == []

    def test_gaps_are_violations(self) -> None:
        violations = validate_split_result(self.request(), self.proposals((1, 1), (3, 3)))
        assert any("gap or overlap" in v for v in violations)

    def test_overlaps_are_violations(self) -> None:
        violations = validate_split_result(self.request(), self.proposals((1, 2), (2, 3)))
        assert any("gap or overlap" in v for v in violations)

    def test_dropping_the_tail_is_a_violation(self) -> None:
        violations = validate_split_result(self.request(), self.proposals((1, 2)))
        assert any("document has 3" in v for v in violations)

    def test_an_empty_split_is_a_violation(self) -> None:
        violations = validate_split_result(self.request(), self.proposals())
        assert any("at least one proposal" in v for v in violations)

    def test_a_proposal_cannot_end_before_it_starts(self) -> None:
        with pytest.raises(ValueError, match="end before it starts"):
            SplitProposal(start_page=3, end_page=2, label=None, confidence=0.5)


# ---------------------------------------------------------------------------
# Structural acceptance: vendor DTOs (and vendor SDKs) stay inside
# adapters — the contract modules import nothing beyond the stdlib and
# the worker itself (pytest is tolerated in the test-suite module).
# ---------------------------------------------------------------------------


def imported_roots(module: object) -> set[str]:
    tree = ast.parse(inspect.getsource(module))
    roots: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            roots.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            roots.add(node.module.split(".")[0])
    return roots


@pytest.mark.parametrize(
    ("module", "extra_allowed"),
    [
        (capabilities_module, set()),
        (native_text_module, set()),
        (ocr_module, set()),
        (classification_module, set()),
        (builtin_module, set()),
        (contract_module, {"pytest"}),
    ],
)
def test_contract_modules_import_no_vendor_sdks(module: object, extra_allowed: set[str]) -> None:
    allowed = set(sys.stdlib_module_names) | {"soa_worker"} | extra_allowed
    outside = imported_roots(module) - allowed
    assert outside == set(), (
        f"{module.__name__} imports {sorted(outside)} — vendor DTOs and SDKs "
        "must stay inside adapters, never in the contract"
    )
