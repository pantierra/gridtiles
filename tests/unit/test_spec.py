import json

import pytest
from pydantic import ValidationError

from gridtiles.spec import LayerSpec

from ..conftest import FIXTURES


def test_example_spec() -> None:
    spec = LayerSpec.model_validate_json((FIXTURES / "spec_pop.json").read_text())
    assert spec.name == "pop"
    assert [v.name for v in spec.values] == ["population", "completeness"]
    assert spec.values[1].from_ == "osm_ratio"


def test_detail_defaults_to_4() -> None:
    spec = json.loads((FIXTURES / "spec_pop.json").read_text())
    del spec["detail"]
    assert LayerSpec.model_validate(spec).detail == 4


def test_round_trips_through_json() -> None:
    spec = LayerSpec.model_validate_json((FIXTURES / "spec_pop.json").read_text())
    assert LayerSpec.model_validate_json(spec.model_dump_json()) == spec
    assert '"from"' in spec.model_dump_json()


@pytest.mark.parametrize("path", sorted((FIXTURES / "invalid_specs").glob("*.json")), ids=lambda p: p.stem)
def test_invalid_specs_rejected(path) -> None:
    with pytest.raises(ValidationError):
        LayerSpec.model_validate_json(path.read_text())
