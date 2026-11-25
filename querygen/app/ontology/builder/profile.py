"""The steward's Odoo replies for the Builder, checked and indexed (plan 2.1 introspect, 2.2 profile; D50, D52).

The steward's browser fetches both replies from Odoo and posts them to querygen, which holds them for one run and
then forgets them (D52). They carry metadata and aggregates only. Odoo's min and max of a date are not read here, so
they never reach the model. A reply of the wrong shape raises InputError, which the route turns into a 422.
"""
from dataclasses import dataclass
from typing import Any


class InputError(ValueError):
    """An Odoo reply, or a request option, is not one the Builder can read. The message names the part."""


@dataclass(frozen=True)
class FieldMeta:
    model: str
    name: str
    label: str
    type: str
    relation: str | None
    stored: bool
    custom: bool
    selection: list | None


def _text(item: dict, key: str, where: str) -> str:
    value = item.get(key)
    if not isinstance(value, str) or not value:
        raise InputError(f"{where}: {key} must be text")
    return value


def _whole(item: dict, key: str, where: str) -> int:
    value = item.get(key)
    if not isinstance(value, int) or isinstance(value, bool):
        raise InputError(f"{where}: {key} must be a whole number")
    return value


def _rows(raw: Any, where: str) -> list[dict]:
    if raw is None:
        return []
    if not isinstance(raw, list) or not all(isinstance(item, dict) for item in raw):
        raise InputError(f"{where} must be a list of objects")
    return raw


class Introspect:
    """The 2.1 reply: models with their fields, warehouses and locations (a location carries its OrgMap role)."""

    def __init__(self, raw: dict):
        self.odoo_version = _text(raw, "odoo_version", "introspect")
        self.models: dict[str, dict] = {}
        for item in _rows(raw.get("models"), "introspect.models"):
            model = _text(item, "model", "introspect.models")
            fields = {}
            for f in _rows(item.get("fields"), f"{model}.fields"):
                name = _text(f, "name", model)
                fields[name] = FieldMeta(
                    model=model, name=name, label=str(f.get("string") or name),
                    type=_text(f, "type", f"{model}.{name}"), relation=f.get("relation") or None,
                    stored=bool(f.get("store")), custom=bool(f.get("custom")),
                    selection=f.get("selection") or None)
            self.models[model] = {"name": str(item.get("name") or model), "custom": bool(item.get("custom")),
                                  "fields": fields}
        self.warehouses = _rows(raw.get("warehouses"), "introspect.warehouses")
        self.locations = _rows(raw.get("locations"), "introspect.locations")
        for item in self.warehouses:
            _whole(item, "id", "introspect.warehouses")
        for item in self.locations:
            _whole(item, "id", "introspect.locations")
            _text(item, "complete_name", "introspect.locations")
            _text(item, "usage", "introspect.locations")
            if item.get("warehouse_id") is not None:
                _whole(item, "warehouse_id", "introspect.locations")

    def field(self, model: str, name: str) -> FieldMeta | None:
        entry = self.models.get(model)
        return entry["fields"].get(name) if entry else None

    def has_model(self, model: str) -> bool:
        return model in self.models


class Profile:
    """The 2.2 reply: row counts and fill rates per model and field. A model the profile could not read is unprofiled."""

    def __init__(self, raw: dict):
        self.row_count: dict[str, int] = {}
        self._fill: dict[tuple[str, str], float] = {}
        for item in _rows(raw.get("models"), "profile.models"):
            if item.get("error"):
                continue
            model = _text(item, "model", "profile.models")
            self.row_count[model] = _whole(item, "row_count", model)
            for f in _rows(item.get("fields"), f"{model}.fields"):
                rate = f.get("fill_rate")
                if isinstance(rate, bool) or not isinstance(rate, (int, float)) or not 0 <= rate <= 1:
                    raise InputError(f"{model}: fill_rate must be a number from 0 to 1")
                self._fill[(model, _text(f, "name", model))] = float(rate)

    def profiled(self, model: str) -> bool:
        return model in self.row_count

    def fill(self, model: str, name: str) -> float | None:
        return self._fill.get((model, name))
