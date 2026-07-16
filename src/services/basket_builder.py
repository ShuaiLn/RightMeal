"""Build the shopping basket from a recipe plan's ingredient demand.

Recipe-first inverts the old flow: the meal plan fixes what must be cooked, so
the basket is simply (ingredient demand - pantry) rounded up into whole
packages and priced. Returns an OptimizationResult so the explanation service,
shopping list, and unused-food logic keep working unchanged.

One per-food pricing core feeds both ``price_demand`` (full result) and
``price_slice`` (cost + unpriced gap only, for the budget-repair inner loop),
so the two can never drift. Both are pure: no argument is mutated and results
are call-order independent. Each food is priced independently — its package
count depends only on its own total demand and pantry stock — so pricing a
slice of changed foods against cumulative demand reconstructs exact totals.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, ROUND_CEILING
from typing import Iterator, Mapping, Sequence

from models.basket import BasketItem, BudgetStatus, OptimizationResult, PantryUse
from models.food import Food, FoodGroup, Nutrients, PackageOption
from models.pricing import (
    PackageOffer,
    PriceQuote,
    PriceSource,
    dollars_to_cents,
    package_id_for,
)
from models.profile import HouseholdProfile
from services.nutrition import NutritionService

GRAM_EPSILON = 0.05
COST_EPSILON = 0.01  # dollars — totals within a cent count as equal
_DEEP_SHORTFALL = 0.5  # a nutrient below 50% of target => nutrition infeasible


PricingValue = PriceQuote | Sequence[PackageOffer]
PricingInputs = Mapping[str, PricingValue]


@dataclass(frozen=True)
class _OfferedPackage:
    """PackageOption-compatible snapshot for a non-catalog retailer package."""

    package_id: str
    label: str
    grams: float
    seed_price: float
    ml: float | None = None


def _package_for_offer(food: Food, offer: PackageOffer) -> PackageOption | _OfferedPackage:
    for package in food.package_options:
        if package_id_for(food.id, package) == offer.package_id:
            return package
    return _OfferedPackage(
        package_id=offer.package_id,
        label=offer.package_label,
        grams=offer.package_grams,
        ml=offer.package_ml,
        seed_price=offer.price,
    )


def _offers_from_quote(food: Food, quote: PriceQuote) -> tuple[PackageOffer, ...]:
    """Compatibility adapter from one normalized quote to package offers.

    Native offer callers never use this.  Seed quotes recover the catalog's
    real per-package seed prices; a legacy live/BLS normalized quote is rounded
    to cents separately for each catalog package.
    """

    if quote.price <= 0 or quote.normalized_unit_price <= 0:
        return ()
    if quote.source is PriceSource.SEED_ESTIMATE:
        return tuple(
            PackageOffer.for_catalog_package(
                food,
                package,
                price_cents=dollars_to_cents(package.seed_price),
                source=quote.source,
                store=quote.store,
                matched_product_name=quote.matched_product_name,
                confidence=quote.confidence,
                is_estimate=quote.is_estimate,
                last_updated=quote.last_updated,
                match_reason=quote.match_reason,
                provider_error=quote.provider_error,
            )
            for package in food.package_options
            if dollars_to_cents(package.seed_price) > 0
        )
    offers: list[PackageOffer] = []
    for package in food.package_options:
        basis = package.ml if food.is_liquid and quote.normalized_unit == "100ml" else package.grams
        if basis is None or basis <= 0:
            continue
        cents = dollars_to_cents(quote.normalized_unit_price * (basis / 100.0))
        if cents <= 0:
            continue
        offers.append(
            PackageOffer.for_catalog_package(
                food,
                package,
                price_cents=cents,
                source=quote.source,
                store=quote.store,
                matched_product_name=quote.matched_product_name,
                confidence=quote.confidence,
                is_estimate=quote.is_estimate,
                last_updated=quote.last_updated,
                match_reason=quote.match_reason,
                raw_unit=quote.raw_unit,
                provider_error=quote.provider_error,
            )
        )
    return tuple(offers)


def offers_for_food(food: Food, value: PricingValue | None) -> tuple[PackageOffer, ...]:
    """Normalize a legacy quote or native offer collection, deduping exact triples."""

    if value is None:
        return ()
    raw = _offers_from_quote(food, value) if isinstance(value, PriceQuote) else tuple(value)
    by_triple: dict[tuple[str, str, str], PackageOffer] = {}
    for offer in raw:
        if not isinstance(offer, PackageOffer):
            raise TypeError("pricing collections must contain PackageOffer values")
        if offer.food_id != food.id:
            raise ValueError(f"offer {offer.offer_id!r} belongs to a different food")
        key = (offer.food_id, offer.package_id, offer.offer_id)
        existing = by_triple.get(key)
        if existing is not None and existing != offer:
            raise ValueError(f"conflicting snapshots for package offer {offer.offer_id!r}")
        by_triple[key] = offer
    return tuple(sorted(by_triple.values(), key=lambda o: (o.offer_id, o.package_id)))


@dataclass(frozen=True)
class _CombinationState:
    cost_cents: int
    package_count: int
    counts: tuple[int, ...]


def _selection_signature(
    offers: Sequence[PackageOffer], counts: Sequence[int]
) -> tuple[str, ...]:
    return tuple(
        offer.offer_id
        for offer, count in zip(offers, counts)
        for _ in range(count)
    )


def minimum_cost_package_combination(
    food: Food,
    grams_needed: float,
    offers: Sequence[PackageOffer],
) -> tuple[BasketItem, ...]:
    """Return the exact deterministic minimum-cost whole-package combination.

    Ordering is cost, waste, package count, then stable expanded offer-id order.
    The dynamic program enumerates each unordered offer-count vector once and
    keeps only the best state for an identical covered weight.
    """

    if grams_needed <= GRAM_EPSILON:
        return ()
    ordered = offers_for_food(food, offers)
    if not ordered:
        return ()

    need = Decimal(str(grams_needed - GRAM_EPSILON))
    gram_sizes = tuple(Decimal(str(o.package_grams)) for o in ordered)
    max_size = max(gram_sizes)
    # An optimal positive-price cover cannot reach need + max_size: removing
    # any one package would still cover the need for less money.
    coverage_bound = need + max_size

    single_covers: list[tuple[tuple, int]] = []
    for index, (offer, grams) in enumerate(zip(ordered, gram_sizes)):
        count = max(1, int((need / grams).to_integral_value(rounding=ROUND_CEILING)))
        counts = tuple(count if i == index else 0 for i in range(len(ordered)))
        total_grams = grams * count
        key = (
            offer.price_cents * count,
            total_grams - need,
            count,
            _selection_signature(ordered, counts),
        )
        single_covers.append((key, offer.price_cents * count))
    upper_cost = min(single_covers, key=lambda pair: pair[0])[1]

    states: dict[Decimal, _CombinationState] = {
        Decimal(0): _CombinationState(0, 0, ())
    }
    for index, (offer, grams) in enumerate(zip(ordered, gram_sizes)):
        next_states: dict[Decimal, _CombinationState] = {}
        for covered, state in states.items():
            max_by_cost = (upper_cost - state.cost_cents) // offer.price_cents
            k = 0
            while k <= max_by_cost:
                total_grams = covered + grams * k
                if total_grams >= coverage_bound:
                    break
                candidate = _CombinationState(
                    cost_cents=state.cost_cents + offer.price_cents * k,
                    package_count=state.package_count + k,
                    counts=state.counts + (k,),
                )
                prior = next_states.get(total_grams)
                candidate_key = (
                    candidate.cost_cents,
                    candidate.package_count,
                    _selection_signature(ordered[: index + 1], candidate.counts),
                )
                if prior is None:
                    next_states[total_grams] = candidate
                else:
                    prior_key = (
                        prior.cost_cents,
                        prior.package_count,
                        _selection_signature(ordered[: index + 1], prior.counts),
                    )
                    if candidate_key < prior_key:
                        next_states[total_grams] = candidate
                k += 1
        states = next_states

    candidates = [
        (covered, state)
        for covered, state in states.items()
        if covered >= need and state.package_count > 0
    ]
    if not candidates:
        return ()
    _, best = min(
        candidates,
        key=lambda pair: (
            pair[1].cost_cents,
            pair[0] - need,
            pair[1].package_count,
            _selection_signature(ordered, pair[1].counts),
        ),
    )
    return tuple(
        BasketItem(
            food=food,
            package=_package_for_offer(food, offer),  # type: ignore[arg-type]
            count=count,
            quote=offer.to_quote(food),
            offer=offer,
        )
        for offer, count in zip(ordered, best.counts)
        if count > 0
    )


# A shorter public alias for callers that think in offers rather than packages.
optimize_package_offers = minimum_cost_package_combination


@dataclass(frozen=True)
class _FoodPrice:
    """One food's share of a demand, priced (the shared per-food core)."""

    food: Food
    grams_needed: float
    from_pantry: float
    gap: float
    items: tuple[BasketItem, ...]  # empty when nothing must be bought or it is unpriced
    unpriced: bool  # a gap exists but there is no quote for this food


@dataclass(frozen=True)
class PricedDemand:
    items: tuple[BasketItem, ...]
    pantry_used: tuple[PantryUse, ...]
    nutrient_totals: Nutrients
    group_grams: tuple[tuple[FoodGroup, float], ...]  # sorted by FoodGroup.value
    total_cost: float
    total_gap_grams: float
    unpriced_gap_grams: float
    unpriced_food_ids: frozenset[str]


@dataclass(frozen=True)
class SlicePrice:
    total_cost: float
    unpriced_gap_grams: float
    unpriced_food_ids: frozenset[str]


def _iter_food_prices(
    demand: dict[str, float],
    pantry_items: dict[str, float],
    foods_by_id: dict[str, Food],
    quotes: PricingInputs | None,
) -> Iterator[_FoodPrice]:
    quotes = quotes or {}
    for food_id in sorted(demand):
        grams_needed = demand[food_id]
        if grams_needed <= GRAM_EPSILON:
            continue
        food = foods_by_id.get(food_id)
        if food is None:
            continue
        available = pantry_items.get(food_id, 0.0)
        from_pantry = min(grams_needed, available)
        gap = grams_needed - from_pantry
        items: tuple[BasketItem, ...] = ()
        unpriced = False
        if gap > GRAM_EPSILON:
            offers = offers_for_food(food, quotes.get(food_id))
            if not offers:
                unpriced = True  # cannot price it; count the gap, never guess
            else:
                items = minimum_cost_package_combination(food, gap, offers)
                if not items:
                    unpriced = True
        yield _FoodPrice(food, grams_needed, from_pantry, gap, items, unpriced)


def price_demand(
    demand: dict[str, float],
    pantry_items: dict[str, float],
    foods_by_id: dict[str, Food],
    quotes: PricingInputs | None,
) -> PricedDemand:
    items: list[BasketItem] = []
    pantry_used: list[PantryUse] = []
    nutrient_totals = Nutrients()
    group_grams: dict[FoodGroup, float] = {}
    total_gap = 0.0
    unpriced_gap = 0.0
    unpriced_ids: set[str] = set()

    for fp in _iter_food_prices(demand, pantry_items, foods_by_id, quotes):
        # Nutrition of the whole demand (what the plan actually consumes).
        nutrient_totals = nutrient_totals.plus(
            fp.food.nutrients_per_purchased_100g().scaled(fp.grams_needed / 100.0))
        group_grams[fp.food.food_group] = group_grams.get(fp.food.food_group, 0.0) + fp.grams_needed
        if fp.from_pantry > GRAM_EPSILON:
            pantry_used.append(PantryUse(food=fp.food, grams=round(fp.from_pantry, 3)))
        if fp.gap > GRAM_EPSILON:
            total_gap += fp.gap
            if fp.unpriced:
                unpriced_gap += fp.gap
                unpriced_ids.add(fp.food.id)
            else:
                items.extend(fp.items)

    return PricedDemand(
        items=tuple(items),
        pantry_used=tuple(pantry_used),
        nutrient_totals=nutrient_totals,
        group_grams=tuple(sorted(group_grams.items(), key=lambda kv: kv[0].value)),
        total_cost=sum(item.total_cost_cents for item in items) / 100.0,
        total_gap_grams=total_gap,
        unpriced_gap_grams=unpriced_gap,
        unpriced_food_ids=frozenset(unpriced_ids),
    )


def price_slice(
    demand: dict[str, float],
    pantry_items: dict[str, float],
    foods_by_id: dict[str, Food],
    quotes: PricingInputs | None,
) -> SlicePrice:
    """Same per-food core as ``price_demand`` but skips the nutrient/group/
    pantry-use aggregation the repair inner loop does not need."""
    items: list[BasketItem] = []
    unpriced_gap = 0.0
    unpriced_ids: set[str] = set()
    for fp in _iter_food_prices(demand, pantry_items, foods_by_id, quotes):
        if fp.gap > GRAM_EPSILON:
            if fp.unpriced:
                unpriced_gap += fp.gap
                unpriced_ids.add(fp.food.id)
            else:
                items.extend(fp.items)
    return SlicePrice(
        total_cost=sum(item.total_cost_cents for item in items) / 100.0,
        unpriced_gap_grams=unpriced_gap,
        unpriced_food_ids=frozenset(unpriced_ids),
    )


def build_shopping_result(
    demand: dict[str, float],
    pantry_items: dict[str, float],
    foods_by_id: dict[str, Food],
    quotes: PricingInputs,
    profile: HouseholdProfile,
    nutrition: NutritionService,
    budget: float,
    horizon_days: int,
    excluded: dict[str, str] | None = None,
) -> OptimizationResult:
    excluded = excluded or {}
    priced = price_demand(demand, pantry_items, foods_by_id, quotes)

    targets = nutrition.household_targets(profile, horizon_days)
    gaps = tuple(NutritionService.gaps(priced.nutrient_totals, targets))
    nutrition_feasible = all(
        (g.achieved / g.target) >= _DEEP_SHORTFALL for g in gaps if g.target > 0)

    # Priority order: a known overage always wins — missing prices can only
    # push the real total further up, never rescue it.
    if priced.total_cost > budget + COST_EPSILON:
        budget_status = BudgetStatus.OVER
    elif priced.unpriced_gap_grams > GRAM_EPSILON:
        budget_status = BudgetStatus.UNKNOWN
    else:
        budget_status = BudgetStatus.WITHIN

    relaxed: list[str] = []
    if budget_status is BudgetStatus.OVER and priced.items:
        relaxed.append(
            f"Estimated basket ${priced.total_cost:.2f} exceeds the ${budget:.2f} "
            "estimated basket budget cap; reduce the plan, raise the cap, or "
            "check for cheaper stores.")
    unpriced_names: tuple[str, ...] = ()
    if priced.unpriced_gap_grams > GRAM_EPSILON:
        # Name the foods, never a weight percentage — one unpriced ingredient
        # can dominate the real cost regardless of its grams.
        names = sorted(foods_by_id[fid].name for fid in priced.unpriced_food_ids)
        unpriced_names = tuple(names)
        shown = ", ".join(names[:3]) + (f", and {len(names) - 3} more" if len(names) > 3 else "")
        relaxed.append(f"No price data for: {shown} — the total above may be understated.")

    return OptimizationResult(
        items=priced.items,
        total_cost=priced.total_cost,
        budget=budget,
        score=0.0,
        nutrient_totals=priced.nutrient_totals,
        gaps=gaps,
        group_coverage=dict(priced.group_grams),
        groups_covered=len(priced.group_grams),
        distinct_foods=len({item.food.id for item in priced.items}),
        budget_status=budget_status,
        nutrition_feasible=nutrition_feasible,
        relaxed_constraints=tuple(relaxed),
        dominance_flags=(),
        unpriced_food_names=unpriced_names,
        excluded_foods=dict(excluded),
        horizon_days=horizon_days,
        pantry_used=priced.pantry_used,
        local_fallback_used=any(
            item.quote.source is PriceSource.SEED_ESTIMATE for item in priced.items
        ),
        local_fallback_food_ids=tuple(sorted({
            item.food.id
            for item in priced.items
            if item.quote.source is PriceSource.SEED_ESTIMATE
        })),
        local_fallback_sources=(
            (PriceSource.SEED_ESTIMATE,)
            if any(item.quote.source is PriceSource.SEED_ESTIMATE for item in priced.items)
            else ()
        ),
        unpriced_food_ids=tuple(sorted(priced.unpriced_food_ids)),
    )
