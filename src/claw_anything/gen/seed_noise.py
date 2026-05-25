"""Seed Noise: data model, loading, and sampling for daily routine noise library."""

from __future__ import annotations

import logging
import random
from pathlib import Path

import yaml
from pydantic import BaseModel, Field

log = logging.getLogger(__name__)


class SeedNoise(BaseModel):
    """A hand-written seed noise pattern for daily routine activities."""

    noise_id: str
    name: str
    description: str = ""

    involved_services: list[str] = Field(default_factory=list)
    leaves_trace: bool = False
    trace_type: str = ""  # "deleted_note", "discarded_draft", "cancelled_todo"

    action_sequence: list[str] = Field(default_factory=list)
    time_preference: str = "any"  # morning | midday | afternoon | evening | any

    adaptable_elements: list[str] = Field(default_factory=list)

    @classmethod
    def from_yaml(cls, path: str | Path) -> SeedNoise:
        path = Path(path)
        with open(path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f)
        return cls(**data)


class SeedNoiseLibrary:
    """Manages a collection of seed noise patterns and provides sampling."""

    def __init__(self, noise_dir: str | Path):
        self.noise_dir = Path(noise_dir).resolve()
        self.patterns: list[SeedNoise] = []
        self._load_all()

    def _load_all(self) -> None:
        """Load all .yaml files from noise_dir."""
        if not self.noise_dir.exists():
            log.warning("Seed noise directory not found: %s", self.noise_dir)
            return
        yaml_files = sorted(self.noise_dir.glob("*.yaml"))
        for f in yaml_files:
            try:
                pattern = SeedNoise.from_yaml(f)
                self.patterns.append(pattern)
            except Exception as e:
                log.warning("Failed to load seed noise %s: %s", f.name, e)
        log.info("Loaded %d seed noise patterns from %s", len(self.patterns), self.noise_dir)

    def sample(
        self,
        n: int = 4,
        primary_services: list[str] | None = None,
        rng: random.Random | None = None,
    ) -> list[SeedNoise]:
        """Sample n noise patterns, weighted towards primary_services.

        Args:
            n: Number of patterns to sample.
            primary_services: Persona's primary services (get higher weight).
            rng: Random instance for reproducibility.

        Returns:
            List of sampled SeedNoise patterns (may contain repeats if n > len).
        """
        if not self.patterns:
            return []

        rng = rng or random.Random()

        # Build weights: boost patterns whose services overlap with primary_services
        weights = []
        for p in self.patterns:
            w = 1.0
            if primary_services:
                overlap = set(p.involved_services) & set(primary_services)
                if overlap:
                    w = 2.0
            weights.append(w)

        # Weighted sampling with replacement
        return rng.choices(self.patterns, weights=weights, k=n)

    def sample_with_coverage(
        self,
        n: int = 4,
        primary_services: list[str] | None = None,
        rng: random.Random | None = None,
    ) -> list[SeedNoise]:
        """Sample n patterns ensuring service diversity.

        First picks one pattern per unique service (round-robin), then fills
        remaining slots with weighted random sampling.

        Args:
            n: Number of patterns to sample.
            primary_services: Persona's primary services (get higher weight).
            rng: Random instance for reproducibility.

        Returns:
            List of sampled SeedNoise patterns.
        """
        if not self.patterns:
            return []

        rng = rng or random.Random()

        # Group patterns by service
        by_service: dict[str, list[SeedNoise]] = {}
        for p in self.patterns:
            for svc in p.involved_services:
                by_service.setdefault(svc, []).append(p)

        # Round-robin: one per service (shuffled)
        services = list(by_service.keys())
        rng.shuffle(services)

        selected: list[SeedNoise] = []
        used_ids: set[str] = set()

        for svc in services:
            if len(selected) >= n:
                break
            candidates = [p for p in by_service[svc] if p.noise_id not in used_ids]
            if candidates:
                pick = rng.choice(candidates)
                selected.append(pick)
                used_ids.add(pick.noise_id)

        # Fill remaining with weighted random
        if len(selected) < n:
            remaining = n - len(selected)
            extras = self.sample(remaining, primary_services, rng)
            selected.extend(extras)

        return selected[:n]

    def get_all(self) -> list[SeedNoise]:
        """Return all loaded patterns."""
        return list(self.patterns)

    def get_by_id(self, noise_id: str) -> SeedNoise | None:
        """Retrieve a seed noise pattern by ID."""
        for p in self.patterns:
            if p.noise_id == noise_id:
                return p
        return None
