"""Versioned presentation and performance scope over immutable broker facts."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import yaml

from .domain import Snapshot


@dataclass(frozen=True, slots=True)
class InvestmentScopePolicy:
    version: str
    excluded_instrument_ids: frozenset[str]
    excluded_account_keys: frozenset[str] = frozenset()

    @classmethod
    def load(cls, path: Path) -> "InvestmentScopePolicy":
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict) or not data.get("version"):
            raise ValueError("Exclusion policy must have a version")
        entries = data.get("instruments")
        if not isinstance(entries, list):
            raise ValueError("Exclusion policy must have an instruments list")
        ids: set[str] = set()
        for entry in entries:
            if not isinstance(entry, dict) or not entry.get("uid"):
                raise ValueError("Each excluded instrument must have a UID")
            ids.add(str(entry["uid"]).strip())
            if entry.get("legacy_key"):
                ids.add(str(entry["legacy_key"]).strip())
        account_keys = data.get("excluded_account_keys", [])
        if not isinstance(account_keys, list) or any(not isinstance(key, str) for key in account_keys):
            raise ValueError("excluded_account_keys must be a list of account keys")
        return cls(str(data["version"]), frozenset(ids), frozenset(account_keys))

    def apply(self, snapshot: Snapshot) -> Snapshot:
        accounts = tuple(account for account in snapshot.accounts
                         if account.key not in self.excluded_account_keys)
        included_keys = {account.key for account in accounts}
        return Snapshot(
            as_of=snapshot.as_of,
            accounts=accounts,
            positions=tuple(valued for valued in snapshot.positions
                            if valued.position.account_key in included_keys and
                            valued.position.instrument_id not in self.excluded_instrument_ids),
            account_errors=tuple(error for error in snapshot.account_errors
                                 if not any(error.startswith(f"{key}:")
                                            for key in self.excluded_account_keys)),
            valuation_errors=tuple(error for error in snapshot.valuation_errors
                                   if not any(f":{key}:" in error
                                              for key in self.excluded_account_keys)
                                   and not any(error.endswith(f":{instrument_id}")
                                               for instrument_id in self.excluded_instrument_ids)),
        )
