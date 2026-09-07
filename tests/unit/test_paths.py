"""Tests for the path factory."""

from __future__ import annotations

import os
import pickle
from collections.abc import Callable
from pathlib import Path
from typing import Final

import pytest
from upath import UPath
from upath.implementations.cloud import AzurePath

from parquet_to_xl.paths import ZPath, zpath


def test_cloud_uri_returns_the_registered_implementation() -> None:
    path: UPath = zpath("az://container/data.parquet", account_name="acct")
    assert isinstance(path, AzurePath)
    assert isinstance(path, UPath)
    assert path.protocol == "az"


def test_local_path_is_os_pathlike(tmp_path: Path) -> None:
    # The property the factory exists to preserve: polars and calamine take
    # these objects directly, which a proxy wrapper could not offer.
    path: UPath = zpath(tmp_path / "data.parquet")
    assert isinstance(path, os.PathLike)
    assert os.fspath(path) == str(tmp_path / "data.parquet")


def test_string_round_trip() -> None:
    assert str(zpath("az://container/data.parquet")) == "az://container/data.parquet"


def test_storage_options_accepted_as_a_mapping() -> None:
    path: UPath = zpath("az://container/data.parquet", storage_options={"account_name": "acct"})
    assert path.storage_options["account_name"] == "acct"


def test_storage_options_accepted_as_keywords() -> None:
    path: UPath = zpath("az://container/data.parquet", account_name="acct")
    assert path.storage_options["account_name"] == "acct"


def test_keywords_win_over_the_mapping() -> None:
    path: UPath = zpath(
        "az://container/data.parquet",
        storage_options={"account_name": "from-mapping"},
        account_name="from-keyword",
    )
    assert path.storage_options["account_name"] == "from-keyword"


def test_explicit_protocol_overrides_the_uri_scheme() -> None:
    path: UPath = zpath("/container/data.parquet", protocol="az", account_name="acct")
    assert path.protocol == "az"


def test_local_paths_carry_no_storage_options(tmp_path: Path) -> None:
    assert dict(zpath(tmp_path).storage_options) == {}


_DERIVATIONS: Final[list[Callable[[UPath], UPath]]] = [lambda p: p.parent, lambda p: p / "data.parquet"]


@pytest.mark.parametrize("derive", _DERIVATIONS)
def test_credentials_survive_derivation(derive: Callable[[UPath], UPath]) -> None:
    # Derived paths never pass back through the factory, so this pins the
    # inheritance that makes a construction-only seam sufficient.
    path: UPath = derive(zpath("az://container/subdir/", account_name="acct"))
    assert path.storage_options["account_name"] == "acct"


def test_pickle_round_trip_preserves_storage_options() -> None:
    # This is the driver-to-executor boundary on Spark: UPath.__reduce__
    # reconstructs the path from its storage options, credentials included.
    path: UPath = zpath("az://container/data.parquet", account_name="acct", sas_token="sv=fake")
    restored: UPath = pickle.loads(pickle.dumps(path))  # noqa: S301
    assert str(restored) == str(path)
    assert restored.storage_options["account_name"] == "acct"
    assert restored.storage_options["sas_token"] == "sv=fake"


def test_sas_token_string_is_accepted_as_a_credential() -> None:
    path: UPath = zpath("az://container/data.parquet", account_name="acct", credential="sv=fake")
    assert path.storage_options["credential"] == "sv=fake"


def test_live_credential_object_is_rejected() -> None:
    class _FakeCredential:
        def get_token(self, *_scopes: str) -> str:
            return "token"

    with pytest.raises(TypeError, match="cannot be pickled"):
        zpath("az://container/data.parquet", account_name="acct", credential=_FakeCredential())


def test_local_filesystem_round_trip(tmp_path: Path) -> None:
    path: UPath = zpath(tmp_path) / "data.txt"
    path.write_text("hello")
    assert path.read_text() == "hello"
    assert path.exists()
    assert [child.name for child in zpath(tmp_path).iterdir()] == ["data.txt"]


def test_memory_filesystem_round_trip() -> None:
    path: UPath = zpath("memory://container/data.txt")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("hello")
    assert path.read_text() == "hello"


def test_a_direct_upath_subclass_is_rejected_by_upath() -> None:
    # Records why this module is a factory rather than the UPath subclass the
    # requirements spec described: since universal-pathlib 0.3.9, UPath.__new__
    # rejects a subclass that is not registered for the detected protocol.
    class _Direct(UPath):
        pass

    with pytest.raises(TypeError):
        _Direct("az://container/data.parquet")


def test_zpath_alias_is_the_factory() -> None:
    assert ZPath is zpath
    assert str(ZPath("az://container/data.parquet")) == "az://container/data.parquet"
