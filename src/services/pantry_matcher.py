"""Deterministic, identity-safe local matching of extracted photo facts."""

from __future__ import annotations

import hashlib
import json
import math
import re
import unicodedata
from dataclasses import dataclass
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any, Iterable, Protocol, Sequence

from models.food import Food
from models.photo_analysis import FoodForm, ProductFacts, ReceiptLineFacts, ReceiptScanItem

MODEL_NAME = "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"
MODEL_VERSION = f"fastembed-0.8.0:{MODEL_NAME}"
TOP_K = 8
SEMANTIC_WEIGHT = 0.80
LEXICAL_WEIGHT = 0.20
PRESELECT_MIN_SCORE = 0.78
PRESELECT_MIN_MARGIN = 0.12

DATA_PATH = Path(__file__).resolve().parent.parent / "data" / "pantry_aliases.json"
BUNDLED_MODEL_PATH = (
    Path(__file__).resolve().parent.parent
    / "assets"
    / "models"
    / "paraphrase-multilingual-MiniLM-L12-v2"
)

MATCHING_FORMS = {
    FoodForm.FRESH.value,
    FoodForm.DRY.value,
    FoodForm.CANNED.value,
    FoodForm.FROZEN.value,
    FoodForm.COOKED.value,
    FoodForm.PREPARED.value,
}

_ABBREVIATIONS = {
    "wht": "white",
    "brn": "brown",
    "blk": "black",
    "lng": "long",
    "grn": "grain",
    "whl": "whole",
    "lntl": "lentil",
    "tom": "tomato",
    "cn": "canned",
    "frz": "frozen",
    "ozs": "oz",
    "lbs": "lb",
    "kgs": "kg",
}

_PACKAGE_WORDS = {
    "bag", "bags", "box", "boxes", "bottle", "bottles", "can", "cans",
    "carton", "cartons", "count", "ct", "each", "ea", "jar", "jars",
    "pack", "packs", "package", "packages", "pkg", "pkgs", "size",
}

_QUANTITY_RE = re.compile(
    r"\b\d+(?:[.,]\d+)?\s*(?:g|kg|oz|lb|ml|l|fl\s*oz|gal|qt|pt)\b",
    re.IGNORECASE,
)


class Embedder(Protocol):
    def embed(self, documents: Sequence[str]) -> Iterable[Sequence[float]]: ...


@dataclass(frozen=True)
class AliasRule:
    text: str
    forms: frozenset[str]
    language: str | None = None


@dataclass(frozen=True)
class AliasEntry:
    food_id: str
    aliases: tuple[AliasRule, ...]
    concepts: tuple[str, ...]
    forms: frozenset[str]


@dataclass(frozen=True)
class MatchCandidate:
    food_id: str
    name: str
    group: str
    form: str
    match_score: float
    cosine_similarity: float | None
    lexical_score: float
    reason: str
    exact: bool = False
    preselected: bool = False


@dataclass(frozen=True)
class MatchResult:
    query: str
    candidates: tuple[MatchCandidate, ...]
    selected_food_id: str | None
    semantic_available: bool
    status_message: str | None = None


def normalize_identity(text: str) -> str:
    """Unicode, punctuation, abbreviation, and conservative plural cleanup."""

    text = unicodedata.normalize("NFKD", text).casefold()
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    tokens = re.findall(r"[a-z0-9]+", text)
    normalized: list[str] = []
    for token in tokens:
        token = _ABBREVIATIONS.get(token, token)
        if len(token) > 4 and token.endswith("ies"):
            token = token[:-3] + "y"
        elif len(token) > 4 and token.endswith("es") and not token.endswith(("ses", "ches")):
            token = token[:-2]
        elif len(token) > 3 and token.endswith("s") and not token.endswith("ss"):
            token = token[:-1]
        normalized.append(token)
    return " ".join(normalized)


def identity_query(
    generic_name: str,
    observed_name: str = "",
    brand: str | None = None,
    package_text: str | None = None,
) -> str:
    """Build a food-identity query with brand and package-size tokens removed."""

    source = generic_name.strip() or observed_name.strip()
    if not source:
        source = observed_name
    brand_normalized = normalize_identity(brand or "")
    source = _QUANTITY_RE.sub(" ", source)
    tokens = normalize_identity(source).split()
    brand_tokens = set(brand_normalized.split())
    package_tokens = set(normalize_identity(package_text or "").split())
    filtered = [
        token for token in tokens
        if token not in brand_tokens
        and token not in _PACKAGE_WORDS
        and not (token in package_tokens and (token.isdigit() or token in _PACKAGE_WORDS))
        and not token.isdigit()
    ]
    return " ".join(filtered)


def lexical_score(query: str, candidate: str) -> float:
    query = normalize_identity(query)
    candidate = normalize_identity(candidate)
    if not query or not candidate:
        return 0.0
    sequence = SequenceMatcher(None, query, candidate).ratio()
    query_tokens = set(query.split())
    candidate_tokens = set(candidate.split())
    coverage = len(query_tokens & candidate_tokens) / len(query_tokens)
    return 0.60 * sequence + 0.40 * coverage


def _read_aliases(path: Path) -> dict[str, AliasEntry]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"Pantry aliases could not be loaded: {exc}") from exc
    if data.get("version") != 1 or not isinstance(data.get("entries"), list):
        raise ValueError("Unsupported Pantry alias data version.")
    entries: dict[str, AliasEntry] = {}
    for raw in data["entries"]:
        food_id = str(raw["food_id"])
        rules: list[AliasRule] = []
        for key in ("canonical_aliases", "receipt_abbreviations", "translations"):
            for alias in raw.get(key, []):
                rules.append(AliasRule(
                    text=str(alias["text"]),
                    forms=frozenset(str(value) for value in alias.get("forms", [])),
                    language=(str(alias["language"]) if alias.get("language") else None),
                ))
        entries[food_id] = AliasEntry(
            food_id=food_id,
            aliases=tuple(rules),
            concepts=tuple(normalize_identity(value) for value in raw.get("concepts", [])),
            forms=frozenset(str(value) for value in raw.get("forms", [])),
        )
    return entries


def _catalog_form(food: Food) -> str:
    form = normalize_identity(food.form).replace(" ", "_")
    if form == "raw":
        return FoodForm.FRESH.value
    if form in MATCHING_FORMS:
        return form
    prep = str(food.prep_state.value)
    if prep == "raw":
        return FoodForm.FRESH.value
    return prep if prep in MATCHING_FORMS else FoodForm.UNKNOWN.value


def forms_conflict(observed_form: str, catalog_form: str) -> bool:
    return (
        observed_form in MATCHING_FORMS
        and catalog_form in MATCHING_FORMS
        and observed_form != catalog_form
    )


def _cosine(left: Sequence[float], right: Sequence[float]) -> float:
    if len(left) != len(right) or len(left) == 0:
        return 0.0
    dot = sum(float(a) * float(b) for a, b in zip(left, right))
    left_norm = math.sqrt(sum(float(value) ** 2 for value in left))
    right_norm = math.sqrt(sum(float(value) ** 2 for value in right))
    if left_norm == 0 or right_norm == 0:
        return 0.0
    return max(-1.0, min(1.0, dot / (left_norm * right_norm)))


class CatalogMatcher:
    def __init__(
        self,
        foods: Sequence[Food],
        *,
        alias_path: Path = DATA_PATH,
        cache_dir: Path | None = None,
        embedder: Embedder | None = None,
        bundled_model_path: Path = BUNDLED_MODEL_PATH,
    ):
        self.foods = tuple(foods)
        self.foods_by_id = {food.id: food for food in foods}
        self.aliases = _read_aliases(Path(alias_path))
        self.cache_dir = Path(cache_dir) if cache_dir is not None else None
        self._embedder = embedder
        self._embedder_attempted = embedder is not None
        self._bundled_model_path = Path(bundled_model_path)
        self._semantic_error: str | None = None
        self._descriptions = {
            food.id: self._description(food) for food in self.foods
        }
        self.signature = catalog_signature(self.foods, self.aliases)
        self._catalog_vectors: dict[str, list[float]] | None = None

    def _ensure_embedder(self) -> None:
        if self._embedder is not None or self._embedder_attempted:
            return
        self._embedder_attempted = True
        if self._bundled_model_path.is_dir():
            try:
                from fastembed import TextEmbedding

                self._embedder = TextEmbedding(
                    model_name=MODEL_NAME,
                    specific_model_path=str(self._bundled_model_path),
                    local_files_only=True,
                )
            except Exception as exc:  # Runtime fallback is a planned behavior.
                self._semantic_error = str(exc)
                self._embedder = None

    @property
    def semantic_available(self) -> bool:
        return self._embedder is not None

    def _description(self, food: Food) -> str:
        entry = self.aliases.get(food.id)
        safe_aliases = [rule.text for rule in entry.aliases] if entry else []
        concepts = list(entry.concepts) if entry else []
        return " | ".join(filter(None, [
            food.name,
            *safe_aliases,
            *concepts,
            _catalog_form(food),
            food.food_group.value,
        ]))

    def _exact_ids(self, query: str, observed_form: str) -> set[str]:
        normalized = normalize_identity(query)
        exact: set[str] = set()
        for food in self.foods:
            catalog_form = _catalog_form(food)
            if forms_conflict(observed_form, catalog_form):
                continue
            if normalized == normalize_identity(food.name):
                exact.add(food.id)
            entry = self.aliases.get(food.id)
            if entry is None:
                continue
            for rule in entry.aliases:
                if normalized != normalize_identity(rule.text):
                    continue
                if rule.forms and observed_form not in rule.forms:
                    continue
                exact.add(food.id)
        return exact

    def _load_or_build_vectors(self) -> dict[str, list[float]]:
        if self._catalog_vectors is not None:
            return self._catalog_vectors
        self._ensure_embedder()
        if self._embedder is None:
            return {}
        cache_path = self.cache_dir / "catalog_embeddings.json" if self.cache_dir else None
        if cache_path is not None:
            try:
                cached = json.loads(cache_path.read_text(encoding="utf-8"))
                if (
                    cached.get("signature") == self.signature
                    and cached.get("model_version") == MODEL_VERSION
                    and set(cached.get("vectors", {})) == set(self._descriptions)
                ):
                    self._catalog_vectors = {
                        food_id: [float(value) for value in vector]
                        for food_id, vector in cached["vectors"].items()
                    }
                    return self._catalog_vectors
            except (OSError, ValueError, TypeError, json.JSONDecodeError):
                pass
        try:
            food_ids = sorted(self._descriptions)
            vectors = list(self._embedder.embed([self._descriptions[fid] for fid in food_ids]))
            if len(vectors) != len(food_ids):
                raise ValueError("The embedding model returned the wrong vector count.")
            self._catalog_vectors = {
                food_id: [float(value) for value in vector]
                for food_id, vector in zip(food_ids, vectors)
            }
            if cache_path is not None:
                cache_path.parent.mkdir(parents=True, exist_ok=True)
                cache_path.write_text(json.dumps({
                    "signature": self.signature,
                    "model_version": MODEL_VERSION,
                    "vectors": self._catalog_vectors,
                }, separators=(",", ":")), encoding="utf-8")
            return self._catalog_vectors
        except Exception as exc:
            self._semantic_error = str(exc)
            self._embedder = None
            self._catalog_vectors = {}
            return {}

    def match(
        self,
        facts: ProductFacts | ReceiptLineFacts | ReceiptScanItem,
        *,
        plan_food_ids: Iterable[str] = (),
    ) -> MatchResult:
        if isinstance(facts, ProductFacts):
            query = identity_query(
                facts.generic_food_name,
                facts.observed_name,
                facts.brand,
                facts.package_text,
            )
        else:
            query = identity_query(
                facts.generic_item_name,
                facts.raw_printed_text,
                facts.brand,
                None,
            )
        observed_form = facts.form.value
        exact_ids = self._exact_ids(query, observed_form)
        plan_ids = set(plan_food_ids)

        vectors = self._load_or_build_vectors()
        cosine_by_id: dict[str, float] = {}
        retrieval_ids: list[str]
        if vectors and self._embedder is not None:
            try:
                query_vector = list(self._embedder.embed([query]))[0]
                cosine_by_id = {
                    food_id: _cosine(query_vector, vector)
                    for food_id, vector in vectors.items()
                }
                retrieval_ids = [
                    food_id for food_id, _score in sorted(
                        cosine_by_id.items(), key=lambda item: (-item[1], item[0])
                    )[:TOP_K]
                ]
            except Exception as exc:
                self._semantic_error = str(exc)
                self._embedder = None
                retrieval_ids = [food.id for food in self.foods]
                cosine_by_id = {}
        else:
            retrieval_ids = [food.id for food in self.foods]

        # Exact identities must remain visible even if semantic retrieval misses
        # one; they still pass the same hard form gate.
        retrieval_ids = list(dict.fromkeys([*exact_ids, *retrieval_ids]))
        candidates: list[MatchCandidate] = []
        for food_id in retrieval_ids:
            food = self.foods_by_id[food_id]
            catalog_form = _catalog_form(food)
            if forms_conflict(observed_form, catalog_form):
                continue
            lexical = max(
                lexical_score(query, food.name),
                *(
                    [lexical_score(query, rule.text) for rule in self.aliases[food_id].aliases]
                    if food_id in self.aliases and self.aliases[food_id].aliases
                    else [0.0]
                ),
            )
            cosine = cosine_by_id.get(food_id)
            score = (
                SEMANTIC_WEIGHT * cosine + LEXICAL_WEIGHT * lexical
                if cosine is not None else lexical
            )
            exact = food_id in exact_ids and len(exact_ids) == 1
            if exact:
                score = 1.0
                reason = "Exact Pantry alias"
            elif cosine is not None:
                agreement = (
                    f"; {catalog_form} form agrees"
                    if observed_form in MATCHING_FORMS else ""
                )
                reason = f"Semantic name match{agreement}."
            else:
                reason = "Lexical name match; semantic matching unavailable."
            candidates.append(MatchCandidate(
                food_id=food.id,
                name=food.name,
                group=food.food_group.value,
                form=catalog_form,
                match_score=max(0.0, min(1.0, score)),
                cosine_similarity=cosine,
                lexical_score=lexical,
                reason=reason,
                exact=exact,
            ))

        candidates.sort(key=lambda item: (
            -item.match_score,
            0 if item.food_id in plan_ids else 1,
            item.food_id,
        ))
        candidates = candidates[:TOP_K]
        selected: str | None = None
        if len(exact_ids) == 1 and any(c.food_id in exact_ids for c in candidates):
            selected = next(iter(exact_ids))
        elif self.semantic_available and candidates:
            first = candidates[0]
            second_score = candidates[1].match_score if len(candidates) > 1 else 0.0
            if (
                first.match_score >= PRESELECT_MIN_SCORE
                and first.match_score - second_score >= PRESELECT_MIN_MARGIN
                and not self._same_concept_form_uncertainty(first.food_id, observed_form)
            ):
                selected = first.food_id
        if selected is not None:
            candidates = [
                MatchCandidate(**{**candidate.__dict__, "preselected": candidate.food_id == selected})
                for candidate in candidates
            ]
        return MatchResult(
            query=query,
            candidates=tuple(candidates),
            selected_food_id=selected,
            semantic_available=self.semantic_available,
            status_message=(
                None if self.semantic_available
                else "Semantic matching unavailable; non-exact matches require manual confirmation."
            ),
        )

    def _same_concept_form_uncertainty(self, food_id: str, observed_form: str) -> bool:
        entry = self.aliases.get(food_id)
        if entry is None or not entry.concepts:
            return False
        concepts = set(entry.concepts)
        for other in self.foods:
            if other.id == food_id:
                continue
            other_entry = self.aliases.get(other.id)
            if other_entry is None or not concepts.intersection(other_entry.concepts):
                continue
            other_form = _catalog_form(other)
            if other_form == FoodForm.UNKNOWN.value or forms_conflict(observed_form, other_form):
                return True
        return False


def catalog_signature(foods: Sequence[Food], aliases: dict[str, AliasEntry]) -> str:
    payload: list[dict[str, Any]] = []
    for food in sorted(foods, key=lambda value: value.id):
        entry = aliases.get(food.id)
        payload.append({
            "id": food.id,
            "name": food.name,
            "aliases": [
                {"text": rule.text, "forms": sorted(rule.forms), "language": rule.language}
                for rule in (entry.aliases if entry else ())
            ],
            "concepts": list(entry.concepts) if entry else [],
            "forms": sorted(entry.forms) if entry else [],
            "catalog_form": _catalog_form(food),
            "group": food.food_group.value,
            "model_version": MODEL_VERSION,
        })
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()
